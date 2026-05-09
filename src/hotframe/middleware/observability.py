# SPDX-License-Identifier: Apache-2.0
"""
Per-request observability middleware.

Reads the request id produced by ``asgi_correlation_id.CorrelationIdMiddleware``
(via its contextvar), binds it together with hub/user identifiers into the
hotframe observability context, and records a request-duration histogram.

The X-Request-ID header lifecycle (generate, propagate, echo) is owned by
``asgi-correlation-id``. This middleware only adds the bits Starlette's
correlation-id stack does not provide: the OpenTelemetry histogram and the
hub/user context binding.
"""

from __future__ import annotations

import time

from asgi_correlation_id.context import correlation_id
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from hotframe.utils.observability_context import bind_context, update_context
from hotframe.utils.observability_metrics import get_request_duration_histogram


class RequestObservabilityMiddleware(BaseHTTPMiddleware):
    """Bind observability context and record request duration."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = correlation_id.get() or ""
        hub_id = str(getattr(request.state, "hub_id", "") or "")
        user_id = str(getattr(request.state, "user_id", "") or "")

        with bind_context(request_id=request_id, hub_id=hub_id, user_id=user_id):
            start = time.perf_counter()
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start) * 1000

            route = request.scope.get("path", request.url.path)
            get_request_duration_histogram().record(
                duration_ms,
                attributes={
                    "http.method": request.method,
                    "http.route": route,
                    "http.status_code": response.status_code,
                },
            )

        return response


def bind_user_context(user_id: str, hub_id: str = "") -> None:
    """Update observability context with user/hub info post-authentication."""
    kwargs: dict[str, str] = {}
    if user_id:
        kwargs["user_id"] = user_id
    if hub_id:
        kwargs["hub_id"] = hub_id
    if kwargs:
        update_context(**kwargs)
