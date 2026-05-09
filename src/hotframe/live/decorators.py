# SPDX-License-Identifier: Apache-2.0
"""
``@event`` decorator — mark a coroutine as a LiveComponent event handler.

Decorating a method records the wire name on the function itself so
that :class:`LiveComponent`'s ``__init_subclass__`` hook can walk the
class body once at import time and build a fast ``{name: bound_handler}``
lookup table on the subclass. We never scan attributes per-event.

The wire name is what the browser sends in ``data-on:click="<name>:..."``
(see ``protocol.py``). It is independent of the Python method name so
Python identifiers don't leak into the protocol.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

# Sentinel attribute used to recognise decorated methods. We could use
# functools.wraps but the function returned by ``event`` IS the original
# coroutine — we only stamp it. No wrapping, no overhead.
_EVENT_NAME_ATTR = "__hf_live_event__"

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def event(name: str) -> Callable[[F], F]:
    """Decorator: mark a coroutine method as an event handler.

    Args:
        name: Wire name. The browser sends ``data-on:click="<name>:..."``;
            the runtime looks up this exact string on the component
            instance.

    Returns:
        The same coroutine, with ``__hf_live_event__`` set on it.

    Example::

        class TodoList(LiveComponent):
            @event("toggle")
            async def toggle(self, todo_id: str) -> None:
                ...
    """
    if not isinstance(name, str) or not name:
        raise ValueError("@event(name) requires a non-empty string")

    def decorate(fn: F) -> F:
        # Bare attr stamp. The function remains a normal coroutine.
        setattr(fn, _EVENT_NAME_ATTR, name)
        return fn

    return decorate


def get_event_name(fn: Callable[..., Any]) -> str | None:
    """Return the wire name stamped by :func:`event`, or None.

    Public so :class:`LiveComponent` can introspect at subclass creation
    time without importing the private sentinel.
    """
    return getattr(fn, _EVENT_NAME_ATTR, None)
