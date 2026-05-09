"""
Module loader — importlib-based dynamic loading, route mount/unmount.

Handles the full lifecycle of bringing a module's Python code into the
running FastAPI application:

1. Add module path to ``sys.path``
2. ``importlib.import_module`` the package
3. Discover ``routes.py`` (HTML views) and ``api.py`` (REST API)
4. Mount routers on the FastAPI app
5. Discover ``events.py``, ``hooks.py``, ``slots.py`` — register with core registries
6. Discover middleware class from manifest
7. Bust OpenAPI schema cache

Unloading reverses all of the above and purges ``sys.modules``.
"""

from __future__ import annotations

import gc
import importlib
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI

from hotframe.apps.config import ModuleManifest
from hotframe.apps.registry import ModuleRegistry, RegisteredModule
from hotframe.engine.import_manager import ImportManager, PurgeReport
from hotframe.middleware.i18n_support import register_module_locales, unregister_module_locales
from hotframe.middleware.stack_manager import MiddlewareStackManager

if TYPE_CHECKING:
    from hotframe.components.registry import ComponentRegistry
    from hotframe.signals.dispatcher import AsyncEventBus
    from hotframe.signals.hooks import HookRegistry
    from hotframe.templating.slots import SlotRegistry

logger = logging.getLogger(__name__)


