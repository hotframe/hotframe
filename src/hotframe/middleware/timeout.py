"""
Request timeout middleware.

Cancels any request that exceeds a configurable timeout (default 30s).
Health check endpoints are excluded to avoid false positives.
"""

from __future__ import annotations

import asyncio
import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30  # seconds


class TimeoutMiddleware(BaseHTTPMiddleware):
    """
    Cancel requests that exceed a timeout threshold.

    Should be the outermost middleware so the timeout covers the entire
    request lifecycle including all inner middleware processing.
    """

    def __init__(self, app, timeout: float = DEFAULT_TIMEOUT) -> None:
        super().__init__(app)
        self.timeout = timeout

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip health checks — they must always respond
        if request.url.path in ("/health/", "/health"):
            return await call_next(request)

        try:
            async with asyncio.timeout(self.timeout):
                return await call_next(request)
        except TimeoutError:
            logger.warning(
                "Request timeout after %ss: %s %s",
                self.timeout,
                request.method,
                request.url.path,
            )
            return JSONResponse(
                {"detail": "Request timeout"},
                status_code=504,
            )
