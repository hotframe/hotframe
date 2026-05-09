"""
Transaction utilities for async SQLAlchemy sessions.

Usage:
    async with atomic(session):
        session.add(obj)
        on_commit(session, lambda: send_notification(obj.id))
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hotframe.db.protocols import ISession

# Storage for on_commit callbacks, keyed by session id
_commit_callbacks: dict[int, list[Callable[[], Any | Awaitable[Any]]]] = {}


@asynccontextmanager
async def atomic(session: ISession):
    """
    Async context manager for transactional blocks.

    If the session is already inside a transaction, uses a SAVEPOINT
    (begin_nested). Otherwise, starts a new transaction via begin().
    On exception, the transaction/savepoint is rolled back.
    After successful commit, any registered on_commit callbacks are fired.
    """
    sid = id(session)
    is_nested = session.in_transaction()

    if is_nested:
        async with session.begin_nested():
            yield session
    else:
        async with session.begin():
            yield session

        # Fire on_commit callbacks only after outermost transaction commits
        callbacks = _commit_callbacks.pop(sid, [])
        for cb in callbacks:
            result = cb()
            if hasattr(result, "__await__"):
                await result


def on_commit(session: ISession, callback: Callable[[], Any | Awaitable[Any]]) -> None:
    """
    Register a callback to run after the outermost transaction commits.

    Callbacks are executed in registration order. If the transaction
    rolls back, callbacks are discarded. Supports both sync and async callables.
    """
    sid = id(session)
    if sid not in _commit_callbacks:
        _commit_callbacks[sid] = []
    _commit_callbacks[sid].append(callback)
