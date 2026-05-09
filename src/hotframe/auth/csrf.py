# SPDX-License-Identifier: Apache-2.0
"""
Double-submit cookie CSRF protection.

Exempt route prefixes are configured via ``settings.CSRF_EXEMPT_PREFIXES``.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

COOKIE_NAME = "csrf_token"
HEADER_NAME = "x-csrf-token"
FORM_FIELD = "csrf_token"


def generate_csrf_token() -> str:
    """Generate a cryptographically secure CSRF token (32 bytes, URL-safe)."""
    return secrets.token_urlsafe(32)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF middleware for FastAPI/Starlette."""

    def __init__(self, app: ASGIApp, exempt_prefixes: tuple[str, ...] | None = None) -> None:
        super().__init__(app)
        if exempt_prefixes is not None:
            self._exempt_prefixes = exempt_prefixes
        else:
            from hotframe.config.settings import get_settings

            settings = get_settings()
            self._exempt_prefixes = tuple(settings.CSRF_EXEMPT_PREFIXES)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        # Skip CSRF processing (including Set-Cookie) for static assets so
        # they remain cacheable by CDNs and browsers without per-user cookies.
        if request.url.path.startswith("/static/"):
            return await call_next(request)

        token = request.cookies.get(COOKIE_NAME)
        new_token = False

        if not token:
            token = generate_csrf_token()
            new_token = True

        request.state.csrf_token = token

        if request.method in _UNSAFE_METHODS and not self._is_exempt(request):
            submitted = await self._get_submitted_token(request)
            if not submitted or not secrets.compare_digest(submitted, token):
                return JSONResponse(
                    {"detail": "CSRF validation failed"},
                    status_code=403,
                )

        response: Response = await call_next(request)

        if new_token:
            response.set_cookie(
                key=COOKIE_NAME,
                value=token,
                httponly=False,
                samesite="lax",
                secure=request.url.scheme == "https",
                path="/",
                max_age=86400 * 30,
            )

        return response

    def _is_exempt(self, request: Request) -> bool:
        path = request.url.path
        return any(path.startswith(prefix) for prefix in self._exempt_prefixes)

    @staticmethod
    async def _get_submitted_token(request: Request) -> str | None:
        header_token = request.headers.get(HEADER_NAME)
        if header_token:
            return header_token

        content_type = request.headers.get("content-type", "")
        if (
            "application/x-www-form-urlencoded" in content_type
            or "multipart/form-data" in content_type
        ):
            try:
                form = await request.form()
                form_token = form.get(FORM_FIELD)
                if form_token:
                    return str(form_token)
            except Exception:
                pass

        return None
