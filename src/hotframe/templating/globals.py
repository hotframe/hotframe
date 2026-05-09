# SPDX-License-Identifier: Apache-2.0
"""
Global template context builder.

The ``get_global_context`` coroutine is called by the ``@view``
decorator before every template render. Application-specific context
can be added via ``settings.GLOBAL_CONTEXT_HOOK``.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from markupsafe import Markup
from starlette.requests import Request

logger = logging.getLogger(__name__)


async def get_global_context(request: Request) -> dict[str, Any]:
    """Build the global context dict for a template render."""
    csrf_token = getattr(request.state, "csrf_token", "")
    _csrf_markup = (
        Markup(f'<input type="hidden" name="csrf_token" value="{csrf_token}">')
        if csrf_token
        else Markup("")
    )

    from hotframe.config.settings import get_settings as _get_settings

    context: dict[str, Any] = {
        "request": request,
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
        "csp_trusted_types": _get_settings().CSP_TRUSTED_TYPES,
        "csrf_token": csrf_token,
        "csrf_input": lambda: _csrf_markup,
        "debug": getattr(request.app.state, "debug", False),
        "current_path": request.url.path,
    }

    # --- Authentication ---
    user = getattr(request.state, "current_user", None)

    if user is None:
        user = await _load_user_from_session(request)

    if user:
        context["user"] = user
        context["is_authenticated"] = True
    else:
        context["is_authenticated"] = False

    # --- Module sidebar menu ---
    registry = getattr(request.app.state, "module_registry", None)
    if registry:
        context["module_menu_items"] = registry.get_menu_items()
    else:
        context["module_menu_items"] = []

    # --- Application-specific context hook ---
    from hotframe.config.settings import get_settings

    settings = get_settings()
    if settings.GLOBAL_CONTEXT_HOOK:
        try:
            module_path, func_name = settings.GLOBAL_CONTEXT_HOOK.rsplit(".", 1)
            mod = importlib.import_module(module_path)
            hook = getattr(mod, func_name)
            extra = await hook(request)
            if isinstance(extra, dict):
                context.update(extra)
        except Exception:
            logger.exception("Error in GLOBAL_CONTEXT_HOOK")

    return context


async def _load_user_from_session(request: Request) -> Any | None:
    """Load the authenticated user from the DB using the session user_id."""
    from hotframe.auth.auth import get_session_user_id

    user_id = get_session_user_id(request)
    if user_id is None:
        return None

    try:
        from sqlalchemy import select

        from hotframe.auth.current_user import _resolve_user_model
        from hotframe.config.database import get_session_factory

        UserModel = _resolve_user_model()
        if UserModel is None:
            return None

        factory = get_session_factory()
        async with factory() as db:
            result = await db.execute(
                select(UserModel).where(
                    UserModel.id == user_id,
                    UserModel.is_active.is_(True),
                )
            )
            user = result.scalar_one_or_none()

        if user is not None:
            request.state.current_user = user
        return user
    except Exception:
        logger.exception("Failed to load user from session")
        return None
