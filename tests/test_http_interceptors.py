"""Tests for the ``hotframe.http`` interceptor subsystem.

Covers:

- The :class:`Interceptor` protocol and :class:`InterceptorBase` helper.
- ``build_chain`` ordering semantics (lower ``order`` wraps outer).
- :class:`RetryInterceptor` retry-count, status-set, backoff plumbing.
- :class:`CircuitBreakerInterceptor` closed → open → half_open → closed
  state machine.
- :class:`RefreshInterceptor` one-shot refresh-and-retry with a capped
  loop on persistent ``401``.
- :class:`AuthenticatedClient` running the full chain with
  ``httpx.MockTransport`` and re-applying auth on every retry.
- :func:`discover_interceptors` filesystem scan, malformed-file safety
  net, deduplication, and order sort.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest

from hotframe.http import (
    AuthenticatedClient,
    BearerAuth,
    CircuitBreakerInterceptor,
    HttpClientRegistry,
    Interceptor,
    InterceptorBase,
    RefreshInterceptor,
    RetryInterceptor,
    discover_interceptors,
    exponential_backoff,
)
from hotframe.http.interceptors import CallNext, build_chain

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _transport_sequence(
    responses: list[httpx.Response],
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Mock transport returning queued responses in order, one per call."""
    queue = list(responses)

    async def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if not queue:
            return httpx.Response(500, json={"detail": "transport exhausted"})
        return queue.pop(0)

    return httpx.MockTransport(handler)


class _RecordingInterceptor(InterceptorBase):
    """Interceptor that records its ordering when the chain executes."""

    def __init__(self, tag: str, trace: list[str], order: int = 100) -> None:
        self.name = f"recording-{tag}"
        self.order = order
        self.applies_to = "*"
        self._tag = tag
        self._trace = trace

    async def intercept(self, request: httpx.Request, call_next: CallNext) -> httpx.Response:
        self._trace.append(f"{self._tag}:before")
        response = await call_next(request)
        self._trace.append(f"{self._tag}:after")
        return response


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestInterceptorProtocol:
    def test_interceptor_base_satisfies_protocol(self):
        class MyInterceptor(InterceptorBase):
            name = "x"
            applies_to = "*"
            order = 100

        instance = MyInterceptor()
        assert isinstance(instance, Interceptor)

    def test_builtin_interceptors_satisfy_protocol(self):
        assert isinstance(RetryInterceptor(on_status=[503]), Interceptor)
        assert isinstance(CircuitBreakerInterceptor(), Interceptor)

        async def _noop() -> None:
            return None

        assert isinstance(RefreshInterceptor(refresh=_noop), Interceptor)

    def test_applies_to_client_matcher(self):
        class Explicit(InterceptorBase):
            name = "a"
            applies_to = "stripe"

        class Wildcard(InterceptorBase):
            name = "b"
            applies_to = "*"

        class ListMatcher(InterceptorBase):
            name = "c"
            applies_to = ["stripe", "twilio"]

        class Callable_(InterceptorBase):
            name = "d"
            applies_to = staticmethod(lambda n: n.startswith("s"))

        assert Explicit().applies_to_client("stripe") is True
        assert Explicit().applies_to_client("twilio") is False
        assert Wildcard().applies_to_client("anything") is True
        assert ListMatcher().applies_to_client("twilio") is True
        assert ListMatcher().applies_to_client("other") is False
        assert Callable_().applies_to_client("stripe") is True
        assert Callable_().applies_to_client("gitlab") is False


# ---------------------------------------------------------------------------
# build_chain
# ---------------------------------------------------------------------------


