"""Tests for ``hotframe.views.responses``.

Auth + permission gating, full-page render, plain HTTP redirect /
refresh / message helpers. Real-time updates live in
``hotframe.live`` and are tested separately.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request

from hotframe.views.responses import (
    add_message,
    htmx_redirect,
    htmx_refresh,
    htmx_trigger,
    htmx_view,
    is_htmx_request,
    is_reactive_request,
    reactive_message,
    reactive_redirect,
    reactive_refresh,
    reactive_trigger,
    view,
)


def _make_request(headers: dict[str, str] | None = None, query: str = "") -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
        "query_string": query.encode(),
    }
    return Request(scope)


async def _collect_body(response) -> str:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, str):
            chunks.append(chunk.encode())
        else:
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


def _body(response) -> str:
    """Run _collect_body on a fresh loop so it works inside pytest-asyncio tests too."""
    # Streaming responses expose body_iterator; the simple HTML/Redirect
    # responses we now return have a plain `body` attribute.
    if hasattr(response, "body") and response.body:
        return response.body.decode("utf-8")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_collect_body(response))
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _collect_body(response)).result()


class TestIsReactiveRequest:
    """``is_reactive_request`` returns False — every HTTP request is a full page."""

    def test_returns_false_when_no_extra_headers(self):
        req = _make_request()
        assert is_reactive_request(req) is False

    def test_returns_false_even_with_arbitrary_headers(self):
        req = _make_request({"X-Custom": "anything"})
        assert is_reactive_request(req) is False

    def test_alias_matches(self):
        assert is_htmx_request(_make_request()) is False
        assert is_htmx_request(_make_request({"X-Custom": "anything"})) is False


class TestReactiveRedirect:
    def test_emits_303_redirect(self):
        response = reactive_redirect("/login")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_htmx_redirect_alias(self):
        response = htmx_redirect("/login")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestReactiveRefresh:
    def test_emits_meta_refresh(self):
        response = reactive_refresh()
        body = _body(response)
        assert 'http-equiv="refresh"' in body
        assert response.status_code == 200

    def test_htmx_refresh_alias(self):
        response = htmx_refresh()
        body = _body(response)
        assert 'http-equiv="refresh"' in body


class TestReactiveTrigger:
    def test_dispatches_custom_event(self):
        response = reactive_trigger("cartUpdated", count=5)
        body = _body(response)
        assert "cartUpdated" in body
        assert "dispatchEvent" in body
        assert "CustomEvent" in body
        assert '"count": 5' in body or '"count":5' in body

    def test_no_detail(self):
        response = reactive_trigger("ping")
        body = _body(response)
        assert "ping" in body
        assert "dispatchEvent" in body


class TestLegacyHtmxTrigger:
    def test_htmx_trigger_simple(self):
        result = htmx_trigger("cartUpdated")
        assert result == {"cartUpdated": True}

    def test_htmx_trigger_with_data(self):
        result = htmx_trigger("cartUpdated", {"count": 5})
        assert result == {"cartUpdated": {"count": 5}}


class TestReactiveMessage:
    def test_emits_toast_html(self):
        response = reactive_message("success", "Item created")
        body = _body(response)
        assert "Item created" in body
        assert "toast-success" in body

    def test_escapes_html(self):
        response = reactive_message("info", "<script>alert(1)</script>")
        body = _body(response)
        assert "<script>alert" not in body
        assert "&lt;script&gt;" in body


class TestAddMessage:
    def test_appends_to_request_state(self):
        req = _make_request()
        add_message(req, "success", "Saved")
        add_message(req, "error", "Oops")
        assert req.state._messages == [
            {"level": "success", "text": "Saved"},
            {"level": "error", "text": "Oops"},
        ]


class TestViewDecorator:
    """``view`` performs auth + permission gating, then full-page render."""

    def test_view_is_callable_and_alias_matches(self):
        assert callable(view)
        assert htmx_view is view

    @pytest.mark.asyncio
    async def test_login_required_redirects_when_no_user(self, monkeypatch):
        from hotframe.config.settings import HotframeSettings

        settings = HotframeSettings(AUTH_LOGIN_URL="/login")
        monkeypatch.setattr("hotframe.config.settings.get_settings", lambda: settings)
        monkeypatch.setattr("hotframe.views.responses.get_session_user_id", lambda r: None)

        @view(login_required=True)
        async def handler(request):  # pragma: no cover
            return {}

        req = _make_request()
        resp = await handler(req)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    @pytest.mark.asyncio
    async def test_login_required_redirects_regardless_of_extra_headers(self, monkeypatch):
        """Auth always returns a plain 302 redirect; no per-header branches."""
        from hotframe.config.settings import HotframeSettings

        settings = HotframeSettings(AUTH_LOGIN_URL="/login")
        monkeypatch.setattr("hotframe.config.settings.get_settings", lambda: settings)
        monkeypatch.setattr("hotframe.views.responses.get_session_user_id", lambda r: None)

        @view(login_required=True)
        async def handler(request):  # pragma: no cover
            return {}

        req = _make_request({"X-Custom": "anything"})
        resp = await handler(req)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"


class TestBroadcast:
    def test_broadcast_hub_import(self):
        from hotframe.views.broadcast import BroadcastHub

        hub = BroadcastHub()
        assert hub is not None
