"""
apps — module configuration and registry.

Defines the declarative contracts that every Hub module must satisfy
(``AppConfig``, ``ModuleConfig``, ``ModuleManifest``) and the in-memory
registries (``AppRegistry``, ``ModuleRegistry``) that the engine uses to
track installed and active modules at runtime.

Key exports::

    from hotframe.apps import AppConfig, ModuleConfig, AppRegistry, ModuleRegistry

Usage::

    registry = AppRegistry()
    registry.register(my_module_config)
    mod = registry.get("sales")
"""

from hotframe.apps.config import (
    AppConfig,
    MenuConfig,
    ModuleConfig,
    ModuleManifest,
    NavigationItem,
    load_manifest,
    manifest_to_dict,
)
from hotframe.apps.registry import (
    AppRegistry,
    ModuleRegistry,
    RegisteredModule,
)

__all__ = [
    # New contract
    "AppConfig",
    "AppRegistry",
    "MenuConfig",
    "ModuleConfig",
    # Legacy contract (compat durante migracion)
    "ModuleManifest",
    "ModuleRegistry",
    "NavigationItem",
    "RegisteredModule",
    "load_manifest",
    "manifest_to_dict",
]
