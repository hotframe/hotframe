# SPDX-License-Identifier: Apache-2.0
"""
HTTP interceptors — Angular-style request/response middleware for
:class:`AuthenticatedClient`.

An interceptor wraps the dispatch of a single outgoing request and can:

- inspect or mutate the outgoing ``httpx.Request`` before it hits the
  wire,
- inspect or transform the incoming ``httpx.Response`` before it is
  returned to the caller,
- short-circuit the call (e.g. circuit breaker),
- retry the call (e.g. on 503 or after a token refresh) by awaiting
  ``call_next`` more than once.

Interceptors are chained: each interceptor receives a ``call_next``
callable that dispatches the next interceptor (or, for the last one,
the terminal dispatcher that actually sends the request through
``httpx``).

The framework only defines the primitive. Concrete behavior —
``RetryInterceptor``, ``CircuitBreakerInterceptor``,
``RefreshInterceptor`` — lives in
:mod:`hotframe.http.builtin_interceptors`; application code may provide
its own by conforming to the :class:`Interceptor` protocol.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

import httpx

# Type alias for the "call the next interceptor (or the wire)" callable
# handed to every interceptor. Intentionally unbound from any concrete
# class so tests and apps can build chains without importing hotframe.
CallNext = Callable[[httpx.Request], Awaitable[httpx.Response]]


@runtime_checkable
class Interceptor(Protocol):
    """Protocol every HTTP interceptor must satisfy.

    Attributes:
        name: Unique identifier used for discovery, logging, and
            deduplication.
        applies_to: Client-name matcher. Either a specific client name,
            a list of names, the wildcard ``"*"``, or a callable that
            takes a client name and returns ``True`` when the interceptor
            should apply.
        order: Ordering hint — lower values wrap later-running
            interceptors and therefore run their ``intercept`` body
            earlier on the way in (and later on the way out).
    """

    name: str
    applies_to: str | list[str] | Callable[[str], bool]
    order: int

    async def intercept(self, request: httpx.Request, call_next: CallNext) -> httpx.Response:
        """Wrap the dispatch of ``request``.

        Implementations must eventually ``await call_next(request)`` to
        progress the chain — unless they are deliberately
        short-circuiting (circuit breaker open, cache hit, etc.).
        """
        ...


class InterceptorBase:
    """Optional convenience base class for interceptors.

    Provides a sensible default ``order`` and a reusable matcher for the
    ``applies_to`` attribute. Inheriting from this class is not
    required — any object that satisfies :class:`Interceptor` works.
    """

    name: str = ""
    applies_to: str | list[str] | Callable[[str], bool] = "*"
    order: int = 100

    def applies_to_client(self, client_name: str) -> bool:
        """Return ``True`` when this interceptor should run for ``client_name``.

        Resolves the ``applies_to`` attribute:

        - ``"*"`` → match every client.
        - ``str`` (non-wildcard) → exact client-name match.
        - ``list[str]`` → membership test.
        - ``Callable[[str], bool]`` → delegated decision.
        """
        matcher = self.applies_to
        if callable(matcher):
            return bool(matcher(client_name))
        if isinstance(matcher, str):
            return matcher == "*" or matcher == client_name
        if isinstance(matcher, list | tuple | set):
            return client_name in matcher
        # Unknown matcher shape — fail closed to avoid accidental
        # application to every client.
        return False

    async def intercept(self, request: httpx.Request, call_next: CallNext) -> httpx.Response:
        """Default pass-through: just forward to ``call_next``."""
        return await call_next(request)


def build_chain(
    interceptors: list[Interceptor],
    terminal: CallNext,
) -> CallNext:
    """Compose ``interceptors`` around ``terminal`` and return the head of the chain.

    Interceptors are sorted by their ``order`` attribute ascending so
    that **lower ``order`` values sit on the outside of the chain** —
    their ``intercept`` bodies execute first on the way down and last
    on the way up.

    The returned callable has the same signature as ``terminal`` and
    can be awaited with a single ``httpx.Request`` to drive the full
    pipeline. An empty interceptor list returns ``terminal`` unchanged.

    Args:
        interceptors: Sequence of interceptor instances. Safe to pass an
            empty list.
        terminal: The inner-most callable that actually dispatches the
            request (typically via ``httpx.AsyncClient.send``).
    """
    if not interceptors:
        return terminal

    ordered = sorted(interceptors, key=lambda i: getattr(i, "order", 100))

    # Build the chain from the innermost out so each closure captures
    # the next-link already wired.
    next_call: CallNext = terminal
    for interceptor in reversed(ordered):
        next_call = _wrap(interceptor, next_call)
    return next_call


def _wrap(interceptor: Interceptor, next_call: CallNext) -> CallNext:
    """Bind ``interceptor`` to ``next_call`` and return a new ``CallNext``."""

    async def call(request: httpx.Request) -> httpx.Response:
        return await interceptor.intercept(request, next_call)

    return call


__all__ = [
    "CallNext",
    "Interceptor",
    "InterceptorBase",
    "build_chain",
]
