"""
Request body size limit middleware.

Rejects requests with Content-Length exceeding the configured maximum.
Returns 413 Payload Too Large. Prevents DoS via oversized payloads.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# 10 MB default
DEFAULT_MAX_BODY = 10 * 1024 * 1024


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests with body larger than max_bytes."""

    def __init__(self, app: Any, max_bytes: int = DEFAULT_MAX_BODY) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_bytes:
                    logger.warning(
                        "Request body too large: %s bytes (max %s) from %s",
                        content_length,
                        self._max_bytes,
                        request.client.host if request.client else "unknown",
                    )
                    return JSONResponse(
                        {"detail": "Request body too large"},
                        status_code=413,
                    )
            except (ValueError, TypeError):
                pass

        return await call_next(request)
