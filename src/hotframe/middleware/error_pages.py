"""
Error page middleware.

Catches unhandled exceptions and HTTP error status codes, rendering
appropriate HTML error pages or JSON error responses based on the
request's Accept header.
"""

from __future__ import annotations

import logging
import traceback

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

logger = logging.getLogger(__name__)

_ERROR_TITLES: dict[int, str] = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


def _wants_json(request: Request) -> bool:
    """Determine if the client prefers JSON over HTML."""
    accept = request.headers.get("Accept", "")
    # Explicit JSON preference or API path
    if "application/json" in accept:
        return True
    if request.url.path.startswith("/api/"):
        return True
    return False


def _render_error_html(status_code: int, detail: str, tb: str | None = None) -> str:
    """Render a minimal error page."""
    title = _ERROR_TITLES.get(status_code, "Error")
    tb_section = ""
    if tb:
        tb_section = f'<details><summary>Traceback</summary><pre style="font-size:12px;overflow:auto;padding:16px;background:#1a1a2e;color:#e0e0e0;border-radius:8px;">{tb}</pre></details>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{status_code} — {title}</title>
    <style>
        body {{ font-family: system-ui, -apple-system, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; background: #f8fafc; color: #1e293b; }}
        .container {{ text-align: center; max-width: 600px; padding: 2rem; }}
        h1 {{ font-size: 4rem; margin: 0; font-weight: 800; color: #6366f1; }}
        h2 {{ font-size: 1.25rem; margin: 0.5rem 0 1rem; font-weight: 500; color: #475569; }}
        p {{ color: #64748b; line-height: 1.6; }}
        a {{ color: #6366f1; text-decoration: none; font-weight: 500; }}
        a:hover {{ text-decoration: underline; }}
        details {{ margin-top: 2rem; text-align: left; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{status_code}</h1>
        <h2>{title}</h2>
        <p>{detail}</p>
        <p><a href="/">Back to home</a></p>
        {tb_section}
    </div>
</body>
</html>"""


class ErrorPageMiddleware(BaseHTTPMiddleware):
    """Catch exceptions and render error pages or JSON errors."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
            return self._handle_exception(request, exc)

        # Handle error status codes from downstream (e.g., raise HTTPException)
        if response.status_code >= 400:
            # Only intercept if the response body is empty or very small
            # Don't override custom error responses from views
            content_length = response.headers.get("content-length")
            if content_length == "0" or (
                content_length is None and response.status_code in _ERROR_TITLES
            ):
                # Let it pass — views may set their own error responses
                pass

        return response

    def _handle_exception(self, request: Request, exc: Exception) -> Response:
        """Build an error response from an unhandled exception."""
        from hotframe.config.settings import get_settings

        settings = get_settings()
        status_code = getattr(exc, "status_code", 500)
        detail = getattr(exc, "detail", str(exc)) or "An unexpected error occurred"

        if _wants_json(request):
            return JSONResponse(
                status_code=status_code,
                content={"error": detail, "status_code": status_code},
            )

        tb = None
        if settings.DEBUG:
            tb = traceback.format_exc()

        html = _render_error_html(status_code, detail, tb)
        return HTMLResponse(content=html, status_code=status_code)