class ModuleLoader:
    """
    Loads and unloads module code in the running FastAPI process.

    Operates purely at the Python/FastAPI level — no DB, no S3. Those
    concerns belong to :class:`~hotframe.engine.module_runtime.ModuleRuntime`.
    """

    def __init__(
        self,
        app: FastAPI,
        registry: ModuleRegistry,
        event_bus: AsyncEventBus,
        hooks: HookRegistry,
        slots: SlotRegistry,
        components: ComponentRegistry | None = None,
        import_manager: ImportManager | None = None,
        stack_manager: MiddlewareStackManager | None = None,
    ) -> None:
        self.app = app
        self.registry = registry
        self.bus = event_bus
        self.hooks = hooks
        self.slots = slots
        # Components registry is optional so legacy callers (and the CLI
        # standalone ModuleRuntime instances in management/cli.py) keep
        # working. When provided, the loader calls unregister_module on
        # unload and rollback so components live and die with their module.
        self.components = components
        # ImportManager tracks sys.modules per package so purge is exact
        # instead of prefix-scanning. Keeping the default factory optional
        # lets tests inject a fresh manager per test.
        self.import_manager = import_manager or ImportManager()
        # MiddlewareStackManager rebuilds Starlette's middleware stack
        # atomically when modules add/remove middleware classes. Created
        # per-app so tests can inject stubs.
        self.stack_manager = stack_manager or MiddlewareStackManager(app)
        # Per-module SQLAlchemy metadata footprint — the mapped classes and
        # the ``Table`` objects that the module added to the shared
        # ``Base.metadata``. ``unload_module`` and the ``load_module``
        # rollback branch both call ``_drop_module_metadata`` so reinstall
        # never raises ``Table 'X' is already defined for this MetaData
        # instance``.
        self._module_metadata: dict[str, tuple[list[type[Any]], list]] = {}

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    async def load_module(
        self,
        module_id: str,
        module_path: Path,
        manifest: ModuleManifest,
    ) -> RegisteredModule:
        """
        Load a module into the running application.

        Steps:
            1. Ensure ``module_path.parent`` is in ``sys.path``
            2. ``importlib.import_module(module_id)``
            3. Import ``{module_id}.routes`` → get ``router``
            4. Import ``{module_id}.api`` → get ``api_router``
            5. Mount HTML view router at ``/m/{module_id}``
            6. Mount API router at ``/api/v1/m/{module_id}``
            7. Import ``{module_id}.events`` → call ``register_events(bus, module_id)``
            8. Import ``{module_id}.hooks`` → call ``register_hooks(hooks, module_id)``
            9. Import ``{module_id}.slots`` → call ``register_slots(slots, module_id)``
            10. Import ``{module_id}.services`` → register into SERVICE_REGISTRY
            11. Import middleware class from ``manifest.MIDDLEWARE``
            12. Bust OpenAPI cache
            13. Register in :class:`ModuleRegistry` and return entry

        Args:
            module_id: The module identifier (e.g. ``inventory``).
            module_path: Filesystem path to the module package directory.
            manifest: Validated manifest from ``module.py``.

        Returns:
            The :class:`RegisteredModule` entry.

        Raises:
            ImportError: If the base package cannot be imported.
        """
        # 1 + 2. Import base package via ImportManager (adds sys.path entry
        # and tracks every sys.modules entry that appears so purge can be
        # exact instead of prefix-scanning). If this fails, ImportManager
        # already cleaned up the half-imported state.
        if self.import_manager.get_bundle(module_id) is not None:
            # Reload path: callers invoke ``reload_module`` (unload → load).
            # If we reach here with a lingering bundle, purge first so the
            # import is fresh.
            self.import_manager.purge(module_id)
        self.import_manager.import_package(module_id, module_id, module_path)
        self._register_exported_models(module_id)

        # Track what we've registered so we can roll back on failure
        mounted_routes: list = []
        events_registered = False
        hooks_registered = False
        slots_registered = False
        locales_registered = False
        static_mounted = False
        middleware_added = False
        components_registered = False
        components_router_mounted = False
        components_static_mounted = False

        try:
            # 3. HTML view routes
            router = self._try_import_router(module_id, "routes", "router")
            if router is None:
                logger.warning("Module %s: no view router found in routes.py", module_id)

            # 4. API routes
            api_router = self._try_import_router(module_id, "api", "api_router")

            # 5. Mount view router (check for conflicts first)
            if router is not None:
                from starlette.routing import Mount

                view_path = f"/m/{module_id}"
                if self._route_exists(view_path):
                    raise RuntimeError(
                        f"Route conflict: {view_path} is already mounted by another module"
                    )
                mount = Mount(view_path, app=router)
                self.app.routes.append(mount)
                mounted_routes.append(mount)

            # 6. Mount API router (check for conflicts first)
            if api_router is not None:
                from starlette.routing import Mount

                api_path = f"/api/v1/m/{module_id}"
                if self._route_exists(api_path):
                    raise RuntimeError(
                        f"Route conflict: {api_path} is already mounted by another module"
                    )
                mount = Mount(api_path, app=api_router)
                self.app.routes.append(mount)
                mounted_routes.append(mount)

            # 7. Events
            self._try_register_events(module_id)
            events_registered = True

            # 8. Hooks
            self._try_register_hooks(module_id)
            hooks_registered = True

            # 9. Slots
            self._try_register_slots(module_id)
            slots_registered = True

            # 10. Services (populates SERVICE_REGISTRY)
            self._try_load_services(module_id)

            # 11. Middleware
            middleware_instance = self._try_load_middleware(module_id, manifest)

            # 12. Register module locales for i18n
            locales_dir = module_path / "locales"
            if locales_dir.exists():
                register_module_locales(module_id, locales_dir)
                locales_registered = True

            # 13. Mount module static files at /static/m/{module_id}/
            static_dir = module_path / "static" / module_id
            if static_dir.exists():
                from starlette.staticfiles import StaticFiles

                self.app.mount(
                    f"/static/m/{module_id}",
                    StaticFiles(directory=str(static_dir)),
                    name=f"static-{module_id}",
                )
                static_mounted = True

            # 13b. Discover + mount component routers and scoped static.
            # Components are owned by the module; routers are mounted at
            # /_components/<name>/ and static at /_components/<name>/static.
            if self.components is not None:
                from hotframe.components.discovery import discover_module_components
                from hotframe.components.mounting import (
                    mount_component_routers_for_module,
                    mount_component_static_for_module,
                )

                discovered = discover_module_components(
                    self.components,
                    module_path,
                    module_id,
                )
                if discovered:
                    components_registered = True
                    if mount_component_routers_for_module(self.app, self.components, module_id):
                        components_router_mounted = True
                    if mount_component_static_for_module(self.app, self.components, module_id):
                        components_static_mounted = True

            # 14. Bust OpenAPI cache
            self.app.openapi_schema = None

            # 15. Register
            entry = self.registry.register(
                module_id=module_id,
                manifest=manifest,
                router=router,
                api_router=api_router,
                middleware=middleware_instance,
                path=module_path,
            )

            # 16. Add module middleware to the Starlette stack
            if middleware_instance is not None:
                await self.stack_manager.add_and_rebuild(middleware_instance)
                middleware_added = True

            logger.info(
                "Loaded module %s v%s from %s",
                module_id,
                manifest.MODULE_VERSION,
                module_path,
            )
            return entry

        except Exception:
            # --- Rollback all registered components ---
            logger.error("Module %s load failed — rolling back partial registration", module_id)

            # Remove mounted routes
            for mount in mounted_routes:
                try:
                    self.app.routes.remove(mount)
                except ValueError:
                    pass

            # Unregister events
            if events_registered:
                try:
                    await self.bus.unsubscribe_module(module_id)
                except Exception:
                    pass

            # Remove hooks
            if hooks_registered:
                try:
                    self.hooks.remove_module_hooks(module_id)
                except Exception:
                    pass

            # Remove slots
            if slots_registered:
                try:
                    self.slots.unregister_module(module_id)
                except Exception:
                    pass

            # Remove components registered by the module (symmetric to
            # the slot cleanup above). Safe to call even if the module
            # never registered any components. Mounts MUST come off
            # before ``unregister_module`` wipes the module->name map
            # the mounting helpers use to resolve paths.
            if self.components is not None:
                if components_router_mounted:
                    from hotframe.components.mounting import (
                        unmount_component_routers_for_module,
                    )

                    try:
                        unmount_component_routers_for_module(self.app, module_id)
                    except Exception:
                        pass
                if components_static_mounted:
                    from hotframe.components.mounting import (
                        unmount_component_static_for_module,
                    )

                    try:
                        unmount_component_static_for_module(self.app, module_id)
                    except Exception:
                        pass
                if components_registered:
                    try:
                        self.components.unregister_module(module_id)
                    except Exception:
                        pass

            # Unregister services
            from hotframe.apps.service_facade import unregister_module_services

            try:
                unregister_module_services(module_id)
            except Exception:
                pass

            # Drop any HTTP clients the module registered before the
            # failure — symmetric to the unload_module path, so a
            # half-loaded module never leaves a named client behind
            # pointing at a dead import.
            http_clients = getattr(self.app.state, "http_clients", None)
            if http_clients is not None:
                try:
                    await http_clients.unregister_module(module_id)
                except Exception:
                    logger.exception(
                        "Failed to unregister HTTP clients for module %s during rollback",
                        module_id,
                    )

            # Unregister locales
            if locales_registered:
                try:
                    unregister_module_locales(module_id)
                except Exception:
                    pass

            # Remove middleware from Starlette stack
            if middleware_added and middleware_instance is not None:
                try:
                    await self.stack_manager.remove_and_rebuild(middleware_instance)
                except Exception:
                    pass

            # Remove static mount
            if static_mounted:
                static_mount_path = f"/static/m/{module_id}"
                self.app.routes[:] = [
                    route
                    for route in self.app.routes
                    if getattr(route, "path", None) != static_mount_path
                ]

            # Drop SQLAlchemy metadata BEFORE purging sys.modules so the
            # next install of the same module does not fail with
            # ``Table 'X' is already defined for this MetaData instance``.
            self._drop_module_metadata(module_id)

            # Purge sys.modules via ImportManager (exact, weakref-checked)
            self._purge_module(module_id)

            # Bust OpenAPI cache
            self.app.openapi_schema = None

            raise

    # ------------------------------------------------------------------
    # Unload
    # ------------------------------------------------------------------

    async def unload_module(self, module_id: str) -> None:
        """
        Unload a module from the running application.

        Steps:
            1. Remove routes matching ``/m/{module_id}`` and ``/api/v1/m/{module_id}``
            2. ``bus.unsubscribe_module(module_id)``
            3. ``hooks.remove_module_hooks(module_id)``
            4. ``slots.unregister_module(module_id)``
            5. Purge ``sys.modules`` entries for the module
            6. Unregister from :class:`ModuleRegistry`
            7. Bust OpenAPI cache
        """
        # 1. Remove routes
        self._remove_routes(module_id)

        # 2. Events
        await self.bus.unsubscribe_module(module_id)

        # 3. Hooks
        self.hooks.remove_module_hooks(module_id)

        # 4. Slots
        self.slots.unregister_module(module_id)

        # 4b. Components (mirror of slots; skipped if no registry was
        # injected, e.g. legacy CLI code paths). Unmount routers and
        # static BEFORE the registry entries are dropped so the helpers
        # can still resolve module->component-name mappings.
        if self.components is not None:
            from hotframe.components.mounting import (
                unmount_component_routers_for_module,
                unmount_component_static_for_module,
            )

            unmount_component_routers_for_module(self.app, module_id)
            unmount_component_static_for_module(self.app, module_id)
            self.components.unregister_module(module_id)

        # 4c. HTTP clients — drop every client the module registered as
        # a safety net in case the module forgot to unregister them in
        # its deactivate hook. Mirrors the slot and component teardown
        # above: named resources owned by a module die with the module.
        http_clients = getattr(self.app.state, "http_clients", None)
        if http_clients is not None:
            try:
                await http_clients.unregister_module(module_id)
            except Exception:
                logger.exception(
                    "Failed to unregister HTTP clients for module %s during unload",
                    module_id,
                )

        # 5. Unregister module locales
        unregister_module_locales(module_id)

        # 6. Unmount module static files
        static_mount_path = f"/static/m/{module_id}"
        self.app.routes[:] = [
            route for route in self.app.routes if getattr(route, "path", None) != static_mount_path
        ]

        # 7. Unregister services
        from hotframe.apps.service_facade import unregister_module_services

        unregister_module_services(module_id)

        # 8. Drop the module's SQLAlchemy metadata footprint BEFORE we
        # purge sys.modules — once the module classes lose their last
        # reference SQLAlchemy can no longer dispose them cleanly.
        self._drop_module_metadata(module_id)

        # 9. Purge sys.modules via ImportManager (exact + weakref check
        # flags zombie classes that were kept alive by caches elsewhere).
        report = self._purge_module(module_id)
        if report is not None and report.zombie_classes:
            logger.warning(
                "Module %s unloaded with %d zombie class(es): %s",
                module_id,
                len(report.zombie_classes),
                ", ".join(report.zombie_classes),
            )

        # 9b. Second-pass cleanup: if tables of this module still live in
        # Base.metadata (because the first drop ran before purge and the
        # purge revealed more references), remove them explicitly so the
        # next install does not fail with 'Table x is already defined'.
        leftover = self._verify_metadata_cleared(module_id)
        if leftover:
            from hotframe.models.base import Base

            logger.warning(
                "Module %s left %d table(s) in Base.metadata after purge: %s — forcing cleanup",
                module_id,
                len(leftover),
                ", ".join(leftover),
            )
            for table_name in leftover:
                table = Base.metadata.tables.get(table_name)
                if table is None:
                    continue
                for mapper in list(Base.registry.mappers):
                    if mapper.local_table is table:
                        try:
                            Base.registry._dispose_cls(mapper.class_)
                        except Exception:
                            logger.warning(
                                "Forced registry dispose failed for %s (module=%s)",
                                mapper.class_.__name__,
                                module_id,
                                exc_info=True,
                            )
                try:
                    Base.metadata.remove(table)
                except Exception:
                    logger.warning(
                        "Forced Base.metadata.remove failed for table %s (module=%s)",
                        table_name,
                        module_id,
                        exc_info=True,
                    )
            still_leftover = self._verify_metadata_cleared(module_id)
            if still_leftover:
                logger.error(
                    "Module %s: %d table(s) still registered after forced cleanup: %s — reinstall will likely fail",
                    module_id,
                    len(still_leftover),
                    ", ".join(still_leftover),
                )

        # 8b. Remove module middleware from Starlette stack
        entry = self.registry.get(module_id)
        if entry is not None and entry.middleware is not None:
            await self.stack_manager.remove_and_rebuild(entry.middleware)
            # Leak B mitigation (doc 05 §3.5): rebuilding the stack creates
            # a fresh chain of bound methods; the previous chain's closures
            # may keep the old middleware instance alive through reference
            # cycles (BaseHTTPMiddleware -> ASGIApp -> Starlette internals).
            # An explicit collection right after rebuild gives those cycles
            # a chance to be reclaimed before the test's RSS measurement.
            gc.collect()

        # 9. Unregister
        self.registry.unregister(module_id)

        # 10. Bust OpenAPI cache
        self.app.openapi_schema = None

        # Leak C mitigation (doc 05 §3.5): trigger one more collection now
        # that ``self.registry`` and ``self._module_metadata`` no longer
        # hold strong references to the module's classes. Without this,
        # SQLAlchemy mapper internals can keep the class graph alive
        # across reinstalls and RSS grows linearly.
        gc.collect()

        logger.info("Unloaded module %s", module_id)

    # ------------------------------------------------------------------
    # Reload
    # ------------------------------------------------------------------

    async def reload_module(
        self,
        module_id: str,
        module_path: Path,
        manifest: ModuleManifest,
    ) -> RegisteredModule:
        """Unload + load. Designed for dev hot-reload."""
        await self.unload_module(module_id)
        return await self.load_module(module_id, module_path, manifest)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _try_import_router(
        module_id: str,
        submodule: str,
        attr: str,
    ) -> APIRouter | None:
        """Try to import ``{module_id}.{submodule}`` and return ``getattr(mod, attr)``."""
        fqn = f"{module_id}.{submodule}"
        try:
            mod = importlib.import_module(fqn)
            router = getattr(mod, attr, None)
            if router is not None and isinstance(router, APIRouter):
                return router
            # Fallback: look for a plain ``router`` attribute
            if attr != "router":
                router = getattr(mod, "router", None)
                if router is not None and isinstance(router, APIRouter):
                    return router
            return None
        except ModuleNotFoundError as e:
            logger.warning("Module %s not found: %s", fqn, e)
            return None
        except Exception:
            logger.exception("Error importing %s", fqn)
            return None

    def _try_register_events(self, module_id: str) -> None:
        """Import ``{module_id}.events`` and call ``register_events(bus, module_id)``."""
        fqn = f"{module_id}.events"
        try:
            mod = importlib.import_module(fqn)
            register_fn = getattr(mod, "register_events", None)
            if register_fn is not None:
                register_fn(self.bus, module_id)
        except ModuleNotFoundError:
            pass
        except Exception:
            logger.exception("Error registering events for %s", module_id)

    def _try_register_hooks(self, module_id: str) -> None:
        """Import ``{module_id}.hooks`` and call ``register_hooks(hooks, module_id)``."""
        fqn = f"{module_id}.hooks"
        try:
            mod = importlib.import_module(fqn)
            register_fn = getattr(mod, "register_hooks", None)
            if register_fn is not None:
                register_fn(self.hooks, module_id)
        except ModuleNotFoundError:
            pass
        except Exception:
            logger.exception("Error registering hooks for %s", module_id)

    def _try_register_slots(self, module_id: str) -> None:
        """Import ``{module_id}.slots`` and call ``register_slots(slots, module_id)``."""
        fqn = f"{module_id}.slots"
        try:
            mod = importlib.import_module(fqn)
            register_fn = getattr(mod, "register_slots", None)
            if register_fn is not None:
                register_fn(self.slots, module_id)
        except ModuleNotFoundError:
            pass
        except Exception:
            logger.exception("Error registering slots for %s", module_id)

    @staticmethod
    def _try_load_services(module_id: str) -> bool:
        """Import ``{module_id}.services`` and register ModuleService subclasses.

        Returns True if at least one service was registered.
        """
        from hotframe.apps.service_facade import register_services

        try:
            count = register_services(module_id)
            return count > 0
        except Exception:
            logger.exception("Error loading services for %s", module_id)
            return False

    @staticmethod
    def _try_load_middleware(
        module_id: str,
        manifest: ModuleManifest,
    ) -> Any | None:
        """
        Load the middleware class specified in ``manifest.MIDDLEWARE``.

        The string format is ``{module_id}.middleware.ClassName``.
        """
        if not manifest.MIDDLEWARE:
            return None
        try:
            parts = manifest.MIDDLEWARE.rsplit(".", 1)
            if len(parts) != 2:
                logger.warning(
                    "Invalid MIDDLEWARE format for %s: %s (expected 'module.ClassName')",
                    module_id,
                    manifest.MIDDLEWARE,
                )
                return None
            mod_path, cls_name = parts
            mod = importlib.import_module(mod_path)
            cls = getattr(mod, cls_name, None)
            if cls is None:
                logger.warning(
                    "Middleware class %s not found in %s",
                    cls_name,
                    mod_path,
                )
                return None
            return cls
        except Exception:
            logger.exception("Error loading middleware for %s", module_id)
            return None

    def _route_exists(self, path: str) -> bool:
        """Check if a route path is already mounted."""
        return any(getattr(route, "path", None) == path for route in self.app.routes)

    def _remove_routes(self, module_id: str) -> None:
        """Remove all routes matching the module prefixes from the app."""
        view_prefix = f"/m/{module_id}"
        api_prefix = f"/api/v1/m/{module_id}"

        self.app.routes[:] = [
            route
            for route in self.app.routes
            if not _route_matches_prefix(route, view_prefix)
            and not _route_matches_prefix(route, api_prefix)
        ]

    def _register_exported_models(self, module_id: str) -> None:
        """Register SQLAlchemy model classes from the module for zombie detection.

        Also records the ``Table`` objects that the module contributed to
        ``Base.metadata`` so ``_drop_module_metadata`` can roll back the
        registration on unload (otherwise reinstall raises
        ``Table 'X' is already defined for this MetaData instance``).
        """
        fqn = f"{module_id}.models"
        mod = sys.modules.get(fqn)
        if mod is None:
            return
        from hotframe.models.base import Base

        classes: list[type] = []
        tables: list = []
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name, None)
            if isinstance(obj, type) and issubclass(obj, Base) and obj is not Base:
                self.import_manager.register_exported_class(module_id, obj)
                classes.append(obj)
                tbl = getattr(obj, "__table__", None)
                if tbl is not None:
                    tables.append(tbl)

        # Merge with anything we already tracked (multiple imports of the
        # same module are tolerated — set semantics on classes, dedup on
        # the Table objects which are identity-comparable).
        prev_classes, prev_tables = self._module_metadata.get(module_id, ([], []))
        merged_classes = list({id(c): c for c in (prev_classes + classes)}.values())
        merged_tables = list({id(t): t for t in (prev_tables + tables)}.values())
        self._module_metadata[module_id] = (merged_classes, merged_tables)

    def _drop_module_metadata(self, module_id: str) -> None:
        """Remove the module's tables from ``Base.metadata`` and dispose mappers.

        Called from :meth:`unload_module` and the failure-rollback branch
        of :meth:`load_module` so the next install of the same module
        does not collide with leftover ``Base.metadata`` registrations.

        ``MetaData.remove(table)`` is the documented inverse of table
        registration; ``registry._dispose_cls`` is SQLAlchemy 2.0's
        official tear-down for a mapped class. Both are best-effort —
        any exception is logged at warning level and swallowed because the
        unload path must remain robust. Warnings (not debug) because a
        silent failure here causes ``Table 'x' is already defined`` on the
        next install of the same module.
        """
        from hotframe.models.base import Base

        classes, tables = self._module_metadata.pop(module_id, ([], []))

        # Drop tables from MetaData first so any subsequent mapper dispose
        # cannot re-trigger their indexes/constraints.
        for tbl in tables:
            try:
                Base.metadata.remove(tbl)
            except KeyError:
                # Already removed — safe to ignore.
                pass
            except Exception:
                logger.warning(
                    "Best-effort: could not remove table %r from Base.metadata (module=%s)",
                    getattr(tbl, "name", tbl),
                    module_id,
                    exc_info=True,
                )

        # Dispose mapper + drop the class from the registry.
        for cls in classes:
            try:
                mapper = cls.__mapper__
            except Exception:
                continue
            try:
                mapper._dispose()
            except Exception:
                logger.warning(
                    "Best-effort: mapper dispose failed for %s (module=%s)",
                    cls.__name__,
                    module_id,
                    exc_info=True,
                )
            try:
                Base.registry._dispose_cls(cls)
            except Exception:
                logger.warning(
                    "Best-effort: registry dispose failed for %s (module=%s)",
                    cls.__name__,
                    module_id,
                    exc_info=True,
                )

    def _purge_module(self, module_id: str) -> PurgeReport | None:
        """Purge the module via ``ImportManager`` and report zombies.

        Returns ``None`` if the module was not tracked (legacy path, or
        load failed before import). Falls back to prefix-based purge to
        guarantee cleanup.
        """
        if self.import_manager.get_bundle(module_id) is not None:
            report = self.import_manager.purge(module_id)
            logger.debug(
                "Purged %d sys.modules entries for %s (zombies=%d)",
                report.purged_count,
                module_id,
                len(report.zombie_classes),
            )
            return report

        # Fallback: untracked module (e.g. imported outside ImportManager).
        prefix = f"{module_id}."
        keys_to_remove = [key for key in sys.modules if key == module_id or key.startswith(prefix)]
        for key in keys_to_remove:
            del sys.modules[key]
        if keys_to_remove:
            logger.debug(
                "Fallback-purged %d sys.modules entries for %s (untracked)",
                len(keys_to_remove),
                module_id,
            )
        return None

    def _verify_metadata_cleared(self, module_id: str) -> list[str]:
        """Return names of tables from ``module_id`` still in ``Base.metadata``.

        Called after ``_drop_module_metadata`` + ``_purge_module`` to detect
        leftover registrations that would collide on reinstall with
        ``Table 'x' is already defined``.
        """
        from hotframe.models.base import Base

        prefix = f"{module_id}."
        leftover: list[str] = []
        for table_name, table in list(Base.metadata.tables.items()):
            info_module = getattr(table, "info", {}).get("module_id")
            if info_module == module_id:
                leftover.append(table_name)
                continue
            cls_module: str | None = None
            for mapper in Base.registry.mappers:
                if mapper.local_table is table:
                    cls_module = mapper.class_.__module__
                    break
            if cls_module and (cls_module == module_id or cls_module.startswith(prefix)):
                leftover.append(table_name)
        return leftover


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------


def _import_fresh(module_id: str) -> Any:
    """Import (or re-import) a module, ensuring fresh code is loaded."""
    if module_id in sys.modules:
        return importlib.reload(sys.modules[module_id])
    return importlib.import_module(module_id)


def _route_matches_prefix(route: Any, prefix: str) -> bool:
    """Check if a route object has a path starting with the given prefix."""
    route_path = getattr(route, "path", None)
    if route_path is not None:
        return route_path == prefix or route_path.startswith(prefix + "/")
    return False
