"""
PIN authentication utilities.

Provides bcrypt-based PIN hashing/verification and session management
for the Hub's PIN-based authentication system.
"""

from __future__ import annotations

from uuid import UUID

import bcrypt
from starlette.requests import Request

SESSION_USER_KEY = "user_id"


def hash_password(password: str) -> str:
    """
    Hash a password using bcrypt.

    Args:
        password: The raw password string.

    Returns:
        Bcrypt hash string.
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """
    Verify a password against a bcrypt hash.

    Args:
        password: The raw password string.
        password_hash: The stored bcrypt hash.

    Returns:
        True if the password matches the hash.
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def hash_pin(pin: str) -> str:
    """
    Hash a PIN using bcrypt.

    Args:
        pin: The raw PIN string (typically 4-8 digits).

    Returns:
        Bcrypt hash string.
    """
    return bcrypt.hashpw(pin.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_pin(pin: str, pin_hash: str) -> bool:
    """
    Verify a PIN against a bcrypt hash.

    Args:
        pin: The raw PIN string.
        pin_hash: The stored bcrypt hash.

    Returns:
        True if the PIN matches the hash.
    """
    try:
        return bcrypt.checkpw(pin.encode("utf-8"), pin_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def get_session_user_id(request: Request) -> UUID | None:
    """
    Read the authenticated user ID from the session.

    Returns:
        The user UUID if authenticated, None otherwise.
    """
    session = request.session
    raw = session.get(SESSION_USER_KEY)
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


def create_session(request: Request, user_id: UUID) -> None:
    """
    Store the authenticated user ID in the session.

    Args:
        request: The current request (must have session middleware active).
        user_id: The authenticated user's UUID.
    """
    request.session[SESSION_USER_KEY] = str(user_id)


def destroy_session(request: Request) -> None:
    """
    Clear all session data (logout).

    The session middleware will delete the cookie on response.
    """
    request.session.clear()
