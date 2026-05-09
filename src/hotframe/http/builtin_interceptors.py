# SPDX-License-Identifier: Apache-2.0
"""
Built-in, framework-agnostic HTTP interceptors.

Hotframe ships the mechanics; applications configure them. Every
interceptor in this module accepts an ``applies_to`` matcher — the
default ``"*"`` means "every client unless narrowed" — and an ``order``
that decides where it sits in the chain relative to other interceptors.

Provided strategies:

- :class:`RetryInterceptor` — retry on a configurable set of status codes
  with an injectable backoff.
- :class:`CircuitBreakerInterceptor` — open after N consecutive failures
  and short-circuit for ``recovery_seconds`` before probing again.
- :class:`RefreshInterceptor` — on a configurable "auth expired" status
  (default ``401``), invoke a user-supplied async refresh callback and
  retry exactly once.

All three cooperate with the :mod:`hotframe.http.interceptors` chain.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

import httpx

from hotframe.http.interceptors import CallNext, InterceptorBase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backoff helpers
# ---------------------------------------------------------------------------


def exponential_backoff(
    base: float = 0.5,
    cap: float = 8.0,
    jitter: bool = True,
) -> Callable[[int], float]:
    """Return a function computing ``min(cap, base * 2**attempt)`` seconds.

    Args:
        base: Seconds for the very first retry (attempt index ``0``).
        cap: Upper bound — no single sleep exceeds this.
        jitter: When ``True`` (default) multiply each delay by a random
            factor in ``[0.5, 1.0]`` to avoid thundering-herd retries.
    """

    def compute(attempt: int) -> float:
        delay = min(cap, base * (2 ** max(0, attempt)))
        if jitter:
            delay *= 0.5 + random.random() * 0.5
        return delay

    return compute


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class RetryInterceptor(InterceptorBase):
    """Retry a request on configurable status codes.

    The interceptor re-dispatches the same ``httpx.Request`` up to
    ``max_attempts`` times total (initial attempt + retries). It sleeps
    between attempts according to ``backoff(attempt_index)``.

    Args:
        on_status: Status codes that trigger a retry (e.g. ``[502, 503, 504]``).
        max_attempts: Total attempts including the first one. Must be ≥ 1.
        backoff: Callable ``(attempt) -> seconds``. Defaults to no sleep.
        applies_to: Client-name matcher (see :class:`Interceptor`).
        order: Chain position — defaults to ``200`` so retries sit *inside*
            the circuit breaker (which defaults to ``100``).
        name: Override the default interceptor name.
    """

    def __init__(
        self,
        on_status: list[int],
        max_attempts: int = 3,
        backoff: Callable[[int], float] | None = None,
        applies_to: str | list[str] | Callable[[str], bool] = "*",
        order: int = 200,
        name: str = "retry",
    ) -> None:
        if max_attempts < 1:
            raise ValueError("RetryInterceptor.max_attempts must be >= 1")
        if not on_status:
            raise ValueError("RetryInterceptor.on_status must not be empty")
        self.name = name
        self.applies_to = applies_to
        self.order = order
        self._on_status = frozenset(on_status)
        self._max_attempts = max_attempts
        self._backoff = backoff or (lambda _attempt: 0.0)

    async def intercept(self, request: httpx.Request, call_next: CallNext) -> httpx.Response:
        last_response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            response = await call_next(request)
            if response.status_code not in self._on_status:
                return response
            last_response = response
            # No sleep after the final attempt — we're about to return.
            if attempt < self._max_attempts - 1:
                delay = self._backoff(attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
        # Exhausted retries — hand back whatever the last attempt got.
        assert last_response is not None  # loop runs at least once
        return last_response


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitBreakerInterceptor(InterceptorBase):
    """Open the circuit after N consecutive failures; short-circuit while open.

    States:

    - ``closed`` — requests pass through normally; failures increment a
      counter.
    - ``open`` — requests are rejected immediately with an ``httpx``
      exception; after ``recovery_seconds`` the breaker moves to
      ``half_open``.
    - ``half_open`` — the next request is allowed through as a probe.
      Success closes the circuit; failure re-opens it.

    A "failure" is either an exception raised by the downstream chain
    or a response whose status is in ``failure_statuses``
    (defaults to all ``5xx``).

    Args:
        threshold: Consecutive failures before the breaker opens.
        recovery_seconds: Minimum seconds to stay open before probing.
        failure_statuses: Status codes considered failures. Defaults to
            ``range(500, 600)``.
        applies_to: Client-name matcher.
        order: Chain position — defaults to ``100`` (outermost among the
            built-ins) so the breaker short-circuits before retries waste
            their budget.
        name: Override the default interceptor name.
    """

    _CLOSED = "closed"
    _OPEN = "open"
    _HALF_OPEN = "half_open"

    def __init__(
        self,
        threshold: int = 5,
        recovery_seconds: float = 30.0,
        failure_statuses: list[int] | None = None,
        applies_to: str | list[str] | Callable[[str], bool] = "*",
        order: int = 100,
        name: str = "circuit_breaker",
    ) -> None:
        if threshold < 1:
            raise ValueError("CircuitBreakerInterceptor.threshold must be >= 1")
        if recovery_seconds <= 0:
            raise ValueError("CircuitBreakerInterceptor.recovery_seconds must be > 0")
        self.name = name
        self.applies_to = applies_to
        self.order = order
        self._threshold = threshold
        self._recovery_seconds = recovery_seconds
        self._failure_statuses = (
            frozenset(failure_statuses)
            if failure_statuses is not None
            else frozenset(range(500, 600))
        )
        self._state = self._CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        """Return the current breaker state: ``closed``/``open``/``half_open``."""
        return self._state

    async def intercept(self, request: httpx.Request, call_next: CallNext) -> httpx.Response:
        async with self._lock:
            now = time.monotonic()
            if self._state == self._OPEN:
                # Still cooling down?
                if self._opened_at is not None and now - self._opened_at < self._recovery_seconds:
                    raise httpx.ConnectError(
                        f"Circuit breaker {self.name!r} is open — short-circuiting "
                        f"({self._failures} consecutive failures)",
                        request=request,
                    )
                # Cool-down elapsed — allow a single probe.
                self._state = self._HALF_OPEN

        try:
            response = await call_next(request)
        except Exception:
            await self._record_failure()
            raise

        if response.status_code in self._failure_statuses:
            await self._record_failure()
        else:
            await self._record_success()
        return response

    async def _record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state == self._HALF_OPEN:
                # Probe failed — re-open immediately.
                self._state = self._OPEN
                self._opened_at = time.monotonic()
            elif self._failures >= self._threshold:
                self._state = self._OPEN
                self._opened_at = time.monotonic()

    async def _record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._state = self._CLOSED
            self._opened_at = None


# ---------------------------------------------------------------------------
# Refresh-on-unauthorized
# ---------------------------------------------------------------------------


class RefreshInterceptor(InterceptorBase):
    """Refresh credentials on ``on_status`` and retry the request.

    Invokes the application-provided ``refresh`` callback when the
    downstream chain returns ``on_status`` (default ``401``), then
    re-dispatches the same request. The retry is capped by
    ``max_retries`` (default ``1``) so a persistent ``401`` after a
    refresh does NOT cause an infinite loop.

    The framework is deliberately agnostic about *how* credentials get
    refreshed — OAuth flows, token rotation, SigV4 re-derivation, etc.
    are all valid. The application supplies ``refresh`` as an async
    callable.

    Args:
        refresh: Async callable ``() -> None`` that refreshes whatever
            credentials the client uses. Must mutate shared state (e.g.
            an auth strategy reading from a callable source) so the
            retry picks up the new value.
        on_status: Status code that triggers a refresh. Defaults to
            ``401``.
        max_retries: Maximum retries after refreshing. Defaults to ``1``.
        applies_to: Client-name matcher.
        order: Chain position — defaults to ``150`` so refresh sits
            between the circuit breaker (``100``) and the retry
            (``200``).
        name: Override the default interceptor name.
    """

    def __init__(
        self,
        refresh: Callable[[], Awaitable[None]],
        on_status: int = 401,
        max_retries: int = 1,
        applies_to: str | list[str] | Callable[[str], bool] = "*",
        order: int = 150,
        name: str = "refresh",
    ) -> None:
        if not callable(refresh):
            raise TypeError("RefreshInterceptor.refresh must be an async callable")
        if max_retries < 1:
            raise ValueError("RefreshInterceptor.max_retries must be >= 1")
        self.name = name
        self.applies_to = applies_to
        self.order = order
        self._refresh = refresh
        self._on_status = on_status
        self._max_retries = max_retries

    async def intercept(self, request: httpx.Request, call_next: CallNext) -> httpx.Response:
        response = await call_next(request)
        retries = 0
        while response.status_code == self._on_status and retries < self._max_retries:
            try:
                await self._refresh()
            except Exception:
                logger.exception(
                    "RefreshInterceptor %r: refresh callback failed; returning original response",
                    self.name,
                )
                return response
            retries += 1
            response = await call_next(request)
        return response


__all__ = [
    "CircuitBreakerInterceptor",
    "RefreshInterceptor",
    "RetryInterceptor",
    "exponential_backoff",
]
