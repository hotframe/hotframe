"""
In-memory module registry — the SINGLE source of truth for loaded modules.

The registry holds every module that has been loaded into the Python runtime
during the current process lifetime. It is rebuilt from scratch on every
cold start (from the DB + S3 pipeline).

Thread safety: the registry is accessed from a single asyncio event loop so
a plain dict is sufficient. The ``version`` counter lets caches (menus,
OpenAPI, template loaders) know when to invalidate.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from hotframe.apps.config import AppConfig, ModuleManifest

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RegisteredModule:
    """Snapshot of a loaded module's runtime state."""

    module_id: str
    manifest: ModuleManifest
    router: APIRouter | None = None
    api_router: APIRouter | None = None
    middleware: Any | None = None
    path: Path = field(default_factory=Path)
    loaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ModuleRegistry:
    """
    In-memory registry of loaded modules.

    This is the SINGLE source of truth for what is currently running in the
    Python process. It is NOT persisted — on restart it is rebuilt by
    :class:`~app.modules.runtime.ModuleRuntime.boot`.
    """

    def __init__(self) -> None:
        self._modules: dict[str, RegisteredModule] = {}
        self._version: int = 0

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register(
        self,
        module_id: str,
        manifest: ModuleManifest,
        router: APIRouter | None,
        api_router: APIRouter | None,
        middleware: Any | None,
        path: Path,
    ) -> RegisteredModule:
        """Register a module. Increments the registry version."""
        entry = RegisteredModule(
            module_id=module_id,
            manifest=manifest,
            router=router,
            api_router=api_router,
            middleware=middleware,
            path=path,
        )
        self._modules[module_id] = entry
        self._version += 1
        logger.info("Registered module %s v%s", module_id, manifest.MODULE_VERSION)
        return entry

    def unregister(self, module_id: str) -> None:
        """Remove a module from the registry. Increments version."""
        if module_id in self._modules:
            del self._modules[module_id]
            self._version += 1
            logger.info("Unregistered module %s", module_id)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get(self, module_id: str) -> RegisteredModule | None:
        """Return a registered module or ``None``."""
        return self._modules.get(module_id)

    def get_all(self) -> dict[str, RegisteredModule]:
        """Return all registered modules (read-only view)."""
        return dict(self._modules)

    def is_loaded(self, module_id: str) -> bool:
        """Check if a module is currently loaded."""
        return module_id in self._modules

    def get_loaded_module_ids(self) -> list[str]:
        """Return the IDs of every module currently loaded into the runtime."""
        return list(self._modules.keys())

    # ------------------------------------------------------------------
    # Derived data
    # ------------------------------------------------------------------

    def get_menu_items(self) -> list[dict]:
        """
        Return sorted menu items for the sidebar.

        Only modules with a ``MENU`` config are included. Sorted by
        ``MENU.order`` ascending, then by label alphabetically.
        """
        items: list[dict] = []
        for entry in self._modules.values():
            menu = entry.manifest.MENU
            if menu is not None:
                items.append(
                    {
                        "module_id": entry.module_id,
                        "label": menu.label,
                        "icon": menu.icon,
                        "order": menu.order,
                    }
                )
        items.sort(key=lambda m: (m["order"], m["label"]))
        return items

    def get_navigation(self, module_id: str) -> list[dict]:
        """Return navigation items for a specific module."""
        entry = self._modules.get(module_id)
        if entry is None:
            return []
        return [
            {
                "label": nav.label,
                "icon": nav.icon,
                "id": nav.id,
                "view": nav.view,
            }
            for nav in entry.manifest.NAVIGATION
        ]

    def get_module_middleware(self) -> list[Any]:
        """
        Return all module middleware classes/instances.

        Used by the middleware manager to build the dynamic middleware stack.
        """
        result: list[Any] = []
        for entry in self._modules.values():
            if entry.middleware is not None:
                result.append(entry.middleware)
        return result

    # Alias expected by ``app/core/middleware/module_middleware.py``.
    get_all_middleware = get_module_middleware

    def get_permissions(self, module_id: str) -> list[str]:
        """Return permission strings for a specific module."""
        entry = self._modules.get(module_id)
        if entry is None:
            return []
        return list(entry.manifest.PERMISSIONS)

    def get_all_permissions(self) -> list[str]:
        """Return all permission strings across all loaded modules."""
        perms: list[str] = []
        for entry in self._modules.values():
            perms.extend(entry.manifest.PERMISSIONS)
        return perms

    # ------------------------------------------------------------------
    # Cache busting
    # ------------------------------------------------------------------

    @property
    def version(self) -> int:
        """
        Monotonically increasing counter.

        Increments on every ``register`` / ``unregister``. Consumers (template
        loaders, OpenAPI cache, menu cache) can compare against a stored
        version to decide whether to rebuild.
        """
        return self._version

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def count(self) -> int:
        return len(self._modules)

    def __repr__(self) -> str:
        return f"<ModuleRegistry modules={self.count} version={self._version}>"


# ======================================================================
# AppRegistry (new contract, Fase 3+)
# ======================================================================


class AppRegistry:
    """
    In-memory registry of loaded AppConfig / ModuleConfig instances.

    This is the new contract (Fase 3+). The old ``ModuleRegistry`` (above)
    tracks dynamic modules loaded via the legacy pipeline with
    ``ModuleManifest``. During the migration both registries coexist;
    Fase 4+ will unify them.

    Key difference from ModuleRegistry:
        - Stores AppConfig instances (not manifests + routers separately).
        - Allows both static apps (is_builtin=True, boot-time) and
          dynamic modules (ModuleConfig subclasses).
        - Indexed by config.name.

    Thread-safety: protected by asyncio.Lock.
    """

    def __init__(self) -> None:
        self._apps: dict[str, AppConfig] = {}
        self._lock: asyncio.Lock | None = None  # lazy: only when actually used

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def register(self, config: AppConfig) -> None:
        """Register an AppConfig/ModuleConfig. Raises ValueError on duplicate."""
        async with self._get_lock():
            if config.name in self._apps:
                raise ValueError(f"App {config.name!r} already registered")
            self._apps[config.name] = config

    async def unregister(self, name: str) -> AppConfig | None:
        """Unregister by name. Returns the removed config or None."""
        async with self._get_lock():
            return self._apps.pop(name, None)

    def get(self, name: str) -> AppConfig | None:
        """Non-async read (common path). O(1)."""
        return self._apps.get(name)

    def all(self) -> list[AppConfig]:
        """Snapshot of registered configs."""
        return list(self._apps.values())

    def by_kind(self, *, builtin: bool | None = None) -> list[AppConfig]:
        """
        Filter by kind.
          builtin=True  → is_builtin core apps
          builtin=False → dynamic modules (is_builtin=False)
          builtin=None  → all
        """
        items = self._apps.values()
        if builtin is None:
            return list(items)
        return [c for c in items if c.is_builtin is builtin]

    def __contains__(self, name: str) -> bool:
        return name in self._apps

    def __len__(self) -> int:
        return len(self._apps)
