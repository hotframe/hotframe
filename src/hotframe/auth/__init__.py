"""
auth — authentication, session management, and FastAPI dependencies.

Provides the full authentication flow for Hub: session-based login
(``create_session``, ``destroy_session``), bcrypt password and PIN
hashing (``hash_password``, ``verify_password``, ``hash_pin``),
JWT helpers, CSRF/CSP guards, and per-request FastAPI dependency
injectors (``get_current_user``, ``get_db``, ``get_hub_id``).

Key exports::

    from hotframe.auth.current_user import get_current_user, get_db, get_hub_id
    from hotframe.auth.auth import create_session, destroy_session, hash_password

Usage::

    @router.get("/dashboard")
    async def dashboard(user=Depends(get_current_user), db=Depends(get_db)):
        ...
"""
