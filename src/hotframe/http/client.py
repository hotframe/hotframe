# SPDX-License-Identifier: Apache-2.0
"""
:class:`AuthenticatedClient` — thin wrapper around ``httpx.AsyncClient``.

The client applies an :class:`~hotframe.http.auth.Auth` strategy to
every outgoing request and, optionally, pipes the dispatch through a
chain of :class:`~hotframe.http.interceptors.Interceptor` instances —
Angular-style HTTP middleware with retry, circuit-breaker, and token
refresh hooks.

All HTTP-facing concerns (connection pooling, timeouts, streaming,
transport selection) are delegated to ``httpx`` — hotframe does not
re-implement any of them.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from hotframe.http.auth import Auth, NoAuth
from hotframe.http.events import (
    EVENT_REQUEST_COMPLETED,
    EVENT_REQUEST_FAILED,
    EVENT_REQUEST_STARTED,
)
from hotframe.http.interceptors import Interceptor, build_chain

if TYPE_CHECKING:
    from hotframe.signals.dispatcher import AsyncEventBus

logger = logging.getLogger(__name__)


class AuthenticatedClient:
    """Async HTTP client that applies an :class:`Auth` strategy per request.

    Wraps ``httpx.AsyncClient``. When no interceptors are attached the
    auth strategy is wired as an ``httpx`` request event hook — so the
    on-wire behavior matches ``httpx``'s own ``auth=`` semantics.

    When interceptors are attached, the auth strategy runs **inside**
    the terminal of the interceptor chain instead. This makes every
    retry inside a :class:`RefreshInterceptor` re-read the auth
    credential source, so a token refreshed mid-flight is picked up on
    the next attempt without any special plumbing.

    Lifecycle events (``http.request.{started,completed,failed}``) are
    emitted once per external ``request()`` call — not once per internal
    retry. A retry-storm would otherwise drown observability.

    Args:
        base_url: Base URL prefix applied to relative paths.
        auth: Authentication strategy; defaults to :class:`NoAuth`.
        timeout: Request timeout (seconds or an ``httpx.Timeout``).
        headers: Default headers applied to every request.
        transport: Optional ``httpx.AsyncBaseTransport`` override —
            commonly ``httpx.MockTransport`` in tests.
        event_bus: Optional hotframe event bus used to emit the
            ``http.request.{started,completed,failed}`` events.
        name: Optional client name included in emitted events. Useful
            when the same client instance is shared across modules.
        interceptors: Optional ordered list of interceptors wrapping
            every dispatch. Passing an empty list is equivalent to
            passing ``None`` — no chain overhead, auth still runs via
            the ``httpx`` event hook.
    """

    def __init__(
        self,
        base_url: str = "",
        auth: Auth | None = None,
        timeout: float | httpx.Timeout = 10.0,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        event_bus: AsyncEventBus | None = None,
        name: str | None = None,
        interceptors: list[Interceptor] | None = None,
    ) -> None:
        self._auth: Auth = auth if auth is not None else NoAuth()
        self._event_bus = event_bus
        self._name = name
        self._interceptors: list[Interceptor] = list(interceptors) if interceptors else []
        # Auth runs as an httpx event hook ONLY when there's no
        # interceptor chain; otherwise the chain's terminal applies it
        # so refresh-and-retry picks up the new credential.
        event_hooks: dict[str, list] = {}
        if not self._interceptors:
            event_hooks["request"] = [self._apply_auth_hook]
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers=headers or {},
            transport=transport,
            event_hooks=event_hooks,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def auth(self) -> Auth:
        """Return the currently active :class:`Auth` strategy."""
        return self._auth

    @property
    def name(self) -> str | None:
        """Return the optional client name used in event payloads."""
        return self._name

    @property
    def base_url(self) -> httpx.URL:
        """Return the underlying ``httpx.AsyncClient`` base URL."""
        return self._client.base_url

    @property
    def headers(self) -> httpx.Headers:
        """Return the default headers applied to every request."""
        return self._client.headers

    @property
    def is_closed(self) -> bool:
        """Return ``True`` if the underlying client has been closed."""
        return self._client.is_closed

    @property
    def interceptors(self) -> list[Interceptor]:
        """Return a shallow copy of the attached interceptor chain."""
        return list(self._interceptors)

    def set_interceptors(self, interceptors: list[Interceptor] | None) -> None:
        """Replace the interceptor chain atomically.

        Passing ``None`` or an empty list clears the chain and
        reinstates the event-hook auth path for raw throughput.
        """
        self._interceptors = list(interceptors) if interceptors else []
        # Reconfigure the auth path to match the new state.
        if self._interceptors:
            self._client.event_hooks["request"] = []
        else:
            self._client.event_hooks["request"] = [self._apply_auth_hook]

    # ------------------------------------------------------------------
    # httpx event hook — runs before every request dispatch
    # ------------------------------------------------------------------

    async def _apply_auth_hook(self, request: httpx.Request) -> None:
        """httpx request event hook: apply the current auth strategy."""
        await self._auth.apply(request)

    # ------------------------------------------------------------------
    # HTTP methods — delegated to httpx.AsyncClient with event emission
    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        url: httpx.URL | str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Dispatch an HTTP request using the wrapped ``httpx.AsyncClient``.

        Emits ``http.request.started``, ``http.request.completed``, or
        ``http.request.failed`` via the attached event bus when one is
        configured. Events fire once per external call — internal retry
        attempts do not produce additional events.
        """
        await self._emit(EVENT_REQUEST_STARTED, method=method, url=str(url))
        started_at = time.perf_counter()
        try:
            if self._interceptors:
                response = await self._dispatch_with_chain(method, url, **kwargs)
            else:
                response = await self._client.request(method, url, **kwargs)
        except Exception as exc:
            duration_ms = (time.perf_counter() - started_at) * 1000.0
            await self._emit(
                EVENT_REQUEST_FAILED,
                method=method,
                url=str(url),
                error=str(exc),
                duration_ms=duration_ms,
            )
            raise
        duration_ms = (time.perf_counter() - started_at) * 1000.0
        await self._emit(
            EVENT_REQUEST_COMPLETED,
            method=method,
            url=str(response.request.url if response.request is not None else url),
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response

    async def _dispatch_with_chain(
        self,
        method: str,
        url: httpx.URL | str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Run the request through the interceptor chain.

        The terminal closure applies auth each time — so a refresh
        interceptor's retry re-reads the (now-rotated) credential
        source — and then dispatches via ``httpx.AsyncClient.send``.
        """
        request = self._client.build_request(method, url, **kwargs)

        async def terminal(req: httpx.Request) -> httpx.Response:
            await self._auth.apply(req)
            return await self._client.send(req)

        chain = build_chain(self._interceptors, terminal)
        return await chain(request)

    async def get(self, url: httpx.URL | str, **kwargs: Any) -> httpx.Response:
        """Issue an HTTP ``GET`` request."""
        return await self.request("GET", url, **kwargs)

    async def post(self, url: httpx.URL | str, **kwargs: Any) -> httpx.Response:
        """Issue an HTTP ``POST`` request."""
        return await self.request("POST", url, **kwargs)

    async def put(self, url: httpx.URL | str, **kwargs: Any) -> httpx.Response:
        """Issue an HTTP ``PUT`` request."""
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: httpx.URL | str, **kwargs: Any) -> httpx.Response:
        """Issue an HTTP ``PATCH`` request."""
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: httpx.URL | str, **kwargs: Any) -> httpx.Response:
        """Issue an HTTP ``DELETE`` request."""
        return await self.request("DELETE", url, **kwargs)

    def stream(
        self,
        method: str,
        url: httpx.URL | str,
        **kwargs: Any,
    ) -> Any:
        """Return an ``httpx`` streaming context manager.

        Streaming does not go through :meth:`request`: the auth hook
        still runs (when no interceptors are attached) but the event
        bus lifecycle events are skipped because ``httpx`` manages the
        stream's completion semantics itself. Interceptors do not wrap
        streamed responses either.
        """
        return self._client.stream(method, url, **kwargs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` and its transport."""
        await self._client.aclose()

    async def __aenter__(self) -> AuthenticatedClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _emit(self, event_name: str, **payload: Any) -> None:
        """Emit a hotframe event if an ``event_bus`` is attached."""
        if self._event_bus is None:
            return
        try:
            await self._event_bus.emit(
                event_name,
                client_name=self._name,
                **payload,
            )
        except Exception:
            # Event emission must never break an HTTP call. Log and
            # carry on — observability failures are not business logic.
            logger.exception("Failed to emit %s for client %r", event_name, self._name)

    def __repr__(self) -> str:
        return (
            f"<AuthenticatedClient name={self._name!r} "
            f"base_url={str(self._client.base_url)!r} "
            f"auth={type(self._auth).__name__} "
            f"interceptors={len(self._interceptors)}>"
        )


__all__ = ["AuthenticatedClient"]
