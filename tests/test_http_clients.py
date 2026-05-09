"""Tests for the ``hotframe.http`` subsystem.

Covers:

- Every built-in :class:`Auth` strategy and the behavior of callable
  credential sources (sync + async) being re-read per request.
- :class:`AuthenticatedClient` dispatch through ``httpx.MockTransport``
  including header application, credential rotation, event emission,
  and lifecycle closing.
- :class:`HttpClientRegistry` — register/replace/get/unregister/list,
  per-module ownership with ``unregister_module``, and
  ``aclose_all`` on shutdown.
- Integration with :func:`hotframe.create_app`: ``app.state.http_clients``
  exists and shutdown closes every client registered under it.
"""

from __future__ import annotations

import base64
import hmac
from hashlib import sha256
from typing import Any

import httpx
import pytest

from hotframe.http import (
    ApiKeyAuth,
    Auth,
    AuthenticatedClient,
    BasicAuth,
    BearerAuth,
    CustomAuth,
    HmacAuth,
    HttpClientRegistry,
    NoAuth,
    QueryApiKeyAuth,
)
from hotframe.http.events import (
    EVENT_REQUEST_COMPLETED,
    EVENT_REQUEST_FAILED,
    EVENT_REQUEST_STARTED,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_transport(requests: list[httpx.Request]) -> httpx.MockTransport:
    """Return a ``MockTransport`` that records every request into ``requests``."""

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


class _RecordingBus:
    """Drop-in stand-in for ``AsyncEventBus.emit``.

    Records every ``(event_name, payload)`` pair emitted so tests can
    assert on observability behavior without spinning a real bus.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event_name: str, **payload: Any) -> None:
        self.events.append((event_name, payload))


# ---------------------------------------------------------------------------
# Auth strategies — header / query / body application
# ---------------------------------------------------------------------------


class TestBearerAuth:
    async def test_static_token_is_applied(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            auth=BearerAuth("static-token"),
            transport=_capture_transport(requests),
        )
        await client.get("/ping")
        await client.aclose()

        assert requests[0].headers["authorization"] == "Bearer static-token"

    async def test_callable_source_is_reevaluated_per_request(self):
        requests: list[httpx.Request] = []
        tokens = iter(["t-1", "t-2", "t-3"])
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            auth=BearerAuth(lambda: next(tokens)),
            transport=_capture_transport(requests),
        )
        await client.get("/one")
        await client.get("/two")
        await client.get("/three")
        await client.aclose()

        assert [r.headers["authorization"] for r in requests] == [
            "Bearer t-1",
            "Bearer t-2",
            "Bearer t-3",
        ]

    async def test_async_callable_source_is_awaited(self):
        requests: list[httpx.Request] = []
        calls: list[int] = []

        async def fetch_token() -> str:
            calls.append(1)
            return f"token-{len(calls)}"

        client = AuthenticatedClient(
            base_url="https://api.example.com",
            auth=BearerAuth(fetch_token),
            transport=_capture_transport(requests),
        )
        await client.get("/a")
        await client.get("/b")
        await client.aclose()

        assert [r.headers["authorization"] for r in requests] == [
            "Bearer token-1",
            "Bearer token-2",
        ]

    async def test_invalid_source_type_raises(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            auth=BearerAuth(123),  # type: ignore[arg-type]
            transport=_capture_transport(requests),
        )
        with pytest.raises(TypeError):
            await client.get("http://localhost/")
        await client.aclose()

    async def test_callable_returning_non_string_raises(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            auth=BearerAuth(lambda: 42),  # type: ignore[return-value]
            transport=_capture_transport(requests),
        )
        with pytest.raises(TypeError):
            await client.get("http://localhost/")
        await client.aclose()


class TestApiKeyAuth:
    async def test_default_header(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            auth=ApiKeyAuth("secret-123"),
            transport=_capture_transport(requests),
        )
        await client.get("/ping")
        await client.aclose()

        assert requests[0].headers["x-api-key"] == "secret-123"

    async def test_custom_header(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            auth=ApiKeyAuth(source="abc", header="X-Hub-Token"),
            transport=_capture_transport(requests),
        )
        await client.get("/ping")
        await client.aclose()

        assert requests[0].headers["x-hub-token"] == "abc"

    async def test_empty_header_rejected(self):
        with pytest.raises(ValueError):
            ApiKeyAuth("secret", header="")


class TestQueryApiKeyAuth:
    async def test_default_param(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            auth=QueryApiKeyAuth("k"),
            transport=_capture_transport(requests),
        )
        await client.get("/search", params={"q": "hello"})
        await client.aclose()

        url = requests[0].url
        assert url.params["api_key"] == "k"
        assert url.params["q"] == "hello"

    async def test_custom_param_and_rotation(self):
        requests: list[httpx.Request] = []
        keys = iter(["k-1", "k-2"])
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            auth=QueryApiKeyAuth(source=lambda: next(keys), param="apikey"),
            transport=_capture_transport(requests),
        )
        await client.get("/one")
        await client.get("/two")
        await client.aclose()

        assert [str(r.url.params["apikey"]) for r in requests] == ["k-1", "k-2"]

    async def test_empty_param_rejected(self):
        with pytest.raises(ValueError):
            QueryApiKeyAuth("k", param="")


class TestBasicAuth:
    async def test_header_is_base64(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            auth=BasicAuth("alice", "s3cret"),
            transport=_capture_transport(requests),
        )
        await client.get("http://localhost/")
        await client.aclose()

        expected = "Basic " + base64.b64encode(b"alice:s3cret").decode("ascii")
        assert requests[0].headers["authorization"] == expected


class TestHmacAuth:
    async def test_signs_body_with_hmac_sha256(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            auth=HmacAuth(key_id="id-1", secret="s3cret"),
            transport=_capture_transport(requests),
        )
        body = b'{"x":1}'
        await client.post("http://localhost/sign", content=body)
        await client.aclose()

        signature = hmac.new(b"s3cret", body, sha256).hexdigest()
        assert (
            requests[0].headers["authorization"] == f"HMAC-SHA256 KeyId=id-1, Signature={signature}"
        )

    async def test_signs_empty_body(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            auth=HmacAuth(key_id="id-1", secret="s3cret"),
            transport=_capture_transport(requests),
        )
        await client.get("http://localhost/")
        await client.aclose()

        signature = hmac.new(b"s3cret", b"", sha256).hexdigest()
        assert signature in requests[0].headers["authorization"]

    async def test_rejects_empty_key_id(self):
        with pytest.raises(ValueError):
            HmacAuth(key_id="", secret="x")

    async def test_rejects_empty_secret(self):
        with pytest.raises(ValueError):
            HmacAuth(key_id="id", secret="")

    async def test_rejects_unknown_algorithm(self):
        with pytest.raises(ValueError):
            HmacAuth(key_id="id", secret="x", algorithm="totally-not-real")


class TestCustomAuth:
    async def test_calls_callable_with_request(self):
        seen: list[httpx.Request] = []

        async def apply(request: httpx.Request) -> None:
            seen.append(request)
            request.headers["X-Custom"] = "ok"

        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            auth=CustomAuth(apply),
            transport=_capture_transport(requests),
        )
        await client.get("http://localhost/")
        await client.aclose()

        assert len(seen) == 1
        assert requests[0].headers["x-custom"] == "ok"

    def test_rejects_non_callable(self):
        with pytest.raises(TypeError):
            CustomAuth("not a callable")  # type: ignore[arg-type]

    def test_rejects_sync_callable(self):
        def _sync(request: httpx.Request) -> None:  # pragma: no cover - never awaited
            return None

        with pytest.raises(TypeError):
            CustomAuth(_sync)  # type: ignore[arg-type]


class TestNoAuth:
    async def test_does_not_add_headers(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            auth=NoAuth(),
            transport=_capture_transport(requests),
        )
        await client.get("http://localhost/")
        await client.aclose()

        assert "authorization" not in {k.lower() for k in requests[0].headers}


class TestAuthBase:
    async def test_base_apply_is_abstract(self):
        with pytest.raises(NotImplementedError):
            await Auth().apply(httpx.Request("GET", "http://localhost/"))


# ---------------------------------------------------------------------------
# AuthenticatedClient
# ---------------------------------------------------------------------------


class TestAuthenticatedClient:
    async def test_default_auth_is_noop(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(transport=_capture_transport(requests))
        assert isinstance(client.auth, NoAuth)
        await client.get("http://localhost/")
        await client.aclose()
        assert client.is_closed is True

    async def test_headers_kwarg_is_preserved(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            headers={"X-App": "hotframe"},
            transport=_capture_transport(requests),
        )
        await client.get("http://localhost/")
        await client.aclose()

        assert requests[0].headers["x-app"] == "hotframe"

    async def test_all_verb_methods_dispatch(self):
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(transport=_capture_transport(requests))
        await client.get("http://localhost/")
        await client.post("http://localhost/", json={"a": 1})
        await client.put("http://localhost/", json={"a": 1})
        await client.patch("http://localhost/", json={"a": 1})
        await client.delete("http://localhost/")
        await client.request("OPTIONS", "http://localhost/")
        await client.aclose()

        assert [r.method for r in requests] == [
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "OPTIONS",
        ]

    async def test_repr_mentions_base_url_and_auth(self):
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            auth=BearerAuth("x"),
            name="example",
        )
        rendered = repr(client)
        await client.aclose()
        assert "example" in rendered
        assert "api.example.com" in rendered
        assert "BearerAuth" in rendered

    async def test_event_bus_emits_started_and_completed(self):
        bus = _RecordingBus()
        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            base_url="https://api.example.com",
            auth=BearerAuth("t"),
            event_bus=bus,  # type: ignore[arg-type]
            name="cloud",
            transport=_capture_transport(requests),
        )
        await client.get("/ping")
        await client.aclose()

        names = [e[0] for e in bus.events]
        assert EVENT_REQUEST_STARTED in names
        assert EVENT_REQUEST_COMPLETED in names
        completed = next(p for n, p in bus.events if n == EVENT_REQUEST_COMPLETED)
        assert completed["client_name"] == "cloud"
        assert completed["method"] == "GET"
        assert completed["status"] == 200
        assert completed["duration_ms"] >= 0

        # Critically: no Authorization/token leakage in any payload.
        for _, payload in bus.events:
            assert "Bearer" not in str(payload)
            assert "authorization" not in payload

    async def test_event_bus_emits_failed_on_exception(self):
        bus = _RecordingBus()

        async def _raise(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        client = AuthenticatedClient(
            auth=NoAuth(),
            event_bus=bus,  # type: ignore[arg-type]
            name="broken",
            transport=httpx.MockTransport(_raise),
        )
        with pytest.raises(httpx.ConnectError):
            await client.get("http://localhost/")
        await client.aclose()

        names = [e[0] for e in bus.events]
        assert EVENT_REQUEST_STARTED in names
        assert EVENT_REQUEST_FAILED in names
        failed = next(p for n, p in bus.events if n == EVENT_REQUEST_FAILED)
        assert failed["client_name"] == "broken"
        assert failed["method"] == "GET"
        assert "boom" in failed["error"]

    async def test_event_emission_swallows_bus_errors(self):
        class _BrokenBus:
            async def emit(self, *_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("bus exploded")

        requests: list[httpx.Request] = []
        client = AuthenticatedClient(
            event_bus=_BrokenBus(),  # type: ignore[arg-type]
            transport=_capture_transport(requests),
        )
        # Must not raise — observability failures cannot break HTTP.
        response = await client.get("http://localhost/")
        await client.aclose()
        assert response.status_code == 200

    async def test_async_context_manager(self):
        requests: list[httpx.Request] = []
        async with AuthenticatedClient(
            transport=_capture_transport(requests),
        ) as client:
            await client.get("http://localhost/")
        assert client.is_closed is True

    async def test_stream_applies_auth_but_skips_lifecycle_events(self):
        bus = _RecordingBus()
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=b"chunk-1chunk-2")

        client = AuthenticatedClient(
            auth=BearerAuth("stream-token"),
            event_bus=bus,  # type: ignore[arg-type]
            transport=httpx.MockTransport(handler),
        )
        async with client.stream("GET", "http://localhost/stream") as response:
            async for _ in response.aiter_bytes():
                pass
        await client.aclose()

        # Auth still ran on the streaming request
        assert requests[0].headers["authorization"] == "Bearer stream-token"
        # But the request-level event bus lifecycle events are skipped
        names = [e[0] for e in bus.events]
        assert EVENT_REQUEST_STARTED not in names
        assert EVENT_REQUEST_COMPLETED not in names


# ---------------------------------------------------------------------------
# HttpClientRegistry
# ---------------------------------------------------------------------------


class TestHttpClientRegistry:
    def _make_client(self, name: str = "test") -> AuthenticatedClient:
        return AuthenticatedClient(
            auth=NoAuth(),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"name": name})),
        )

    async def test_register_and_lookup(self):
        registry = HttpClientRegistry()
        client = self._make_client()
        registry.register("stripe", client)

        assert registry["stripe"] is client
        assert registry.get("stripe") is client
        assert "stripe" in registry
        assert len(registry) == 1
        assert registry.list_registered() == ["stripe"]

        await registry.aclose_all()

    async def test_get_missing_returns_none(self):
        registry = HttpClientRegistry()
        assert registry.get("nope") is None

    async def test_getitem_missing_raises_keyerror_with_useful_message(self):
        registry = HttpClientRegistry()
        with pytest.raises(KeyError) as exc:
            registry["nope"]
        assert "nope" in str(exc.value)

    async def test_register_rejects_empty_name(self):
        registry = HttpClientRegistry()
        with pytest.raises(ValueError):
            registry.register("", self._make_client())
        with pytest.raises(ValueError):
            registry.register(123, self._make_client())  # type: ignore[arg-type]

    async def test_register_rejects_non_client(self):
        registry = HttpClientRegistry()
        with pytest.raises(TypeError):
            registry.register("bad", object())  # type: ignore[arg-type]

    async def test_register_duplicate_raises(self):
        registry = HttpClientRegistry()
        c1 = self._make_client("a")
        c2 = self._make_client("b")
        registry.register("shared", c1)
        with pytest.raises(KeyError):
            registry.register("shared", c2)
        await c2.aclose()
        await registry.aclose_all()

    async def test_replace_overwrites_and_closes_previous(self):
        registry = HttpClientRegistry()
        c1 = self._make_client("a")
        c2 = self._make_client("b")
        registry.register("name", c1)
        registry.replace("name", c2)

        assert registry["name"] is c2
        # c1 was scheduled for close in the background — give it a tick.
        import asyncio as _asyncio

        for _ in range(20):
            if c1.is_closed:
                break
            await _asyncio.sleep(0)
        assert c1.is_closed is True

        await registry.aclose_all()

    async def test_replace_creates_when_absent(self):
        registry = HttpClientRegistry()
        client = self._make_client()
        registry.replace("new", client)
        assert registry["new"] is client
        await registry.aclose_all()

    async def test_replace_rejects_non_client(self):
        registry = HttpClientRegistry()
        with pytest.raises(TypeError):
            registry.replace("x", object())  # type: ignore[arg-type]

    async def test_unregister_closes_and_removes(self):
        registry = HttpClientRegistry()
        client = self._make_client()
        registry.register("name", client)
        await registry.unregister("name")

        assert "name" not in registry
        assert registry.get("name") is None
        assert client.is_closed is True

    async def test_unregister_missing_raises(self):
        registry = HttpClientRegistry()
        with pytest.raises(KeyError):
            await registry.unregister("nope")

    async def test_owner_of_tracks_registration(self):
        registry = HttpClientRegistry()
        app_client = self._make_client()
        module_client = self._make_client()
        registry.register("app", app_client)
        registry.register("stripe", module_client, owner_module_id="m_stripe")

        assert registry.owner_of("app") is None
        assert registry.owner_of("stripe") == "m_stripe"

        with pytest.raises(KeyError):
            registry.owner_of("missing")

        await registry.aclose_all()

    async def test_unregister_module_drops_only_owned(self):
        registry = HttpClientRegistry()
        app_client = self._make_client()
        stripe_client = self._make_client()
        whatsapp_client = self._make_client()
        other_module_client = self._make_client()

        registry.register("app", app_client)  # no owner
        registry.register("stripe", stripe_client, owner_module_id="m_pay")
        registry.register("whatsapp", whatsapp_client, owner_module_id="m_pay")
        registry.register("analytics", other_module_client, owner_module_id="m_track")

        await registry.unregister_module("m_pay")

        assert "app" in registry
        assert "analytics" in registry
        assert "stripe" not in registry
        assert "whatsapp" not in registry
        assert stripe_client.is_closed is True
        assert whatsapp_client.is_closed is True
        assert app_client.is_closed is False
        assert other_module_client.is_closed is False

        await registry.aclose_all()

    async def test_unregister_module_no_clients_is_noop(self):
        registry = HttpClientRegistry()
        registry.register("app", self._make_client())
        await registry.unregister_module("m_ghost")  # must not raise
        assert "app" in registry
        await registry.aclose_all()

    async def test_aclose_all_closes_every_client(self):
        registry = HttpClientRegistry()
        a = self._make_client()
        b = self._make_client()
        c = self._make_client()
        registry.register("a", a)
        registry.register("b", b, owner_module_id="m")
        registry.register("c", c)

        await registry.aclose_all()

        assert len(registry) == 0
        assert a.is_closed is True
        assert b.is_closed is True
        assert c.is_closed is True
        # Double-close is safe.
        await registry.aclose_all()

    async def test_aclose_silent_swallows_errors(self, caplog):
        registry = HttpClientRegistry()

        class _Boom(AuthenticatedClient):
            async def aclose(self) -> None:  # type: ignore[override]
                raise RuntimeError("detonate")

        client = _Boom(transport=httpx.MockTransport(lambda r: httpx.Response(204)))
        registry.register("x", client)
        # Must not raise even though close throws.
        await registry.aclose_all()
        assert any("Failed to close HTTP client" in rec.message for rec in caplog.records)

    async def test_repr(self):
        registry = HttpClientRegistry()
        registry.register("a", self._make_client())
        assert "HttpClientRegistry" in repr(registry)
        await registry.aclose_all()


# ---------------------------------------------------------------------------
# Integration with create_app
# ---------------------------------------------------------------------------


class TestAppWiring:
    async def test_create_app_populates_http_clients_and_shuts_down(self):
        from hotframe.testing import create_test_app

        app = create_test_app()
        async with app.router.lifespan_context(app):
            registry = getattr(app.state, "http_clients", None)
            assert isinstance(registry, HttpClientRegistry)

            tracked = AuthenticatedClient(
                auth=NoAuth(),
                transport=httpx.MockTransport(lambda r: httpx.Response(200)),
            )
            registry.register("cloud", tracked)
            assert "cloud" in registry

        # After the lifespan exits, aclose_all must have closed the client.
        assert tracked.is_closed is True

    async def test_settings_has_http_client_events_flag(self):
        from hotframe.config.settings import HotframeSettings

        settings = HotframeSettings()
        assert settings.HTTP_CLIENT_EVENTS is False

    async def test_http_symbols_importable_from_top_level(self):
        import hotframe

        assert hotframe.AuthenticatedClient is AuthenticatedClient
        assert hotframe.HttpClientRegistry is HttpClientRegistry
        assert hotframe.Auth is Auth
        assert hotframe.BearerAuth is BearerAuth
        assert hotframe.ApiKeyAuth is ApiKeyAuth
        assert hotframe.QueryApiKeyAuth is QueryApiKeyAuth
        assert hotframe.BasicAuth is BasicAuth
        assert hotframe.HmacAuth is HmacAuth
        assert hotframe.CustomAuth is CustomAuth
        assert hotframe.NoAuth is NoAuth
