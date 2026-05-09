# SPDX-License-Identifier: Apache-2.0
"""
Shared pytest fixtures and test utilities for hotframe applications.

Usage in your project's conftest.py::

    from hotframe.testing import create_test_app, test_db_session

    @pytest.fixture
    async def app():
        return create_test_app()

    @pytest.fixture
    async def db():
        async for session in test_db_session():
            yield session
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hotframe.models.base import Base

_test_engine = None
_test_session_factory = None


def create_test_app(settings: Any | None = None, **overrides: Any) -> FastAPI:
    """Create a FastAPI application configured for testing.

    Uses SQLite in-memory by default. Disables middleware that interferes
    with testing (CSRF, rate limiting).

    Args:
        settings: Optional settings instance. If None, creates one with
                  test-friendly defaults.
        **overrides: Override specific settings fields.

    Returns:
        A configured FastAPI test application.
    """
    from hotframe.config.settings import HotframeSettings, set_settings

    if settings is None:
        test_defaults: dict[str, Any] = {
            "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "DEBUG": True,
            "DEPLOYMENT_MODE": "local",
            "SECRET_KEY": "test-secret-key-not-for-production",
            "CSRF_EXEMPT_PREFIXES": ["/"],  # Exempt all routes in tests
            "RATE_LIMIT_API": 999999,
            "RATE_LIMIT_AUTH": 999999,
            "LOG_LEVEL": "WARNING",
        }
        test_defaults.update(overrides)
        settings = HotframeSettings(**test_defaults)

    set_settings(settings)

    from hotframe.bootstrap import create_app

    app = create_app(settings)
    return app


async def create_test_tables() -> None:
    """Create all SQLAlchemy tables in the test database."""
    global _test_engine
    if _test_engine is None:
        _test_engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False,
        )
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_test_tables() -> None:
    """Drop all SQLAlchemy tables in the test database."""
    global _test_engine
    if _test_engine is not None:
        async with _test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


async def test_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a test database session with in-memory SQLite.

    Creates tables on first use. Each session is rolled back after use.
    """
    global _test_engine, _test_session_factory

    if _test_engine is None:
        _test_engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False,
        )
        async with _test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    if _test_session_factory is None:
        _test_session_factory = async_sessionmaker(
            _test_engine,
            expire_on_commit=False,
        )

    async with _test_session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


async def cleanup_test_db() -> None:
    """Dispose the test engine. Call in session-scoped teardown."""
    global _test_engine, _test_session_factory
    if _test_engine is not None:
        await _test_engine.dispose()
        _test_engine = None
        _test_session_factory = None


class FakeEventBus:
    """In-memory event bus for testing.

    Records all emitted events for assertion::

        bus = FakeEventBus()
        await bus.emit("test.event", {"key": "value"})
        assert bus.events == [("test.event", {"key": "value"})]
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.typed_events: list[Any] = []

    async def emit(self, event_name: str, data: Any = None, **kwargs: Any) -> None:
        """Record an emitted event by name and data."""
        self.events.append((event_name, data))

    async def emit_typed(self, event: Any) -> None:
        """Record a typed event object."""
        self.typed_events.append(event)

    def reset(self) -> None:
        """Clear all recorded events."""
        self.events.clear()
        self.typed_events.clear()


class FakeHookRegistry:
    """In-memory hook registry for testing."""

    def __init__(self) -> None:
        self._actions: dict[str, list] = {}
        self._filters: dict[str, list] = {}

    async def do_action(self, name: str, *args: Any, **kwargs: Any) -> None:
        """Invoke all registered action handlers for the given hook name."""
        for fn in self._actions.get(name, []):
            await fn(*args, **kwargs)

    async def apply_filters(self, name: str, value: Any, *args: Any, **kwargs: Any) -> Any:
        """Pass value through all registered filter handlers for the given hook name."""
        for fn in self._filters.get(name, []):
            value = await fn(value, *args, **kwargs)
        return value

    def add_action(self, name: str, fn: Any, priority: int = 10) -> None:
        """Register an action handler for the given hook name."""
        self._actions.setdefault(name, []).append(fn)

    def add_filter(self, name: str, fn: Any, priority: int = 10) -> None:
        """Register a filter handler for the given hook name."""
        self._filters.setdefault(name, []).append(fn)
