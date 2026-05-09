# SPDX-License-Identifier: Apache-2.0
"""
Global rate limiting middleware.

Auth route prefixes are configurable via ``settings.RATE_LIMIT_AUTH_PREFIXES``.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

DEFAULT_API_RATE = 120
DEFAULT_VIEW_RATE = 300
DEFAULT_AUTH_RATE = 60
DEFAULT_WINDOW = 60


class _SlidingWindow:
    """In-memory sliding window rate counter."""

    __slots__ = ("_requests",)

    def __init__(self) -> None:
        self._requests: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str, limit: int, window: int) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - window

        timestamps = self._requests[key]
        self._requests[key] = timestamps = [t for t in timestamps if t > cutoff]

        if len(timestamps) >= limit:
            return False, 0

        timestamps.append(now)
        remaining = limit - len(timestamps)
        return True, remaining

    def cleanup(self, max_age: float = 300.0) -> None:
        now = time.monotonic()
        empty_keys = [k for k, v in self._requests.items() if not v or v[-1] < now - max_age]
        for k in empty_keys:
            del self._requests[k]


_window = _SlidingWindow()
_last_cleanup = time.monotonic()


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class APIRateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limit requests by client IP using a sliding window."""

    def __init__(
        self,
        app: Any,
        api_rate: int = DEFAULT_API_RATE,
        view_rate: int = DEFAULT_VIEW_RATE,
        auth_rate: int = DEFAULT_AUTH_RATE,
        window: int = DEFAULT_WINDOW,
        auth_prefixes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(app)
        self._api_rate = api_rate
        self._view_rate = view_rate
        self._auth_rate = auth_rate
        self._window = window
        self._auth_prefixes = auth_prefixes

    def _get_rate_config(self, path: str) -> tuple[str, int] | None:
        if path.startswith("/api/"):
            return "api", self._api_rate
        if path.startswith("/m/"):
            return "view", self._view_rate
        if self._auth_prefixes and any(path.startswith(prefix) for prefix in self._auth_prefixes):
            return "auth", self._auth_rate
        return None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        config = self._get_rate_config(request.url.path)
        if config is None:
            return await call_next(request)

        bucket_prefix, rate = config

        global _last_cleanup
        now = time.monotonic()
        if now - _last_cleanup > 60:
            _window.cleanup()
            _last_cleanup = now

        client_ip = _get_client_ip(request)
        bucket_key = f"{bucket_prefix}:{client_ip}"
        allowed, remaining = _window.is_allowed(bucket_key, rate, self._window)

        if not allowed:
            logger.warning("Rate limit exceeded for %s on %s", client_ip, request.url.path)
            return JSONResponse(
                {"error": "Too many requests", "retry_after": self._window},
                status_code=429,
                headers={
                    "Retry-After": str(self._window),
                    "X-RateLimit-Limit": str(rate),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(rate)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
