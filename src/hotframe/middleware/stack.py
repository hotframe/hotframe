# SPDX-License-Identifier: Apache-2.0
"""
Middleware stack builder.

Reads ``settings.MIDDLEWARE`` list and instantiates each middleware class.
Like Django's MIDDLEWARE setting — order matters (first = outermost).
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from hotframe.config.settings import HotframeSettings

logger = logging.getLogger(__name__)


def _import_class(dotted_path: str) -> type:
    """Import a class from a dotted path like 'hotframe.auth.csrf.CSRFMiddleware'."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


def _get_middleware_kwargs(cls: type, settings: HotframeSettings) -> dict[str, Any]:
    """Return constructor kwargs for known middleware classes based on settings."""
    from starlette.middleware.sessions import SessionMiddleware

    from hotframe.middleware.body_limit import BodyLimitMiddleware
    from hotframe.middleware.csp import CSPMiddleware
    from hotframe.middleware.module_middleware import ModuleMiddlewareManager
    from hotframe.middleware.rate_limit import APIRateLimitMiddleware
    from hotframe.middleware.session_safe import RobustSessionMiddleware
    from hotframe.middleware.timeout import TimeoutMiddleware

    if cls is SessionMiddleware or cls is RobustSessionMiddleware:
        return {
            "secret_key": settings.SECRET_KEY,
            "max_age": settings.SESSION_MAX_AGE,
            "session_cookie": settings.SESSION_COOKIE_NAME,
            "same_site": "strict",
            "https_only": settings.DEPLOYMENT_MODE != "local",
        }
    if cls is CSPMiddleware:
        return {"enforce": settings.CSP_ENFORCE}
    if cls is APIRateLimitMiddleware:
        auth_rate = 10000 if settings.DEBUG else settings.RATE_LIMIT_AUTH
        return {
            "api_rate": settings.RATE_LIMIT_API,
            "auth_rate": auth_rate,
            "window": 60,
            "auth_prefixes": tuple(settings.RATE_LIMIT_AUTH_PREFIXES),
        }
    if cls is BodyLimitMiddleware:
        return {"max_bytes": settings.MAX_REQUEST_BODY}
    if cls is TimeoutMiddleware:
        return {"timeout": 30}
    if cls is ModuleMiddlewareManager:
        return {"registry": None}
    return {}


def build_middleware_stack(app: FastAPI, settings: HotframeSettings) -> None:
    """
    Add all middleware from settings.MIDDLEWARE in correct order.

    Starlette convention: last added = outermost = runs first on request.
    The MIDDLEWARE list is in execution order (outermost first), so we
    add them in reverse.
    """
    for dotted_path in reversed(settings.MIDDLEWARE):
        try:
            cls = _import_class(dotted_path)
            kwargs = _get_middleware_kwargs(cls, settings)
            # Settings give us middleware as a dotted import path; the resolved
            # ``cls`` is dynamic and Starlette's _MiddlewareFactory typing
            # cannot describe it. Runtime validation lives in add_middleware.
            app.add_middleware(cls, **kwargs)  # type: ignore[arg-type]
        except Exception:
            logger.exception("Failed to add middleware: %s", dotted_path)
            raise
