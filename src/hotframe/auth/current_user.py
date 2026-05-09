# SPDX-License-Identifier: Apache-2.0
"""
FastAPI dependency injection providers.

Centralizes common dependencies: database sessions, authenticated user,
and core registries. The user model is resolved from ``settings.AUTH_USER_MODEL``.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select

from hotframe.auth.auth import get_session_user_id
from hotframe.db.protocols import ISession

if TYPE_CHECKING:
    from hotframe.signals.dispatcher import AsyncEventBus
    from hotframe.signals.hooks import HookRegistry
    from hotframe.templating.slots import SlotRegistry


def _get_session_factory():
    from hotframe.config.database import get_session_factory

    return get_session_factory()


def _resolve_user_model() -> type[Any] | None:
    """Import the user model class from settings.AUTH_USER_MODEL.

    Returns None if AUTH_USER_MODEL is not configured.

    Returns ``type[Any]`` because the swappable user class is a SQLAlchemy
    declarative whose columns (``id``, ``is_active``) are descriptors that
    cannot be statically typed without losing the descriptor magic. The
    runtime contract — that the model exposes id/is_active — is documented
    and enforced by the queries that consume it.
    """
    from hotframe.config.settings import get_settings

    settings = get_settings()
    if not settings.AUTH_USER_MODEL:
        return None

    module_path, class_name = settings.AUTH_USER_MODEL.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


# ---------------------------------------------------------------------------
# Database session
# ---------------------------------------------------------------------------


async def get_db() -> AsyncGenerator[ISession, None]:
    """
    FastAPI dependency that yields an async database session.
    """
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbSession = Annotated[ISession, Depends(get_db)]
"""Annotated type alias for injecting an async DB session."""


# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------


async def get_current_user(
    request: Request,
    db: DbSession,
) -> Any:
    """
    Resolve the authenticated user from the session.

    Uses ``settings.AUTH_USER_MODEL`` to determine which model to query.
    Sets ``request.state.user_permissions`` for downstream permission checks.

    Raises:
        HTTPException 401: If no user is authenticated or user not found.
        HTTPException 500: If AUTH_USER_MODEL is not configured.
    """
    user_id = get_session_user_id(request)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    UserModel = _resolve_user_model()
    if UserModel is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AUTH_USER_MODEL not configured in settings",
        )

    result = await db.execute(
        select(UserModel).where(
            UserModel.id == user_id,
            UserModel.is_active.is_(True),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or deactivated",
        )

    # Resolve permissions if the user model supports it
    perms: list[str] = getattr(request.state, "user_permissions", None) or []
    if not perms:
        if getattr(user, "is_admin", False):
            perms = ["*"]
        elif hasattr(user, "get_permissions"):
            perms = await user.get_permissions() if callable(user.get_permissions) else []
        elif hasattr(user, "role") and hasattr(getattr(user, "role", None), "permissions"):
            role = user.role
            perms = [rp.permission_pattern for rp in role.permissions]

    request.state.user_permissions = perms
    request.state.current_user = user

    return user


async def get_current_user_optional(
    request: Request,
    db: DbSession,
) -> Any | None:
    """
    Same as ``get_current_user`` but returns None if not authenticated.
    """
    user_id = get_session_user_id(request)
    if user_id is None:
        return None

    UserModel = _resolve_user_model()
    if UserModel is None:
        return None

    result = await db.execute(
        select(UserModel).where(
            UserModel.id == user_id,
            UserModel.is_active.is_(True),
        )
    )
    user = result.scalar_one_or_none()

    if user is not None:
        request.state.user_permissions = getattr(user, "permissions", []) or []
        request.state.current_user = user

    return user


# Annotated types for route signatures
CurrentUser = Annotated[Any, Depends(get_current_user)]
OptionalUser = Annotated[Any | None, Depends(get_current_user_optional)]


# ---------------------------------------------------------------------------
# Core registries (from app.state)
# ---------------------------------------------------------------------------


def get_event_bus(request: Request) -> AsyncEventBus:
    """Get the AsyncEventBus from app state."""
    bus = getattr(request.app.state, "event_bus", None)
    if bus is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Event bus not initialized",
        )
    return bus


def get_hooks(request: Request) -> HookRegistry:
    """Get the HookRegistry from app state."""
    hooks = getattr(request.app.state, "hooks", None)
    if hooks is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Hook registry not initialized",
        )
    return hooks


def get_slots(request: Request) -> SlotRegistry:
    """Get the SlotRegistry from app state."""
    slots = getattr(request.app.state, "slots", None)
    if slots is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Slot registry not initialized",
        )
    return slots


EventBus = Annotated["AsyncEventBus", Depends(get_event_bus)]
Hooks = Annotated["HookRegistry", Depends(get_hooks)]
Slots = Annotated["SlotRegistry", Depends(get_slots)]
