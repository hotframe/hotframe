"""Tests for hotframe.components.discovery."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from hotframe.components import Component, ComponentRegistry
from hotframe.components.discovery import (
    discover_app_components,
    discover_apps_components,
    discover_components,
    discover_module_components,
)


@pytest.fixture
def tmp_components_root():
    """Create a temporary components root with a clean teardown."""
    tmp = Path(tempfile.mkdtemp(prefix="hotframe-components-"))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test fixtures — helpers to build fake components on disk.
# ---------------------------------------------------------------------------


def _make_template_only(root: Path, name: str) -> Path:
    comp = root / name
    comp.mkdir(parents=True)
    (comp / "template.html").write_text(f"<div>template-only:{name}</div>")
    return comp


def _make_python_component(
    root: Path,
    name: str,
    *,
    class_body: str = "",
) -> Path:
    comp = root / name
    comp.mkdir(parents=True)
    (comp / "template.html").write_text("<div>{{ title }}</div>")
    default_body = class_body or "    title: str\n    subtitle: str = 'default'\n"
    (comp / "component.py").write_text(
        "from hotframe.components import Component\n\n"
        "class MyComponent(Component):\n"
        f"{default_body}"
    )
    return comp


def _make_with_routes(root: Path, name: str) -> Path:
    comp = root / name
    comp.mkdir(parents=True)
    (comp / "template.html").write_text(f"<div>with-routes:{name}</div>")
    (comp / "routes.py").write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n"
        "@router.get('/ping')\n"
        "async def ping():\n"
        "    return {'ok': True}\n"
    )
    return comp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDiscoverTemplateOnly:
    def test_single_template_only(self, tmp_components_root):
        _make_template_only(tmp_components_root, "alert")
        entries = discover_components(tmp_components_root)
        assert len(entries) == 1
        e = entries[0]
        assert e.name == "alert"
        assert e.template == "alert/template.html"
        assert e.props_cls is None
        assert e.render_fn is None
        assert e.has_endpoint is False
        assert e.extra_router is None

    def test_multiple_template_only(self, tmp_components_root):
        _make_template_only(tmp_components_root, "alert")
        _make_template_only(tmp_components_root, "badge")
        entries = discover_components(tmp_components_root)
        names = {e.name for e in entries}
        assert names == {"alert", "badge"}


class TestDiscoverPythonComponent:
    def test_python_component_has_props_cls(self, tmp_components_root):
        _make_python_component(tmp_components_root, "card")
        entries = discover_components(tmp_components_root, import_prefix="_hf_test_python")
        assert len(entries) == 1
        e = entries[0]
        assert e.props_cls is not None
        assert issubclass(e.props_cls, Component)
        assert e.render_fn is not None

    def test_render_fn_validates_and_returns_context(self, tmp_components_root):
        _make_python_component(tmp_components_root, "card")
        entries = discover_components(tmp_components_root, import_prefix="_hf_test_python_render")
        render_fn = entries[0].render_fn
        result = render_fn(title="Hello")
        assert result == {"title": "Hello", "subtitle": "default"}


class TestDiscoverRoutes:
    def test_routes_py_registers_router(self, tmp_components_root):
        _make_with_routes(tmp_components_root, "picker")
        entries = discover_components(tmp_components_root, import_prefix="_hf_test_routes")
        assert len(entries) == 1
        e = entries[0]
        assert e.has_endpoint is True
        assert e.extra_router is not None


class TestDiscoveryWarnings:
    def test_missing_template_html_is_skipped(self, tmp_components_root, caplog):
        comp = tmp_components_root / "broken"
        comp.mkdir()
        # No template.html on purpose.
        with caplog.at_level("WARNING"):
            entries = discover_components(tmp_components_root)
        assert entries == []
        assert any("missing template.html" in rec.message for rec in caplog.records)

    def test_component_py_without_component_subclass_warns(self, tmp_components_root, caplog):
        comp = tmp_components_root / "oops"
        comp.mkdir()
        (comp / "template.html").write_text("<div />")
        (comp / "component.py").write_text("x = 1\n")
        with caplog.at_level("WARNING"):
            entries = discover_components(tmp_components_root, import_prefix="_hf_test_no_subclass")
        assert len(entries) == 1
        assert entries[0].props_cls is None
        assert any(
            "no Component or LiveComponent subclass" in rec.message for rec in caplog.records
        )

    def test_routes_py_without_router_variable_warns(self, tmp_components_root, caplog):
        comp = tmp_components_root / "no_router"
        comp.mkdir()
        (comp / "template.html").write_text("<div />")
        (comp / "routes.py").write_text("# empty\n")
        with caplog.at_level("WARNING"):
            entries = discover_components(tmp_components_root, import_prefix="_hf_test_no_router")
        assert len(entries) == 1
        assert entries[0].extra_router is None
        assert entries[0].has_endpoint is False
        assert any("no module-level `router` attribute" in rec.message for rec in caplog.records)

    def test_ignores_private_and_dot_dirs(self, tmp_components_root):
        _make_template_only(tmp_components_root, "good")
        (tmp_components_root / "_private").mkdir()
        (tmp_components_root / "_private" / "template.html").write_text("<div />")
        (tmp_components_root / ".hidden").mkdir()
        (tmp_components_root / ".hidden" / "template.html").write_text("<div />")
        entries = discover_components(tmp_components_root)
        assert {e.name for e in entries} == {"good"}


class TestModuleDiscovery:
    def test_discover_module_components(self, tmp_components_root):
        # Simulate modules/sample/components/<name>
        module_dir = tmp_components_root / "sample"
        comps_dir = module_dir / "components"
        comps_dir.mkdir(parents=True)
        _make_template_only(comps_dir, "widget")

        registry = ComponentRegistry()
        n = discover_module_components(registry, module_dir, "sample")
        assert n == 1
        entry = registry.get("widget")
        assert entry is not None
        assert entry.module_id == "sample"
        assert entry.template == "sample/components/widget/template.html"

    def test_module_discovery_noop_without_components_dir(self, tmp_components_root):
        module_dir = tmp_components_root / "empty"
        module_dir.mkdir()
        registry = ComponentRegistry()
        assert discover_module_components(registry, module_dir, "empty") == 0
        assert len(registry) == 0


class TestAppDiscovery:
    def test_discover_app_components(self, tmp_components_root):
        # Simulate apps/sample/components/<name>
        apps_dir = tmp_components_root
        app_dir = apps_dir / "sample"
        comps_dir = app_dir / "components"
        comps_dir.mkdir(parents=True)
        _make_template_only(comps_dir, "widget")

        registry = ComponentRegistry()
        n = discover_app_components(registry, apps_dir, "sample")
        assert n == 1
        entry = registry.get("widget")
        assert entry is not None
        # App components are project-local: no owning module_id.
        assert entry.module_id is None
        assert entry.template == "sample/components/widget/template.html"

    def test_discover_app_components_noop_without_components_dir(self, tmp_components_root):
        apps_dir = tmp_components_root
        (apps_dir / "empty").mkdir()
        registry = ComponentRegistry()
        assert discover_app_components(registry, apps_dir, "empty") == 0
        assert len(registry) == 0

    def test_discover_apps_components_scans_every_app(self, tmp_components_root):
        apps_dir = tmp_components_root
        # app_a has components, app_b does not, _private is ignored.
        (apps_dir / "app_a" / "components").mkdir(parents=True)
        _make_template_only(apps_dir / "app_a" / "components", "alpha")
        (apps_dir / "app_b").mkdir()
        (apps_dir / "_private" / "components").mkdir(parents=True)
        _make_template_only(apps_dir / "_private" / "components", "nope")

        registry = ComponentRegistry()
        n = discover_apps_components(registry, apps_dir)
        assert n == 1
        assert "alpha" in registry
        assert "nope" not in registry

    def test_discover_apps_components_handles_missing_dir(self, tmp_components_root):
        missing = tmp_components_root / "does-not-exist"
        registry = ComponentRegistry()
        assert discover_apps_components(registry, missing) == 0
