# SPDX-License-Identifier: Apache-2.0
"""
Module boundary middleware.

Catches exceptions raised by module routes during a request and returns a
contained 5xx response without taking the Hub Core down. Tracks repeated
failures per module in a rolling window and, once a threshold is crossed,
marks the module as ``degraded`` in the DB and emits a ``module.degraded``
event so UI/alerting can react.

Scope: this middleware ONLY intercepts requests whose path starts with a
module mount point — ``/m/{module_id}/...`` or ``/api/v1/m/{module_id}/...``.
Hub Core routes pass through untouched, so a bug in this middleware can
never break the Hub Core's own error handling.

What it does NOT do (by design — see doc 05 §6):

- Does not stop bugs in C extensions that segfault — that requires
  process isolation (Nivel 3), out of scope.
- Does not stop infinite loops — that's :class:`TimeoutMiddleware`'s job.
- Does not auto-disable degraded modules — the user decides via the
  marketplace UI.

The in-memory tracker is per-process. Workers each maintain their own;
restart resets the counter. For a Hub running a single uvicorn worker
(the common case) this is exactly the desired behaviour.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

if TYPE_CHECKING:
    from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


# Regex captures both HTML view (``/m/{id}/...``) and API
# (``/api/v1/m/{id}/...``) module mount points. Module IDs are
# constrained to lowercase alphanum + underscore + hyphen — same
# character set the loader/manifest accept.
_MODULE_URL = re.compile(r"^/(?:api/v1/)?m/([a-z0-9_-]+)(?:/|$)")


@dataclass
class _ModuleErrorTracker:
    """Rolling window of error timestamps for one module.

    Bounded ``deque`` so memory stays O(threshold) per module even if the
    threshold logic is bypassed somehow. ``time.monotonic()`` is used so
    wall-clock changes (NTP step) cannot make events look "in the past".
    """

    threshold: int = 10
    window_seconds: float = 60.0
    errors: deque[float] = field(default_factory=lambda: deque(maxlen=50))

    def record(self) -> bool:
        """Append an error timestamp and return ``True`` if the threshold is met.

        Old entries outside the rolling window are pruned on every call —
        cheap because the deque is bounded.
        """
        now = time.monotonic()
        self.errors.append(now)
        cutoff = now - self.window_seconds
        while self.errors and self.errors[0] < cutoff:
            self.errors.popleft()
        return len(self.errors) >= self.threshold

    def reset(self) -> None:
        """Drop all recorded errors. Called after the user re-activates the module."""
        self.errors.clear()


class ModuleBoundaryMiddleware(BaseHTTPMiddleware):
    """Containment boundary for exceptions raised inside a module's request handler.

    Place this middleware *after* CSRF / DB-session middleware (so the
    request-scoped DB session, if any, is available when we mark the
    module degraded) and *before* :class:`ModuleMiddlewareManager` (so a
    buggy module-contributed middleware also gets caught).

    The tracker is per-instance, so tests can construct an isolated
    middleware with their own thresholds.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        threshold: int = 10,
        window_seconds: float = 60.0,
    ) -> None:
        super().__init__(app)
        self._threshold = threshold
        self._window_seconds = window_seconds
        self._trackers: dict[str, _ModuleErrorTracker] = defaultdict(
            lambda: _ModuleErrorTracker(
                threshold=self._threshold,
                window_seconds=self._window_seconds,
            )
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reset_tracker(self, module_id: str) -> None:
        """Clear the rolling error window for a module.

        Called by the marketplace ``reactivate`` endpoint so an explicit
        user action gives the module a clean slate.
        """
        if module_id in self._trackers:
            self._trackers[module_id].reset()

    # ------------------------------------------------------------------
    # ASGI dispatch
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        module_id = self._extract_module_id(request.url.path)
        if module_id is None:
            # Not a module route — pass through unmodified so we never
            # interfere with Hub Core error semantics.
            return await call_next(request)

        try:
            return await call_next(request)
        except Exception as exc:
            logger.exception(
                "Module boundary captured exception in %s on %s %s: %s",
                module_id,
                request.method,
                request.url.path,
                exc,
            )
            await self._handle_error(request, module_id, exc)
            return self._render_error(request, module_id, exc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_module_id(path: str) -> str | None:
        m = _MODULE_URL.match(path)
        return m.group(1) if m else None

    async def _handle_error(
        self,
        request: Request,
        module_id: str,
        error: Exception,
    ) -> None:
        """Update the rolling tracker, emit events, and (if needed) flip to degraded.

        Every step is wrapped in a ``try/except`` because the boundary
        middleware itself must never raise — doing so would defeat the
        entire purpose of containment.
        """
        tracker = self._trackers[module_id]
        try:
            should_degrade = tracker.record()
        except Exception:
            logger.exception("Boundary tracker.record failed for %s", module_id)
            should_degrade = False

        # Emit ``module.error`` so observers (logs, UI banners, alerting)
        # can react. ``app.state.event_bus`` is created in lifespan, so it
        # is present in normal app boot but may be missing in unit tests
        # that build a bare ASGI app without the lifespan running.
        bus = getattr(request.app.state, "event_bus", None)
        if bus is not None:
            try:
                await bus.emit(
                    "module.error",
                    module_id=module_id,
                    error=str(error),
                    error_type=type(error).__name__,
                    path=request.url.path,
                    method=request.method,
                )
            except Exception:
                logger.exception("Failed to emit module.error for %s", module_id)

        if not should_degrade:
            return

        # Threshold reached — mark degraded in DB and emit ``module.degraded``.
        # Use the request's session if a session middleware put one on
        # ``request.state``; fall back to opening one from the project
        # session factory. Both branches are best-effort — if neither
        # works we still return a contained response to the caller.
        marked = await self._mark_degraded(request, module_id, error)
        if marked and bus is not None:
            try:
                await bus.emit(
                    "module.degraded",
                    module_id=module_id,
                    error=str(error),
                    threshold=self._threshold,
                    window_seconds=self._window_seconds,
                )
            except Exception:
                logger.exception("Failed to emit module.degraded for %s", module_id)

    async def _mark_degraded(
        self,
        request: Request,
        module_id: str,
        error: Exception,
    ) -> bool:
        """Persist ``status='degraded'`` for the module. Returns True on success.

        Tries, in order:

        1. ``request.state.session`` — populated by a project-supplied DB
           middleware that exposes the request-scoped SQLAlchemy session.
        2. ``hotframe.config.database.get_session_factory()`` — opens a
           fresh session, commits, closes it. Always works in a normal
           bootstrap.
        """
        from hotframe.engine.state import ModuleStateDB

        message = (
            f"Module {module_id} crossed boundary threshold "
            f"({self._threshold} errors / {self._window_seconds:.0f}s). "
            f"Last error: {type(error).__name__}: {error}"
        )

        state_db = ModuleStateDB()
        # Prefer the request-scoped session if the project exposes one.
        session = getattr(request.state, "session", None)
        if session is not None:
            try:
                await state_db.set_degraded(session, module_id, error_message=message)
                # Caller is responsible for commit on the request session;
                # we deliberately do NOT commit here.
                return True
            except Exception:
                logger.exception(
                    "Failed to mark module %s degraded via request session",
                    module_id,
                )

        # Fall back to a transient session.
        try:
            from hotframe.config.database import get_session_factory

            factory = get_session_factory()
            async with factory() as boundary_session:
                await state_db.set_degraded(
                    boundary_session,  # type: ignore[arg-type]
                    module_id,
                    error_message=message,
                )
                await boundary_session.commit()
            return True
        except Exception:
            logger.exception(
                "Failed to open transient session to mark module %s degraded",
                module_id,
            )
            return False

    @staticmethod
    def _render_error(
        request: Request,
        module_id: str,
        error: Exception,
    ) -> Response:
        """Return a contained 500 response.

        Picks JSON for API routes (``/api/v1/m/...``) or when the client
        sent an ``Accept: application/json`` header; otherwise returns a
        minimal HTML stub. We deliberately do *not* render through the
        project's Jinja env here — that env may itself be the source of
        the failure (a broken module template) and we must not double-fault.
        """
        is_api = request.url.path.startswith("/api/")
        accept = request.headers.get("accept", "")
        wants_json = is_api or "application/json" in accept

        if wants_json:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "module_unavailable",
                    "module_id": module_id,
                    "detail": (
                        f"Module '{module_id}' raised an unhandled exception. "
                        "The rest of the Hub continues to operate."
                    ),
                    "error_type": type(error).__name__,
                },
            )

        # Plain HTML fallback. No Jinja, no nonces — the response must be
        # renderable even if the project's template engine is itself the
        # broken piece.
        body = (
            "<!doctype html><html lang='en'><head>"
            "<meta charset='utf-8'>"
            f"<title>Module {module_id} unavailable</title>"
            "</head><body>"
            f"<h1>Module &quot;{module_id}&quot; is not available</h1>"
            "<p>This module raised an unhandled exception. "
            "The rest of the Hub keeps working — you can navigate away "
            "and disable the module from the marketplace.</p>"
            f"<p><a href='/'>Back to the Hub</a></p>"
            "</body></html>"
        )
        return HTMLResponse(content=body, status_code=503)


__all__ = ["ModuleBoundaryMiddleware"]
