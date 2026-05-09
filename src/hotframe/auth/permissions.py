"""
Permission checking system.

Supports wildcard permissions (``*``, ``inventory.*``) and provides
FastAPI dependency factories for route-level permission enforcement.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException, status
from starlette.requests import Request

if TYPE_CHECKING:
    pass


def has_permission(user_permissions: list[str], required: str) -> bool:
    """
    Check if a user's permission set grants the required permission.

    Supports wildcard matching:
    - ``*`` matches everything
    - ``inventory.*`` matches ``inventory.view_product``, ``inventory.edit_product``
    - Exact match: ``pos.open_register`` matches ``pos.open_register``

    Args:
        user_permissions: List of permission strings the user holds.
        required: The permission string to check for.

    Returns:
        True if any user permission grants the required permission.
    """
    for perm in user_permissions:
        if perm == "*":
            return True
        if perm == required:
            return True
        if fnmatch(required, perm):
            return True
    return False


def require_permission(*perms: str, any_perm: bool = False) -> Any:
    """
    FastAPI dependency factory for permission checking.

    Args:
        *perms: One or more required permission strings.
        any_perm: If True, user needs ANY of the permissions.
                  If False (default), user needs ALL of them.

    Returns:
        A FastAPI ``Depends`` dependency.

    Usage::

        @router.get("/products", dependencies=[Depends(require_permission("inventory.view_product"))])
        async def list_products(): ...

        @router.post("/admin/reset", dependencies=[Depends(require_permission("admin.manage", "admin.reset", any_perm=True))])
        async def admin_reset(): ...
    """

    async def _check_permissions(request: Request) -> None:
        # Import here to avoid circular dependency
        from hotframe.auth.auth import get_session_user_id

        user_id = get_session_user_id(request)
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authentication required",
            )

        # User permissions are loaded by get_current_user dependency and stored on request
        user_permissions: list[str] = getattr(request.state, "user_permissions", [])

        if any_perm:
            granted = any(has_permission(user_permissions, p) for p in perms)
        else:
            granted = all(has_permission(user_permissions, p) for p in perms)

        if not granted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission(s): {', '.join(perms)}",
            )

    return Depends(_check_permissions)


async def _require_admin(request: Request) -> None:
    """Dependency that requires the user to be an admin."""
    from hotframe.auth.auth import get_session_user_id

    user_id = get_session_user_id(request)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authentication required",
        )

    user_permissions: list[str] = getattr(request.state, "user_permissions", [])
    if not has_permission(user_permissions, "*"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


RequireAdmin = Depends(_require_admin)
"""FastAPI dependency that requires admin (wildcard ``*``) permissions."""
