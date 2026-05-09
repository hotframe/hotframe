"""
config — runtime settings, database engine, and filesystem paths.

Centralises all configuration concerns: ``HotframeSettings`` (pydantic-settings
model loaded from env vars), ``get_settings()`` (cached singleton getter),
``get_engine()`` / ``get_session_factory()`` for the async SQLAlchemy engine,
and ``dispose_engine()`` for graceful shutdown. Filesystem paths (modules
directory, static root, etc.) live in ``paths.py``.

Key exports::

    from hotframe.config.settings import HotframeSettings, get_settings
    from hotframe.config.database import get_engine, get_session_factory, dispose_engine

Usage::

    settings = get_settings()
    engine = get_engine()          # AsyncEngine bound to settings.DATABASE_URL
    await dispose_engine()         # call on application shutdown
"""
