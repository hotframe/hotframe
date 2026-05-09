# SPDX-License-Identifier: Apache-2.0
"""
``@view`` decorator and HTTP response helpers.

``@view`` provides the conventions for HTML routes: session-based auth,
permission gating, automatic template discovery (``{module}/pages/{view}.html``),
and full-page server-rendered HTML. Reactive UI updates are not the
responsibility of these helpers — they live on the WebSocket-backed
:mod:`hotframe.live` runtime instead.

The redirect / refresh / message helpers exposed here are simple HTTP
responses (303 redirect, meta-refresh, inline HTML toast) that any
non-live route can return.
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import AsyncGenerator, Callable
from functools import lru_cache, wraps
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import TemplateNotFound
from sse_starlette.sse import EventSourceResponse
from starlette.responses import RedirectResponse, Response

from hotframe.auth.auth import get_session_user_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Permission resolution
# ---------------------------------------------------------------------------


async def _resolve_permissions(request: Request, user_id: Any) -> list[str]:
    """Load user permissions via the configured PERMISSION_RESOLVER.

    Falls back to empty list if no resolver is configured.
    """
    from hotframe.config.settings import get_settings

    settings = get_settings()
    if not settings.PERMISSION_RESOLVER:
        return []

    module_path, func_name = settings.PERMISSION_RESOLVER.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    resolver = getattr(mod, func_name)
    return await resolver(request, user_id)


# ---------------------------------------------------------------------------
# Request introspection
# ---------------------------------------------------------------------------


def is_reactive_request(request: Request) -> bool:
    """Return ``False``.

    All HTTP routes serve full pages; live updates flow over the
    WebSocket runtime in :mod:`hotframe.live`, never via per-request
    headers. The function is kept so callers that wrap branches around
    it remain well-typed; new code should not rely on it.
    """
    return False


def is_htmx_request(request: Request) -> bool:
    """Alias of :func:`is_reactive_request`."""
    return is_reactive_request(request)


# ---------------------------------------------------------------------------
# Template auto-discovery
# ---------------------------------------------------------------------------


_PARTIAL_PATTERNS = (
    "{module}/partials/{view}_content.html",
    "{module}/partials/{view}.html",
    "{module}/partials/{view}_list.html",
    "{module}/partials/{view}_form.html",
)

_FULL_PATTERNS = (
    "{module}/pages/{view}.html",
    "{module}/pages/{view}_list.html",
    "{module}/pages/{view}_form.html",
    "{module}/pages/list.html",
    "{module}/pages/index.html",
)


_ENV_BY_ID: dict[int, Any] = {}


def _register_env(env: Any) -> int:
    _ENV_BY_ID[id(env)] = env
    return id(env)


@lru_cache(maxsize=512)
def _resolve_template(env_id: int, module_id: str, view_id: str, kind: str) -> str:
    env = _ENV_BY_ID.get(env_id)
    if env is None:
        raise RuntimeError("Jinja2 environment not registered for template resolution")
    patterns = _PARTIAL_PATTERNS if kind == "partial" else _FULL_PATTERNS
    candidates: list[str] = []
    if kind == "full" and view_id == "dashboard":
        candidates.append(f"{module_id}/pages/index.html")
    for pattern in patterns:
        candidates.append(pattern.format(module=module_id, view=view_id))
    seen: set[str] = set()
    ordered: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    for name in ordered:
        try:
            env.get_template(name)
            return name
        except TemplateNotFound:
            continue
    return ordered[0]


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def view(
    full_template: str | None = None,
    partial_template: str | None = None,
    module_id: str | None = None,
    view_id: str | None = None,
    login_required: bool = True,
    permissions: list[str] | str | None = None,
) -> Callable:
    """View decorator for HTML routes.

    Performs auth + permission checks, resolves templates by
    convention (``{module}/pages/{view}.html`` and variants), and
    renders the result as a full HTML page.

    ``partial_template`` is reserved for the rare case where a route
    deliberately returns only an inner block. To override the resolved
    template, pass a ``template`` key in the result dict.
    """
    if isinstance(permissions, str):
        permissions = [permissions]

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(request: Request, *args: Any, **kwargs: Any) -> Response:
            from hotframe.config.settings import get_settings

            settings = get_settings()

            # 1. Authentication
            if login_required:
                user_id = get_session_user_id(request)
                if user_id is None:
                    return RedirectResponse(settings.AUTH_LOGIN_URL, status_code=302)

                if permissions:
                    from hotframe.auth.permissions import has_permission

                    user_perms: list[str] | None = getattr(
                        request.state,
                        "user_permissions",
                        None,
                    )
                    if user_perms is None:
                        user_perms = await _resolve_permissions(request, user_id)
                        request.state.user_permissions = user_perms

                    if not all(has_permission(user_perms, p) for p in permissions):
                        return RedirectResponse(
                            settings.AUTH_UNAUTHORIZED_URL,
                            status_code=302,
                        )

            # 2. Call the view function
            result = await func(request, *args, **kwargs)

            if isinstance(result, Response):
                return result

            context: dict[str, Any] = result if isinstance(result, dict) else {}

            # 3. Build template context
            from hotframe.templating.globals import get_global_context

            global_ctx = await get_global_context(request)
            merged = {**global_ctx, **context}

            if module_id:
                registry = getattr(request.app.state, "module_registry", None)
                navigation = registry.get_navigation(module_id) if registry else []
                merged["module_id"] = module_id
                merged["view_id"] = view_id
                merged["navigation"] = navigation
                merged["current_view"] = view_id
                merged["current_module"] = module_id

            # 4. Resolve templates
            _full = full_template
            _partial = partial_template

            templates = request.app.state.templates

            if module_id and view_id:
                env_id = _register_env(templates.env)
                if not _partial:
                    _partial = _resolve_template(env_id, module_id, view_id, "partial")
                if not _full:
                    _full = _resolve_template(env_id, module_id, view_id, "full")

            return _render_full(templates, request, merged, _full, _partial)

        return wrapper

    return decorator


# Alias.
htmx_view = view


# ---------------------------------------------------------------------------
# Render helpers (private)
# ---------------------------------------------------------------------------


def _render_full(
    templates: Any,
    request: Request,
    context: dict[str, Any],
    full: str | None,
    partial: str | None,
) -> Response:
    context["content_template"] = context.pop("template", None) or partial
    tpl_name = full or "page_base.html"
    try:
        return templates.TemplateResponse(request, tpl_name, context)
    except Exception as exc:
        logger.error("Template render error in %s: %s", tpl_name, exc)
        return HTMLResponse(
            f'<div class="alert alert-error">'
            f"<strong>Template Error</strong>: {tpl_name}<br>"
            f"<small>{type(exc).__name__}: {exc}</small></div>",
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Redirect / refresh / message helpers
# ---------------------------------------------------------------------------
#
# Plain HTTP responses any non-live route can return. Live components
# call ``self.navigate(...)`` / ``self.toast(...)`` instead and never
# go through these helpers.
# ---------------------------------------------------------------------------


def reactive_redirect(url: str) -> Response:
    """Issue a 303 See Other redirect."""
    return RedirectResponse(url, status_code=303)


def reactive_refresh() -> Response:
    """Reload the current page via ``<meta http-equiv="refresh">``."""
    return HTMLResponse('<meta http-equiv="refresh" content="0">', status_code=200)


def reactive_trigger(name: str, **detail: Any) -> Response:
    """Dispatch a ``CustomEvent`` on the client.

    Returns an HTML fragment with a one-line inline script that calls
    ``window.dispatchEvent``. Useful for non-live routes that need to
    notify a listener already in the DOM.
    """
    payload = json.dumps(detail, ensure_ascii=False, default=str)
    script = (
        f"<script>window.dispatchEvent(new CustomEvent("
        f"{json.dumps(name)}, {{detail: {payload}}}))</script>"
    )
    return HTMLResponse(script, status_code=200)


def reactive_message(level: str, text: str) -> Response:
    """Return an HTML fragment representing a flash toast."""
    safe_level = (level or "info").replace('"', "")
    safe_text = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return HTMLResponse(
        f'<div class="toast toast-{safe_level}" role="status">{safe_text}</div>',
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


def htmx_redirect(url: str) -> Response:
    return reactive_redirect(url)


def htmx_refresh() -> Response:
    return reactive_refresh()


def htmx_trigger(event: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an event-payload dict suitable for trigger headers."""
    if data:
        return {event: data}
    return {event: True}


