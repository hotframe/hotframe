"""Tests for hotframe.components.registry and ComponentEntry."""

from __future__ import annotations

import logging

import pytest

from hotframe.components.entry import ComponentEntry
from hotframe.components.registry import ComponentRegistry


def _make_entry(name: str, **overrides) -> ComponentEntry:
    defaults = {"name": name, "template": f"{name}.html"}
    defaults.update(overrides)
    return ComponentEntry(**defaults)


class TestRegisterAndLookup:
    def test_register_adds_entry(self):
        registry = ComponentRegistry()
        entry = _make_entry("button")
        registry.register(entry)
        assert registry.get("button") is entry

    def test_register_writes_back_module_id(self):
        registry = ComponentRegistry()
        entry = _make_entry("button")
        registry.register(entry, module_id="ui_kit")
        assert entry.module_id == "ui_kit"

    def test_register_does_not_overwrite_explicit_module_id_when_kw_is_none(self):
        registry = ComponentRegistry()
        entry = _make_entry("button", module_id="ui_kit")
        registry.register(entry)  # no module_id kwarg
        assert entry.module_id == "ui_kit"

    def test_get_returns_none_for_missing(self):
        registry = ComponentRegistry()
        assert registry.get("missing") is None


class TestOverwriteWarning:
    def test_name_collision_logs_warning_and_overwrites(self, caplog):
        registry = ComponentRegistry()
        first = _make_entry("button", template="a.html")
        second = _make_entry("button", template="b.html")
        registry.register(first, module_id="mod_a")

        with caplog.at_level(logging.WARNING, logger="hotframe.components.registry"):
            registry.register(second, module_id="mod_b")

        assert registry.get("button") is second
        assert any(
            "collision" in rec.message.lower() and "'button'" in rec.message
            for rec in caplog.records
        )


class TestUnregister:
    def test_unregister_removes_entry(self):
        registry = ComponentRegistry()
        registry.register(_make_entry("button"))
        registry.unregister("button")
        assert registry.get("button") is None

    def test_unregister_missing_is_silent(self):
        registry = ComponentRegistry()
        # Must not raise
        registry.unregister("never_registered")


class TestUnregisterModule:
    def test_unregister_module_removes_all_entries_for_that_module(self):
        registry = ComponentRegistry()
        registry.register(_make_entry("button"), module_id="ui_kit")
        registry.register(_make_entry("card"), module_id="ui_kit")
        registry.register(_make_entry("chart"), module_id="reporting")

        registry.unregister_module("ui_kit")

        assert registry.get("button") is None
        assert registry.get("card") is None
        assert registry.get("chart") is not None
        assert len(registry) == 1

    def test_unregister_module_preserves_entries_without_module_id(self):
        registry = ComponentRegistry()
        registry.register(_make_entry("neutral"))  # module_id = None
        registry.register(_make_entry("owned"), module_id="mod")

        registry.unregister_module("mod")

        assert registry.get("neutral") is not None
        assert registry.get("owned") is None

    def test_unregister_module_no_match_is_noop(self):
        registry = ComponentRegistry()
        registry.register(_make_entry("button"), module_id="ui_kit")
        registry.unregister_module("nonexistent")
        assert len(registry) == 1


class TestContainerProtocol:
    def test_has(self):
        registry = ComponentRegistry()
        registry.register(_make_entry("button"))
        assert registry.has("button") is True
        assert registry.has("missing") is False

    def test_contains(self):
        registry = ComponentRegistry()
        registry.register(_make_entry("button"))
        assert "button" in registry
        assert "missing" not in registry

    def test_len(self):
        registry = ComponentRegistry()
        assert len(registry) == 0
        registry.register(_make_entry("a"))
        registry.register(_make_entry("b"))
        assert len(registry) == 2

    def test_list_components(self):
        registry = ComponentRegistry()
        registry.register(_make_entry("a"))
        registry.register(_make_entry("b"))
        names = [e.name for e in registry.list_components()]
        assert sorted(names) == ["a", "b"]

    def test_repr(self):
        registry = ComponentRegistry()
        registry.register(_make_entry("a"))
        assert "ComponentRegistry" in repr(registry)
        assert "1" in repr(registry)


class TestClear:
    def test_clear_empties_the_registry(self):
        registry = ComponentRegistry()
        registry.register(_make_entry("a"), module_id="m")
        registry.register(_make_entry("b"))
        registry.clear()
        assert len(registry) == 0
        assert registry.get("a") is None


class TestComponentEntry:
    def test_defaults(self):
        entry = ComponentEntry(name="button", template="button.html")
        assert entry.name == "button"
        assert entry.template == "button.html"
        assert entry.has_endpoint is False
        assert entry.render_fn is None
        assert entry.extra_router is None
        assert entry.module_id is None
        assert entry.static_dir is None
        assert entry.props_cls is None

    def test_dataclass_uses_slots(self):
        entry = ComponentEntry(name="button", template="button.html")
        with pytest.raises(AttributeError):
            entry.unknown_attr = "nope"  # type: ignore[attr-defined]


class TestPublicApi:
    def test_component_registry_importable_from_hotframe(self):
        from hotframe import ComponentRegistry as Exported

        assert Exported is ComponentRegistry

    def test_component_entry_importable_from_hotframe(self):
        from hotframe import ComponentEntry as Exported

        assert Exported is ComponentEntry


class TestAppStateWiring:
    @pytest.mark.asyncio
    async def test_registry_is_on_app_state_after_startup(self):
        from hotframe.testing import create_test_app

        app = create_test_app()
        # Enter the FastAPI lifespan context manually so app.state is
        # populated without needing a running ASGI server.
        async with app.router.lifespan_context(app):
            components = getattr(app.state, "components", None)
            assert components is not None
            assert isinstance(components, ComponentRegistry)
