# SPDX-License-Identifier: Apache-2.0
"""Tests for component router and static asset mounting."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from hotframe.components.entry import ComponentEntry
from hotframe.components.mounting import (
    mount_component_routers,
    mount_component_routers_for_module,
    mount_component_static,
    mount_component_static_for_module,
    unmount_component_router,
    unmount_component_routers_for_module,
    unmount_component_static,
    unmount_component_static_for_module,
)
from hotframe.components.registry import ComponentRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_router(label: str) -> APIRouter:
    """Build a minimal APIRouter with a single GET endpoint."""
    router = APIRouter()

    @router.get("/ping")
    async def _ping() -> dict:
        return {"component": label}

    @router.post("/echo")
    async def _echo(payload: dict) -> dict:
        return {"got": payload}

    return router


def _bare_app() -> FastAPI:
    """Build a FastAPI app without the hotframe lifespan to keep tests fast."""
    app = FastAPI()
    app.state.components = ComponentRegistry()
    return app


# ---------------------------------------------------------------------------
# Router mounting
# ---------------------------------------------------------------------------


class TestMountComponentRouters:
    @pytest.mark.asyncio
    async def test_router_mounted_under_component_prefix(self):
        app = _bare_app()
        registry: ComponentRegistry = app.state.components

        registry.register(
            ComponentEntry(
                name="button",
                template="components/button/template.html",
                has_endpoint=True,
                extra_router=_make_router("button"),
            )
        )

        count = mount_component_routers(app, registry)
        assert count == 1

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/_components/button/ping")
            assert r.status_code == 200
            assert r.json() == {"component": "button"}

    @pytest.mark.asyncio
    async def test_multiple_components_do_not_conflict(self):
        app = _bare_app()
        registry: ComponentRegistry = app.state.components

        for name in ("card", "tabs", "modal"):
            registry.register(
                ComponentEntry(
                    name=name,
                    template=f"components/{name}/template.html",
                    has_endpoint=True,
                    extra_router=_make_router(name),
                )
            )

        count = mount_component_routers(app, registry)
        assert count == 3

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for name in ("card", "tabs", "modal"):
                r = await client.get(f"/_components/{name}/ping")
                assert r.status_code == 200
                assert r.json() == {"component": name}

    @pytest.mark.asyncio
    async def test_components_without_router_are_skipped(self):
        app = _bare_app()
        registry: ComponentRegistry = app.state.components

        registry.register(
            ComponentEntry(
                name="button",
                template="components/button/template.html",
                extra_router=None,
            )
        )

        count = mount_component_routers(app, registry)
        assert count == 0

    @pytest.mark.asyncio
    async def test_unknown_component_returns_404(self):
        app = _bare_app()
        registry: ComponentRegistry = app.state.components

        registry.register(
            ComponentEntry(
                name="button",
                template="components/button/template.html",
                has_endpoint=True,
                extra_router=_make_router("button"),
            )
        )
        mount_component_routers(app, registry)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/_components/nope/ping")
            assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_mounted_routes_carry_component_tag(self):
        app = _bare_app()
        registry: ComponentRegistry = app.state.components
        registry.register(
            ComponentEntry(
                name="button",
                template="components/button/template.html",
                has_endpoint=True,
                extra_router=_make_router("button"),
            )
        )
        mount_component_routers(app, registry)

        schema = app.openapi()
        button_ping = schema["paths"]["/_components/button/ping"]["get"]
        assert "component:button" in button_ping["tags"]


# ---------------------------------------------------------------------------
# Per-module router mounting + teardown
# ---------------------------------------------------------------------------


class TestMountComponentRoutersForModule:
    @pytest.mark.asyncio
    async def test_only_named_modules_routers_are_mounted(self):
        app = _bare_app()
        registry: ComponentRegistry = app.state.components

        registry.register(
            ComponentEntry(
                name="framework_btn",
                template="fw/btn.html",
                has_endpoint=True,
                extra_router=_make_router("framework_btn"),
                module_id=None,
            )
        )
        registry.register(
            ComponentEntry(
                name="shop_card",
                template="shop/card.html",
                has_endpoint=True,
                extra_router=_make_router("shop_card"),
                module_id="shop",
            )
        )

        count = mount_component_routers_for_module(app, registry, "shop")
        assert count == 1

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/_components/shop_card/ping")
            assert r.status_code == 200

            r = await client.get("/_components/framework_btn/ping")
            assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_unmount_by_module_id_only_removes_that_modules_routes(self):
        app = _bare_app()
        registry: ComponentRegistry = app.state.components

        registry.register(
            ComponentEntry(
                name="alpha_btn",
                template="alpha/btn.html",
                has_endpoint=True,
                extra_router=_make_router("alpha_btn"),
                module_id="alpha",
            )
        )
        registry.register(
            ComponentEntry(
                name="beta_btn",
                template="beta/btn.html",
                has_endpoint=True,
                extra_router=_make_router("beta_btn"),
                module_id="beta",
            )
        )

        assert mount_component_routers(app, registry) == 2

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/_components/alpha_btn/ping")).status_code == 200
            assert (await client.get("/_components/beta_btn/ping")).status_code == 200

        removed = unmount_component_routers_for_module(app, "alpha")
        assert removed >= 1

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/_components/alpha_btn/ping")).status_code == 404
            assert (await client.get("/_components/beta_btn/ping")).status_code == 200

    @pytest.mark.asyncio
    async def test_unmount_single_component(self):
        app = _bare_app()
        registry: ComponentRegistry = app.state.components

        registry.register(
            ComponentEntry(
                name="gone",
                template="gone/template.html",
                has_endpoint=True,
                extra_router=_make_router("gone"),
            )
        )
        mount_component_routers(app, registry)

        assert unmount_component_router(app, "gone") is True
        assert unmount_component_router(app, "gone") is False

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/_components/gone/ping")
            assert r.status_code == 404


# ---------------------------------------------------------------------------
# Static asset serving
# ---------------------------------------------------------------------------


class TestMountComponentStatic:
    @pytest.mark.asyncio
    async def test_static_files_served_under_component_prefix(self, tmp_path: Path):
        static_dir = tmp_path / "button_static"
        static_dir.mkdir()
        (static_dir / "theme.css").write_text("body { color: red }", encoding="utf-8")

        app = _bare_app()
        registry: ComponentRegistry = app.state.components
        registry.register(
            ComponentEntry(
                name="button",
                template="components/button/template.html",
                static_dir=str(static_dir),
            )
        )

        count = mount_component_static(app, registry)
        assert count == 1

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/_components/button/static/theme.css")
            assert r.status_code == 200
            assert "color: red" in r.text

    @pytest.mark.asyncio
    async def test_missing_static_dir_logs_and_skips(self, tmp_path: Path, caplog):
        app = _bare_app()
        registry: ComponentRegistry = app.state.components
        registry.register(
            ComponentEntry(
                name="ghost",
                template="components/ghost/template.html",
                static_dir=str(tmp_path / "does_not_exist"),
            )
        )

        with caplog.at_level("WARNING"):
            count = mount_component_static(app, registry)
        assert count == 0
        assert any("ghost" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_unmount_static_by_module_id(self, tmp_path: Path):
        static_a = tmp_path / "a"
        static_a.mkdir()
        (static_a / "a.css").write_text("/* a */", encoding="utf-8")

        static_b = tmp_path / "b"
        static_b.mkdir()
        (static_b / "b.css").write_text("/* b */", encoding="utf-8")

        app = _bare_app()
        registry: ComponentRegistry = app.state.components

        registry.register(
            ComponentEntry(
                name="a_comp",
                template="a/template.html",
                static_dir=str(static_a),
                module_id="mod_a",
            )
        )
        registry.register(
            ComponentEntry(
                name="b_comp",
                template="b/template.html",
                static_dir=str(static_b),
                module_id="mod_b",
            )
        )
        assert mount_component_static_for_module(app, registry, "mod_a") == 1
        assert mount_component_static_for_module(app, registry, "mod_b") == 1

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/_components/a_comp/static/a.css")).status_code == 200
            assert (await client.get("/_components/b_comp/static/b.css")).status_code == 200

        removed = unmount_component_static_for_module(app, "mod_a")
        assert removed == 1

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/_components/a_comp/static/a.css")).status_code == 404
            assert (await client.get("/_components/b_comp/static/b.css")).status_code == 200

    @pytest.mark.asyncio
    async def test_unmount_static_by_name(self, tmp_path: Path):
        static_dir = tmp_path / "once"
        static_dir.mkdir()
        (static_dir / "file.css").write_text("/* x */", encoding="utf-8")

        app = _bare_app()
        registry: ComponentRegistry = app.state.components
        registry.register(
            ComponentEntry(
                name="once",
                template="once/template.html",
                static_dir=str(static_dir),
            )
        )
        mount_component_static(app, registry)

        assert unmount_component_static(app, "once") is True
        assert unmount_component_static(app, "once") is False

    @pytest.mark.asyncio
    async def test_double_mount_is_a_noop(self, tmp_path: Path):
        static_dir = tmp_path / "idem"
        static_dir.mkdir()
        (static_dir / "x.css").write_text("", encoding="utf-8")

        app = _bare_app()
        registry: ComponentRegistry = app.state.components
        registry.register(
            ComponentEntry(
                name="idem",
                template="idem/template.html",
                static_dir=str(static_dir),
            )
        )

        first = mount_component_static(app, registry)
        second = mount_component_static(app, registry)
        assert first == 1
        assert second == 0


# ---------------------------------------------------------------------------
# CSRF behaviour — components use the normal middleware stack. No auto
# exemption. These tests use the full hotframe app so CSRFMiddleware is
# active.
# ---------------------------------------------------------------------------


class TestCSRFBehaviour:
    @pytest.mark.asyncio
    async def test_post_without_csrf_token_is_blocked(self):
        from hotframe.config.settings import HotframeSettings, reset_settings, set_settings

        reset_settings()
        settings = HotframeSettings(
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            DEBUG=True,
            LOG_LEVEL="WARNING",
            # Explicit exempt list WITHOUT /_components/ so CSRF must run
            # on component POSTs.
            CSRF_EXEMPT_PREFIXES=["/api/", "/health", "/static/"],
        )
        set_settings(settings)

        from hotframe.bootstrap import create_app

        app = create_app(settings)

        async with app.router.lifespan_context(app):
            registry: ComponentRegistry = app.state.components
            router = APIRouter()

            @router.post("/submit")
            async def submit(payload: dict) -> dict:
                return {"ok": True}

            registry.register(
                ComponentEntry(
                    name="form",
                    template="components/form/template.html",
                    has_endpoint=True,
                    extra_router=router,
                )
            )
            mount_component_routers(app, registry)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post(
                    "/_components/form/submit",
                    json={"k": "v"},
                )
                assert r.status_code == 403
                assert "CSRF" in r.text

        reset_settings()

    @pytest.mark.asyncio
    async def test_post_with_csrf_token_succeeds(self):
        from hotframe.config.settings import HotframeSettings, reset_settings, set_settings

        reset_settings()
        settings = HotframeSettings(
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            DEBUG=True,
            LOG_LEVEL="WARNING",
            CSRF_EXEMPT_PREFIXES=["/api/", "/health", "/static/"],
        )
        set_settings(settings)

        from hotframe.bootstrap import create_app

        app = create_app(settings)

        async with app.router.lifespan_context(app):
            registry: ComponentRegistry = app.state.components
            router = APIRouter()

            @router.post("/submit")
            async def submit(payload: dict) -> dict:
                return {"ok": True}

            registry.register(
                ComponentEntry(
                    name="form",
                    template="components/form/template.html",
                    has_endpoint=True,
                    extra_router=router,
                )
            )
            mount_component_routers(app, registry)

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # A GET primes the CSRF cookie.
                await client.get("/health")
                token = client.cookies.get("csrf_token")
                assert token is not None

                r = await client.post(
                    "/_components/form/submit",
                    json={"k": "v"},
                    headers={"x-csrf-token": token},
                )
                assert r.status_code == 200
                assert r.json() == {"ok": True}

        reset_settings()
