"""Tests for hotframe.apps."""

from hotframe.apps.config import AppConfig, ModuleConfig, ModuleManifest
from hotframe.apps.registry import ModuleRegistry
from hotframe.apps.service_facade import ModuleService, action


class TestAppConfig:
    def test_app_config_exists(self):
        assert AppConfig is not None

    def test_module_config_exists(self):
        assert ModuleConfig is not None


class TestModuleManifest:
    def test_manifest_has_fields(self):
        assert "MODULE_ID" in ModuleManifest.model_fields
        assert "MODULE_VERSION" in ModuleManifest.model_fields
        assert "DEPENDENCIES" in ModuleManifest.model_fields


class TestModuleRegistry:
    def test_create_registry(self):
        registry = ModuleRegistry()
        assert registry is not None

    def test_get_loaded_module_ids_empty(self):
        """Fresh registry has no loaded modules."""
        registry = ModuleRegistry()
        assert registry.get_loaded_module_ids() == []

    def test_get_loaded_module_ids_after_register(self):
        """get_loaded_module_ids reflects every registered module_id."""
        from pathlib import Path

        registry = ModuleRegistry()
        manifest = ModuleManifest(
            MODULE_ID="demo",
            MODULE_NAME="Demo",
            MODULE_VERSION="1.0.0",
        )
        registry.register(
            module_id="demo",
            manifest=manifest,
            router=None,
            api_router=None,
            middleware=None,
            path=Path("/tmp/demo"),
        )

        assert registry.get_loaded_module_ids() == ["demo"]
        assert isinstance(registry.get_loaded_module_ids(), list)

        registry.unregister("demo")
        assert registry.get_loaded_module_ids() == []


class TestServiceFacade:
    def test_action_decorator(self):
        class TestService(ModuleService):
            @action(permission="view_test")
            async def list_items(self):
                return []

        assert hasattr(TestService.list_items, "_action_meta")
        assert TestService.list_items._action_meta.permission == "view_test"
        assert TestService.list_items._action_meta.mutates is False

    def test_action_mutates(self):
        class TestService(ModuleService):
            @action(permission="add_test", mutates=True, description="Create item")
            async def create_item(self, name: str):
                pass

        meta = TestService.create_item._action_meta
        assert meta.mutates is True
        assert meta.description == "Create item"