class TestBuildChain:
    async def test_empty_returns_terminal(self):
        async def terminal(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        chain = build_chain([], terminal)
        assert chain is terminal

    async def test_orders_by_order_ascending(self):
        trace: list[str] = []
        outer = _RecordingInterceptor("outer", trace, order=10)
        inner = _RecordingInterceptor("inner", trace, order=200)
        middle = _RecordingInterceptor("middle", trace, order=100)

        async def terminal(req: httpx.Request) -> httpx.Response:
            trace.append("terminal")
            return httpx.Response(200)

        chain = build_chain([inner, outer, middle], terminal)
        request = httpx.Request("GET", "https://example.com/x")
        response = await chain(request)

        assert response.status_code == 200
        assert trace == [
            "outer:before",
            "middle:before",
            "inner:before",
            "terminal",
            "inner:after",
            "middle:after",
            "outer:after",
        ]


# ---------------------------------------------------------------------------
# RetryInterceptor
# ---------------------------------------------------------------------------


class TestRetryInterceptor:
    async def test_retries_on_configured_status(self):
        calls: list[int] = []

        async def terminal(req: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(503)
            return httpx.Response(200)

        interceptor = RetryInterceptor(on_status=[503], max_attempts=5)
        chain = build_chain([interceptor], terminal)

        response = await chain(httpx.Request("GET", "https://x/"))
        assert response.status_code == 200
        assert len(calls) == 3

    async def test_respects_max_attempts(self):
        calls = 0

        async def terminal(req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        interceptor = RetryInterceptor(on_status=[503], max_attempts=2)
        chain = build_chain([interceptor], terminal)
        response = await chain(httpx.Request("GET", "https://x/"))

        assert response.status_code == 503
        assert calls == 2  # initial + 1 retry

    async def test_backoff_is_called_between_attempts(self):
        delays: list[int] = []

        def backoff(attempt: int) -> float:
            delays.append(attempt)
            return 0.0  # don't actually sleep the test

        calls = 0

        async def terminal(req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        interceptor = RetryInterceptor(on_status=[503], max_attempts=3, backoff=backoff)
        chain = build_chain([interceptor], terminal)
        await chain(httpx.Request("GET", "https://x/"))

        assert calls == 3
        # Backoff called between attempts 0->1 and 1->2 (not after final).
        assert delays == [0, 1]

    async def test_exponential_backoff_bounds(self):
        compute = exponential_backoff(base=1.0, cap=4.0, jitter=False)
        assert compute(0) == 1.0
        assert compute(1) == 2.0
        assert compute(2) == 4.0
        assert compute(10) == 4.0  # capped

    async def test_invalid_config_rejected(self):
        with pytest.raises(ValueError):
            RetryInterceptor(on_status=[], max_attempts=1)
        with pytest.raises(ValueError):
            RetryInterceptor(on_status=[503], max_attempts=0)


# ---------------------------------------------------------------------------
# CircuitBreakerInterceptor
# ---------------------------------------------------------------------------


class TestCircuitBreakerInterceptor:
    async def test_opens_after_threshold_failures(self):
        async def terminal(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        breaker = CircuitBreakerInterceptor(threshold=3, recovery_seconds=60.0)
        chain = build_chain([breaker], terminal)

        for _ in range(3):
            await chain(httpx.Request("GET", "https://x/"))

        assert breaker.state == "open"

        with pytest.raises(httpx.ConnectError):
            await chain(httpx.Request("GET", "https://x/"))

    async def test_half_open_recovers_after_success(self, monkeypatch):
        calls = 0

        async def terminal(req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls <= 2:
                return httpx.Response(500)
            return httpx.Response(200)

        breaker = CircuitBreakerInterceptor(threshold=2, recovery_seconds=0.01)
        chain = build_chain([breaker], terminal)

        await chain(httpx.Request("GET", "https://x/"))
        await chain(httpx.Request("GET", "https://x/"))
        assert breaker.state == "open"

        # Simulate recovery window elapsing.
        import asyncio

        await asyncio.sleep(0.02)

        response = await chain(httpx.Request("GET", "https://x/"))
        assert response.status_code == 200
        assert breaker.state == "closed"

    async def test_half_open_failure_reopens(self):
        async def terminal(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        breaker = CircuitBreakerInterceptor(threshold=1, recovery_seconds=0.01)
        chain = build_chain([breaker], terminal)

        await chain(httpx.Request("GET", "https://x/"))
        assert breaker.state == "open"

        import asyncio

        await asyncio.sleep(0.02)

        with pytest.raises(httpx.ConnectError):
            # First call after cool-down becomes the probe — probe fails
            # with a 500, breaker re-opens, and subsequent calls are
            # short-circuited.
            await chain(httpx.Request("GET", "https://x/"))
            await chain(httpx.Request("GET", "https://x/"))

    async def test_exception_from_inner_counts_as_failure(self):
        async def terminal(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("boom", request=req)

        breaker = CircuitBreakerInterceptor(threshold=2, recovery_seconds=60.0)
        chain = build_chain([breaker], terminal)

        with pytest.raises(httpx.ReadTimeout):
            await chain(httpx.Request("GET", "https://x/"))
        with pytest.raises(httpx.ReadTimeout):
            await chain(httpx.Request("GET", "https://x/"))

        assert breaker.state == "open"


# ---------------------------------------------------------------------------
# RefreshInterceptor
# ---------------------------------------------------------------------------


class TestRefreshInterceptor:
    async def test_refresh_called_on_unauthorized_and_retries_once(self):
        refresh_calls = 0
        attempts = 0

        async def refresh() -> None:
            nonlocal refresh_calls
            refresh_calls += 1

        async def terminal(req: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(401)
            return httpx.Response(200)

        interceptor = RefreshInterceptor(refresh=refresh)
        chain = build_chain([interceptor], terminal)

        response = await chain(httpx.Request("GET", "https://x/"))
        assert response.status_code == 200
        assert refresh_calls == 1
        assert attempts == 2

    async def test_persistent_401_does_not_loop_forever(self):
        refresh_calls = 0
        attempts = 0

        async def refresh() -> None:
            nonlocal refresh_calls
            refresh_calls += 1

        async def terminal(req: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(401)

        interceptor = RefreshInterceptor(refresh=refresh, max_retries=1)
        chain = build_chain([interceptor], terminal)

        response = await chain(httpx.Request("GET", "https://x/"))
        assert response.status_code == 401
        assert refresh_calls == 1
        assert attempts == 2  # initial + 1 retry, then stop

    async def test_refresh_failure_returns_original_response(self):
        async def refresh() -> None:
            raise RuntimeError("refresh blew up")

        async def terminal(req: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        interceptor = RefreshInterceptor(refresh=refresh)
        chain = build_chain([interceptor], terminal)

        response = await chain(httpx.Request("GET", "https://x/"))
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# AuthenticatedClient integration
# ---------------------------------------------------------------------------


class TestAuthenticatedClientWithInterceptors:
    async def test_chain_runs_end_to_end(self):
        trace: list[str] = []
        observed: list[httpx.Request] = []

        class Observer(InterceptorBase):
            name = "observer"
            order = 100
            applies_to = "*"

            async def intercept(self, request, call_next):
                trace.append("in")
                response = await call_next(request)
                trace.append("out")
                return response

        transport = _transport_sequence(
            [httpx.Response(200, json={"ok": True})],
            requests=observed,
        )
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            transport=transport,
            interceptors=[Observer()],
        )
        try:
            response = await client.get("/ping")
        finally:
            await client.aclose()

        assert response.status_code == 200
        assert trace == ["in", "out"]
        assert len(observed) == 1

    async def test_retry_plus_auth_reapplied_each_attempt(self):
        tokens = iter(["old-token", "new-token"])

        def token_source() -> str:
            return next(tokens)

        attempts: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request.headers.get("Authorization", ""))
            if len(attempts) == 1:
                return httpx.Response(503)
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            auth=BearerAuth(token_source),
            transport=transport,
            interceptors=[RetryInterceptor(on_status=[503], max_attempts=2)],
        )
        try:
            response = await client.get("/x")
        finally:
            await client.aclose()

        assert response.status_code == 200
        # Auth applied inside the chain terminal, so each retry re-read
        # the callable source — the second attempt carries the fresh token.
        assert attempts[0] == "Bearer old-token"
        assert attempts[1] == "Bearer new-token"

    async def test_refresh_interceptor_picks_up_new_token(self):
        tokens = ["t1", "t2", "t3"]
        seen: list[str] = []

        def token_source() -> str:
            return tokens[0]

        async def refresh() -> None:
            tokens.pop(0)  # rotate: new current token becomes tokens[0]

        async def handler(request: httpx.Request) -> httpx.Response:
            auth_header = request.headers.get("Authorization", "")
            seen.append(auth_header)
            if auth_header == "Bearer t1":
                return httpx.Response(401)
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            auth=BearerAuth(token_source),
            transport=transport,
            interceptors=[RefreshInterceptor(refresh=refresh)],
        )
        try:
            response = await client.get("/protected")
        finally:
            await client.aclose()

        assert response.status_code == 200
        assert seen == ["Bearer t1", "Bearer t2"]

    async def test_no_interceptors_preserves_event_hook_auth(self):
        """Backward compat: the pre-existing auth path still runs when
        no interceptors are attached."""

        seen: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization", ""))
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            auth=BearerAuth("classic-token"),
            transport=transport,
        )
        try:
            response = await client.get("/x")
        finally:
            await client.aclose()

        assert response.status_code == 200
        assert seen == ["Bearer classic-token"]

    async def test_events_fire_once_per_external_call(self):
        """Even across internal retries, lifecycle events fire once."""

        class _Bus:
            def __init__(self) -> None:
                self.events: list[tuple[str, dict[str, Any]]] = []

            async def emit(self, name: str, **payload: Any) -> None:
                self.events.append((name, payload))

        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(503)
            return httpx.Response(200)

        bus = _Bus()
        transport = httpx.MockTransport(handler)
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            transport=transport,
            event_bus=bus,  # type: ignore[arg-type]
            interceptors=[RetryInterceptor(on_status=[503], max_attempts=5)],
        )
        try:
            await client.get("/retry")
        finally:
            await client.aclose()

        started = [e for e in bus.events if e[0] == "http.request.started"]
        completed = [e for e in bus.events if e[0] == "http.request.completed"]
        assert len(started) == 1
        assert len(completed) == 1
        assert completed[0][1]["status"] == 200


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


class TestRegistryAmbientInterceptors:
    async def test_ambient_auto_applied_when_matching(self):
        class Ambient(InterceptorBase):
            name = "ambient-stripe"
            order = 100
            applies_to = "stripe"

            async def intercept(self, request, call_next):
                request.headers["X-Stamp"] = "1"
                return await call_next(request)

        ambient = [Ambient()]
        registry = HttpClientRegistry(ambient_interceptors=ambient)

        transport = _transport_sequence([httpx.Response(200)])
        stripe_client = AuthenticatedClient(transport=transport)
        registry.register("stripe", stripe_client)

        assert len(stripe_client.interceptors) == 1
        assert stripe_client.interceptors[0].name == "ambient-stripe"

        other_client = AuthenticatedClient(transport=_transport_sequence([httpx.Response(200)]))
        registry.register("twilio", other_client)
        assert other_client.interceptors == []

        await registry.aclose_all()

    async def test_explicit_interceptors_bypass_ambient(self):
        class Ambient(InterceptorBase):
            name = "ambient"
            order = 100
            applies_to = "*"

            async def intercept(self, request, call_next):
                return await call_next(request)

        class Explicit(InterceptorBase):
            name = "explicit"
            order = 100
            applies_to = "*"

            async def intercept(self, request, call_next):
                return await call_next(request)

        registry = HttpClientRegistry(ambient_interceptors=[Ambient()])
        client = AuthenticatedClient(transport=_transport_sequence([httpx.Response(200)]))
        registry.register("c1", client, interceptors=[Explicit()])

        names = {i.name for i in client.interceptors}
        assert names == {"explicit"}

        await registry.aclose_all()


# ---------------------------------------------------------------------------
# discover_interceptors
# ---------------------------------------------------------------------------


VALID_INTERCEPTOR_FILE = """
from hotframe.http import InterceptorBase

class MyInterceptor(InterceptorBase):
    name = "custom"
    order = 42
    applies_to = "*"

    async def intercept(self, request, call_next):
        return await call_next(request)

custom = MyInterceptor()
"""


DUPLICATE_INTERCEPTOR_FILE = """
from hotframe.http import InterceptorBase

class MyInterceptor(InterceptorBase):
    name = "custom"
    order = 99
    applies_to = "*"

    async def intercept(self, request, call_next):
        return await call_next(request)

custom = MyInterceptor()
"""


SECOND_VALID_FILE = """
from hotframe.http import InterceptorBase

class SecondInterceptor(InterceptorBase):
    name = "second"
    order = 10
    applies_to = "*"

    async def intercept(self, request, call_next):
        return await call_next(request)

second = SecondInterceptor()
"""


MALFORMED_FILE = """
this is not valid python !!!
"""


NON_INTERCEPTOR_FILE = """
# No interceptor-like objects here.
FOO = "bar"

def helper():
    return 1
"""


class TestDiscoverInterceptors:
    def test_discovers_and_orders(self, tmp_path: Path):
        (tmp_path / "a.py").write_text(textwrap.dedent(VALID_INTERCEPTOR_FILE))
        (tmp_path / "b.py").write_text(textwrap.dedent(SECOND_VALID_FILE))

        discovered = discover_interceptors([tmp_path])

        assert [i.name for i in discovered] == ["second", "custom"]

    def test_malformed_file_is_skipped(self, tmp_path: Path):
        (tmp_path / "good.py").write_text(textwrap.dedent(VALID_INTERCEPTOR_FILE))
        (tmp_path / "bad.py").write_text(textwrap.dedent(MALFORMED_FILE))

        discovered = discover_interceptors([tmp_path])

        assert [i.name for i in discovered] == ["custom"]

    def test_duplicates_are_deduplicated(self, tmp_path: Path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "one.py").write_text(textwrap.dedent(VALID_INTERCEPTOR_FILE))
        (dir_b / "two.py").write_text(textwrap.dedent(DUPLICATE_INTERCEPTOR_FILE))

        discovered = discover_interceptors([dir_a, dir_b])

        assert len(discovered) == 1
        assert discovered[0].name == "custom"

    def test_non_interceptor_files_are_ignored(self, tmp_path: Path):
        (tmp_path / "noise.py").write_text(textwrap.dedent(NON_INTERCEPTOR_FILE))

        discovered = discover_interceptors([tmp_path])
        assert discovered == []

    def test_missing_directory_is_tolerated(self, tmp_path: Path):
        missing = tmp_path / "does-not-exist"
        discovered = discover_interceptors([missing])
        assert discovered == []
