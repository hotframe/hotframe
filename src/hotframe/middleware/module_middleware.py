"""
Module middleware manager.

Delegates to middleware registered by active modules. Each module can
define a middleware class with ``process_request()`` and ``process_response()``
methods. The manager caches the middleware list from the module registry
to avoid any filesystem I/O on each request.

Module middleware interface::

    class MyModuleMiddleware:
        async def process_request(self, request: Request) -> Response | None:
            '''Return a Response to short-circuit, or None to continue.'''
            return None

        async def process_response(self, request: Request, response: Response) -> Response:
            '''Optionally modify the response.'''
            return response
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@runtime_checkable
class ModuleMiddlewareProtocol(Protocol):
    """Protocol that module middleware classes must satisfy."""

    async def process_request(self, request: Request) -> Response | None: ...
    async def process_response(self, request: Request, response: Response) -> Response: ...


class ModuleMiddlewareManager(BaseHTTPMiddleware):
    """
    Run middleware contributed by active modules.

    The ``registry`` attribute is set after module runtime boots.
    Until then, this middleware is a transparent passthrough.
    """

    def __init__(self, app: Any, registry: Any | None = None) -> None:
        super().__init__(app)
        self.registry = registry
        self._cached_middleware: list[ModuleMiddlewareProtocol] | None = None
        self._cache_version: int = -1

    def _get_middleware_list(self) -> list[ModuleMiddlewareProtocol]:
        """Get middleware list from registry, using cache when possible."""
        if self.registry is None:
            return []

        # Check if registry has changed (version bump on module load/unload)
        current_version = getattr(self.registry, "version", 0)
        if self._cached_middleware is not None and self._cache_version == current_version:
            return self._cached_middleware

        # Rebuild cache
        middleware_list: list[ModuleMiddlewareProtocol] = []
        get_middleware = getattr(self.registry, "get_all_middleware", None)
        if get_middleware is not None:
            try:
                raw = get_middleware()
                for mw in raw:
                    if isinstance(mw, ModuleMiddlewareProtocol):
                        middleware_list.append(mw)
                    else:
                        logger.warning(
                            "Module middleware %r does not satisfy ModuleMiddlewareProtocol — skipping",
                            type(mw).__name__,
                        )
            except Exception:
                logger.exception("Error loading module middleware from registry")

        self._cached_middleware = middleware_list
        self._cache_version = current_version
        return middleware_list

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        middleware_list = self._get_middleware_list()

        if not middleware_list:
            return await call_next(request)

        # Process request phase (in order)
        for mw in middleware_list:
            try:
                result = await mw.process_request(request)
                if result is not None:
                    # Short-circuit: module middleware returned a response
                    return result
            except Exception:
                logger.exception(
                    "Error in module middleware %r process_request",
                    type(mw).__name__,
                )

        response = await call_next(request)

        # Process response phase (in reverse order)
        for mw in reversed(middleware_list):
            try:
                response = await mw.process_response(request, response)
            except Exception:
                logger.exception(
                    "Error in module middleware %r process_response",
                    type(mw).__name__,
                )

        return response

    def invalidate_cache(self) -> None:
        """Force middleware list to be rebuilt on next request."""
        self._cached_middleware = None
        self._cache_version = -1
