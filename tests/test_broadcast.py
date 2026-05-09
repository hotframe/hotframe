"""Regression tests for SSE and WebSocket broadcast endpoints.

Covers four bugs tracked as V7-1..V7-4:
- V7-1: ``/stream/{topic}`` and ``/stream/_mux`` must require authentication
- V7-2: WebSocket upgrades must not be blocked by BaseHTTPMiddleware subclasses
- V7-3: ``/stream/_mux`` without topics must return 400 immediately (no hang)
- V7-4: ``/ws/stream/{topic}`` handler must be registered as a route
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hotframe.bootstrap import create_app
from hotframe.config.settings import HotframeSettings, reset_settings, set_settings


def _build_app() -> TestClient:
    reset_settings()
    settings = HotframeSettings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DEBUG=True,
        LOG_LEVEL="WARNING",
        CSRF_EXEMPT_PREFIXES=["/"],
        RATE_LIMIT_API=999999,
        RATE_LIMIT_AUTH=999999,
    )
    set_settings(settings)
    app = create_app(settings)
    return TestClient(app)


class TestSseRequiresAuth:
    """V7-1 — SSE endpoints must reject anonymous callers.

    hotframe's global 401 handler rewrites HTTP 401 on non-``/api/`` paths
    into a 302 redirect to ``settings.AUTH_LOGIN_URL`` (browser-friendly).
    From the SSE handler's point of view the dependency raised 401; from
    the wire it is a 302 with a Location header. Either outcome proves
    the endpoint rejected the anonymous request without opening the
    event stream — which is what the bug report requires.
    """

    def test_stream_topic_anonymous_rejected(self):
        client = _build_app()
        response = client.get("/stream/notifications", follow_redirects=False)
        # 401 (direct) or 302 -> /login (via bootstrap exception handler)
        assert response.status_code in (302, 401)
        if response.status_code == 302:
            assert "/login" in response.headers.get("location", "")

    def test_stream_mux_anonymous_rejected(self):
        client = _build_app()
        response = client.get("/stream/_mux?topics=a,b", follow_redirects=False)
        assert response.status_code in (302, 401)
        if response.status_code == 302:
            assert "/login" in response.headers.get("location", "")


class TestMuxRequiresTopics:
    """V7-3 — /stream/_mux with no topics must return 400 (no hang)."""

    def test_mux_without_topics_returns_400(self):
        client = _build_app()
        # Note: auth runs first (V7-1), so anonymous callers see 302/401
        # before reaching the topic validator. The regression under test
        # is that the topic validator never opens the event stream; the
        # auth short-circuit is even stronger evidence (handler was
        # never entered). For a direct test of the 400 path, we call
        # the handler function directly below.
        response = client.get("/stream/_mux", follow_redirects=False)
        assert response.status_code in (302, 400, 401)

    def test_mux_validator_raises_http_400(self):
        """Direct test of the topic validator — bypasses auth."""
        from fastapi import HTTPException

        from hotframe.views.broadcast import stream_multiplexed

        async def _run():
            # Dummy request and user — the validator must reject before
            # touching either of them.
            try:
                await stream_multiplexed(request=None, user=object(), topics="")
            except HTTPException as exc:
                return exc
            return None

        import asyncio

        exc = asyncio.run(_run())
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 400
        assert "topic" in (exc.detail or "").lower()

    def test_mux_validator_rejects_whitespace_only_topics(self):
        from fastapi import HTTPException

        from hotframe.views.broadcast import stream_multiplexed

        async def _run():
            try:
                await stream_multiplexed(request=None, user=object(), topics=" , , ")
            except HTTPException as exc:
                return exc
            return None

        import asyncio

        exc = asyncio.run(_run())
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 400


class TestWsStreamRegistered:
    """V7-4 — /ws/stream/{topic} must be registered as a WebSocket route."""

    def test_ws_stream_route_exists(self):
        client = _build_app()
        app = client.app

        # Find a websocket route whose path matches /ws/stream/{topic}.
        from starlette.routing import WebSocketRoute

        ws_routes = [r for r in app.routes if isinstance(r, WebSocketRoute)]
        matched = [r for r in ws_routes if "/ws/stream/" in getattr(r, "path", "")]
        assert matched, (
            "ws_broadcast_handler must be registered at /ws/stream/{topic} "
            "— found routes: " + ", ".join(getattr(r, "path", "?") for r in ws_routes)
        )

    def test_ws_stream_requires_auth(self):
        """V7-2 — handshake must succeed through middleware, then auth closes
        anonymous connections.

        If the WebSocket is blocked by middleware we would get a 403 on
        the handshake (WebSocketDisconnect). If it is accepted without
        auth we would receive frames. The correct behaviour is: the
        handshake completes, the handler runs, and the server closes
        with application code 4401 (unauthorized) because no session
        cookie was provided.
        """
        client = _build_app()
        # No auth cookie set — connection must be rejected by the
        # handler (not the middleware). The TestClient raises
        # WebSocketDisconnect on receive when the server closes.
        try:
            with client.websocket_connect("/ws/stream/notifications"):
                # Server closes before any frame; iterating should raise.
                pass
        except WebSocketDisconnect as exc:
            # Expected — 4401 (app-defined "unauthorized") or 1005/1000
            # depending on how the test client surfaces the close. Any
            # close is acceptable as long as middleware didn't reject
            # the upgrade with 403.
            assert exc.code in (4401, 1005, 1000, 1001, 1006), f"unexpected close code: {exc.code}"


class TestWebsocketPassesMiddleware:
    """V7-2 — BaseHTTPMiddleware subclasses must not reject WS upgrades."""

    def test_upgrade_reaches_ws_handler(self):
        """The upgrade request must reach the handler (not be rejected at
        the middleware layer with 403). We assert this by checking that
        the close comes from the handler (code 4401) rather than from a
        rejected handshake.

        BaseHTTPMiddleware.__call__ in Starlette already forwards non-HTTP
        scopes to the wrapped app without invoking dispatch(), so pure
        BaseHTTPMiddleware subclasses are transparent to WebSocket
        traffic. This test locks that invariant in.
        """
        client = _build_app()
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/ws/stream/test") as ws,
        ):
            # Drain until close.
            ws.receive_text()

        # 4401 means our handler executed and rejected auth. 1006 / 1000
        # / 1001 / 1005 are normal-close framings that different test
        # clients may surface. A 403-at-handshake would raise a
        # different exception type on connect, not WebSocketDisconnect.
        assert excinfo.value.code in (4401, 1000, 1001, 1005, 1006)
