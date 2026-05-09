# SPDX-License-Identifier: Apache-2.0
"""
Tests for :class:`hotframe.engine.boundary.ModuleBoundaryMiddleware`.

These tests build a minimal Starlette app with a fake "bomba" module
whose route raises a ``RuntimeError``. The boundary middleware must:

1. Convert the exception into a contained 5xx response
2. Leave Hub Core routes (e.g. ``/health``) responsive
3. After ``threshold`` errors inside the rolling window, mark the module
   ``degraded`` (via the injected ``ModuleStateDB``) and emit
   ``module.degraded`` on the event bus
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from hotframe.engine.boundary import ModuleBoundaryMiddleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingBus:
    """Collects every ``emit`` call so tests can assert on emitted events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, name: str, **kwargs: Any) -> None:
        self.events.append((name, kwargs))


class _FakeStateDB:
    """Stand-in for ``ModuleStateDB`` so boundary tests don't need a DB.

    The boundary middleware imports ``ModuleStateDB`` lazily inside
    ``_mark_degraded`` — tests monkeypatch that import site to swap in
    this fake.
    """

    def __init__(self) -> None:
        self.degraded_calls: list[tuple[str, str]] = []

    async def set_degraded(
        self,
        session: Any,
        module_id: str,
        error_message: str,
        **filters: Any,
    ) -> None:
        self.degraded_calls.append((module_id, error_message))


def _build_app(
    *,
    bus: _RecordingBus,
    state_db: _FakeStateDB,
    threshold: int = 10,
    window_seconds: float = 60.0,
) -> Starlette:
    """Build a minimal Starlette app with one healthy core route plus a
    "bomba" module route that always raises.

    The boundary middleware is wired with a tiny threshold so tests can
    drive it across the limit cheaply.
    """

    async def health(_request: Request) -> Response:
        return PlainTextResponse("ok")

    async def bomba_explode(_request: Request) -> Response:  # pragma: no cover
        raise RuntimeError("kaboom")

    async def bomba_ok(_request: Request) -> Response:
        return JSONResponse({"module": "bomba", "ok": True})

    class _AttachFakeSession(BaseHTTPMiddleware):
        """Inner middleware that pins ``request.state.session`` so the
        boundary's request-session branch fires instead of the
        session-factory fallback (which would require a real DB)."""

        async def dispatch(
            self,
            request: Request,
            call_next: Callable[[Request], Any],
        ) -> Response:
            request.state.session = object()
            return await call_next(request)

    app = Starlette(
        debug=False,
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/m/bomba/explode", bomba_explode, methods=["GET"]),
            Route("/m/bomba/ok", bomba_ok, methods=["GET"]),
        ],
    )

    # NOTE on order: ``add_middleware`` wraps the existing app, so the
    # last-added middleware is the OUTERMOST. We need the boundary on the
    # outside (so it catches exceptions from the inner _AttachFakeSession
    # AND from the route), but _AttachFakeSession must run BEFORE the
    # boundary's ``call_next`` returns so ``request.state.session`` is set
    # by the time _mark_degraded reads it. Both run on the request path,
    # in outer-to-inner order: boundary -> attach_session -> route.
    app.add_middleware(_AttachFakeSession)
    app.add_middleware(
        ModuleBoundaryMiddleware,
        threshold=threshold,
        window_seconds=window_seconds,
    )

    # The middleware reads ``app.state.event_bus``; install our recording bus.
    app.state.event_bus = bus
    return app


@pytest.fixture
def fake_state_db(monkeypatch: pytest.MonkeyPatch) -> _FakeStateDB:
    """Monkeypatch ``ModuleStateDB`` inside the boundary module for the test."""
    state_db = _FakeStateDB()

    def _factory() -> _FakeStateDB:
        return state_db

    # The middleware does ``from hotframe.engine.state import ModuleStateDB``
    # inside ``_mark_degraded`` — patch the source module so the fresh
    # import inside the helper resolves to our fake.
    monkeypatch.setattr(
        "hotframe.engine.state.ModuleStateDB",
        _factory,
        raising=True,
    )
    return state_db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_extract_module_id_view_route() -> None:
    assert ModuleBoundaryMiddleware._extract_module_id("/m/sales/orders") == "sales"


def test_extract_module_id_api_route() -> None:
    assert ModuleBoundaryMiddleware._extract_module_id("/api/v1/m/inventory/stock") == "inventory"


