"""
SQLAlchemy async engine and session factory.

Provides a singleton engine, session factory, and FastAPI dependency.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hotframe.config.settings import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the singleton async engine, creating it on first call."""
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict = {
            "echo": settings.DB_ECHO,
        }
        if not settings.is_sqlite:
            kwargs.update(
                pool_size=settings.DB_POOL_SIZE,
                max_overflow=settings.DB_MAX_OVERFLOW,
                pool_recycle=settings.DB_POOL_RECYCLE,
                pool_pre_ping=True,
                pool_timeout=settings.DB_POOL_TIMEOUT,
            )
            # Transaction-mode poolers (RDS Proxy, PgBouncer, Supavisor,
            # Cloud SQL Auth Proxy with pool) rotate the backend
            # connection between transactions, which invalidates any
            # prepared statement asyncpg caches on the client side.
            # Opt in via DB_DISABLE_PREPARED_STATEMENTS=true.
            if settings.DB_DISABLE_PREPARED_STATEMENTS and "asyncpg" in settings.DATABASE_URL:
                kwargs["connect_args"] = {
                    "prepared_statement_cache_size": 0,
                    "statement_cache_size": 0,
                }
        else:
            # SQLite doesn't support pool_size / max_overflow
            kwargs["connect_args"] = {"check_same_thread": False}

        _engine = create_async_engine(settings.DATABASE_URL, **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the singleton session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async session and handles cleanup."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose the engine on application shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