# ---------------------------------------------------------------------------
# Flash / inline messages
# ---------------------------------------------------------------------------


def add_message(request: Request, level: str, text: str) -> None:
    """Append a flash message for the current request.

    Stored on ``request.state._messages``; the session-flash middleware
    replays it on the next full-page response. Live components call
    ``self.toast(...)`` directly instead and never touch this.
    """
    if not hasattr(request.state, "_messages"):
        request.state._messages = []
    request.state._messages.append({"level": level, "text": text})


# ---------------------------------------------------------------------------
# Generic SSE stream — useful for one-way pushes (log tailing etc.)
# ---------------------------------------------------------------------------


async def sse_stream(
    request: Request,
    generator: AsyncGenerator[dict[str, Any] | str, None],
    *,
    event_type: str = "message",
    ping_interval: int = 15,
) -> EventSourceResponse:
    """Wrap an async generator as a Server-Sent Events response.

    A plain SSE helper for one-way pushes (log streaming, progress
    updates). The LiveComponent runtime has its own WebSocket path
    and does not use this.
    """

    async def event_generator() -> AsyncGenerator[dict[str, str], None]:
        try:
            async for chunk in generator:
                if await request.is_disconnected():
                    logger.debug("SSE client disconnected, stopping stream")
                    break

                try:
                    data = (
                        json.dumps(chunk, ensure_ascii=False, default=str)
                        if isinstance(chunk, dict)
                        else str(chunk)
                    )
                except (TypeError, ValueError):
                    logger.warning("Failed to serialize SSE chunk, skipping", exc_info=True)
                    continue
                yield {"event": event_type, "data": data}

            yield {"event": "done", "data": ""}
        except Exception as exc:
            import traceback

            tb = traceback.format_exc()
            logger.error("Error in SSE stream: %s\n%s", exc, tb)
            yield {
                "event": "error",
                "data": json.dumps({"error": f"Internal server error: {exc}"}),
            }

    return EventSourceResponse(
        event_generator(),
        ping=ping_interval,
    )