def test_extract_module_id_non_module_path() -> None:
    assert ModuleBoundaryMiddleware._extract_module_id("/health") is None
    assert ModuleBoundaryMiddleware._extract_module_id("/") is None
    assert ModuleBoundaryMiddleware._extract_module_id("/admin/foo") is None


def test_module_exception_does_not_crash_hub(
    fake_state_db: _FakeStateDB,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A buggy module route returns a contained 5xx; Hub Core stays up."""
    bus = _RecordingBus()
    app = _build_app(bus=bus, state_db=fake_state_db)

    caplog.set_level(logging.ERROR, logger="hotframe.engine.boundary")
    with TestClient(app, raise_server_exceptions=False) as client:
        # Module route blows up — but is contained.
        resp = client.get("/m/bomba/explode")
        assert resp.status_code == 503, resp.text
        assert "bomba" in resp.text.lower()

        # Hub Core route still serves normally.
        resp_health = client.get("/health")
        assert resp_health.status_code == 200
        assert resp_health.text == "ok"

        # Healthy route on the *same* module also still serves — the
        # boundary degrades behaviour per request, not per module.
        resp_ok = client.get("/m/bomba/ok")
        assert resp_ok.status_code == 200

    # ``module.error`` was emitted at least once for the explosion.
    error_events = [e for e in bus.events if e[0] == "module.error"]
    assert error_events, "expected at least one module.error emission"
    assert error_events[0][1]["module_id"] == "bomba"
    assert error_events[0][1]["error_type"] == "RuntimeError"


def test_api_route_returns_json_error_envelope(fake_state_db: _FakeStateDB) -> None:
    """API routes get a JSON envelope, not HTML — clients can parse it."""
    bus = _RecordingBus()

    async def api_explode(_request: Request) -> Response:  # pragma: no cover
        raise ValueError("api boom")

    app = Starlette(
        routes=[Route("/api/v1/m/bomba/explode", api_explode, methods=["GET"])],
    )
    app.add_middleware(ModuleBoundaryMiddleware, threshold=10)
    app.state.event_bus = bus

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/m/bomba/explode")
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "module_unavailable"
        assert body["module_id"] == "bomba"
        assert body["error_type"] == "ValueError"


def test_module_marked_degraded_after_threshold(
    fake_state_db: _FakeStateDB,
) -> None:
    """``threshold`` errors in the rolling window flips status to degraded."""
    bus = _RecordingBus()
    app = _build_app(bus=bus, state_db=fake_state_db, threshold=3, window_seconds=60.0)

    with TestClient(app, raise_server_exceptions=False) as client:
        for _ in range(3):
            client.get("/m/bomba/explode")

    assert fake_state_db.degraded_calls, "expected set_degraded to fire after threshold"
    module_id, message = fake_state_db.degraded_calls[0]
    assert module_id == "bomba"
    assert "threshold" in message.lower()

    degraded_events = [e for e in bus.events if e[0] == "module.degraded"]
    assert degraded_events, "expected module.degraded event after threshold"
    assert degraded_events[0][1]["module_id"] == "bomba"


def test_below_threshold_does_not_mark_degraded(
    fake_state_db: _FakeStateDB,
) -> None:
    """Errors below the threshold record ``module.error`` but never degrade."""
    bus = _RecordingBus()
    app = _build_app(bus=bus, state_db=fake_state_db, threshold=10)

    with TestClient(app, raise_server_exceptions=False) as client:
        for _ in range(3):
            client.get("/m/bomba/explode")

    assert fake_state_db.degraded_calls == []
    assert not [e for e in bus.events if e[0] == "module.degraded"]
    # ``module.error`` still emitted on every failure.
    assert len([e for e in bus.events if e[0] == "module.error"]) == 3


def test_reset_tracker_gives_module_clean_slate(
    fake_state_db: _FakeStateDB,
) -> None:
    """``reset_tracker(module_id)`` drops the rolling window for that module."""
    middleware = ModuleBoundaryMiddleware(
        app=lambda *_a, **_k: None,  # type: ignore[arg-type]
        threshold=3,
        window_seconds=60.0,
    )

    # Manually drive the tracker to threshold-1.
    tracker = middleware._trackers["bomba"]
    tracker.record()
    tracker.record()
    assert len(tracker.errors) == 2

    middleware.reset_tracker("bomba")
    assert middleware._trackers["bomba"].errors == middleware._trackers["bomba"].errors
    assert len(middleware._trackers["bomba"].errors) == 0
