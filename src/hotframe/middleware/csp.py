"""
Content Security Policy middleware.

Generates a unique nonce per request, stores it on ``request.state.csp_nonce``,
and adds the appropriate CSP header to every response.
"""

from __future__ import annotations

import secrets
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from hotframe.auth.csp import build_csp_header


class CSPMiddleware(BaseHTTPMiddleware):
    """Add Content-Security-Policy headers with per-request nonce."""

    def __init__(self, app: Any, enforce: bool = False) -> None:
        super().__init__(app)
        self._enforce = enforce

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        nonce = secrets.token_urlsafe(32)
        request.state.csp_nonce = nonce

        response = await call_next(request)

        header_name, header_value = build_csp_header(nonce, self._enforce)
        response.headers[header_name] = header_value

        # HSTS: enforce HTTPS in production (only when accessed via HTTPS)
        if self._enforce and request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        # Prevent search engine indexing (Hubs are private apps behind auth)
        response.headers["X-Robots-Tag"] = "noindex, nofollow"

        return response
