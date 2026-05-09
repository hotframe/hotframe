"""
Module Runtime — the central orchestrator for the entire module system.

This is the HEART of hotframe. It ties together:
- **Registry** — in-memory state of loaded modules
- **Loader** — importlib + FastAPI route mount/unmount
- **StateDB** — hub_module table CRUD
- **S3Source** — download + cache + verify
- **DependencyManager** — topological sort + protection
- **LifecycleManager** — on_install/activate/deactivate/uninstall hooks
- **MigrationRunner** — per-module Alembic
- **Watcher** — dev hot-reload

Every module operation (install, activate, deactivate, uninstall, update)
goes through this class. The REST API endpoints and the HTML view
handlers call the same methods — this is the shared business logic
layer.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from hotframe.apps.config import load_manifest, manifest_to_dict
from hotframe.apps.registry import ModuleRegistry
from hotframe.dev.autoreload import ModuleWatcher
from hotframe.engine.dependency import DependencyManager
from hotframe.engine.lifecycle import ModuleLifecycleManager
from hotframe.engine.loader import ModuleLoader
from hotframe.engine.pipeline import HotMountPipeline, PhaseResult
from hotframe.engine.s3_source import S3ModuleSource
from hotframe.engine.state import ModuleStateDB, _get_module_model
from hotframe.migrations.runner import ModuleMigrationRunner

if TYPE_CHECKING:
    from fastapi import FastAPI

    from hotframe.components.registry import ComponentRegistry
    from hotframe.config.settings import HotframeSettings
    from hotframe.db.protocols import ISession
    from hotframe.signals.dispatcher import AsyncEventBus
    from hotframe.signals.hooks import HookRegistry
    from hotframe.templating.slots import SlotRegistry

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Result dataclasses
# ------------------------------------------------------------------


@dataclass
class InstallResult:
    """Result of a module install operation."""

    success: bool = False
    module_id: str = ""
    version: str = ""
    error: str | None = None
    auto_installed: list[str] = field(default_factory=list)


@dataclass
class ActivateResult:
    """Result of a module activate operation."""

    success: bool = False
    module_id: str = ""
    error: str | None = None


@dataclass
class DeactivateResult:
    """Result of a module deactivate operation."""

    success: bool = False
    module_id: str = ""
    error: str | None = None
    dependents: list[str] = field(default_factory=list)
    cascade_order: list[str] = field(default_factory=list)
    cascaded: list[str] = field(default_factory=list)


@dataclass
class UninstallResult:
    """Result of a module uninstall operation."""

    success: bool = False
    module_id: str = ""
    error: str | None = None
    dependents: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class UpdateResult:
    """Result of a module update operation."""

    success: bool = False
    module_id: str = ""
    from_version: str = ""
    to_version: str = ""
    error: str | None = None


# ------------------------------------------------------------------
# ModuleRuntime
# ------------------------------------------------------------------


class ModuleRuntime:
    """
    Central orchestrator for the module plugin system.

    Ties together all sub-systems (registry, loader, state, S3, deps,
    lifecycle, migrations) into a cohesive API used by both REST
    endpoints and HTML view handlers.

    Usage::

        runtime = ModuleRuntime(app, settings, event_bus, hooks, slots)
        await runtime.boot(session, hub_id)
        result = await runtime.install(session, hub_id, "my_module")
    """

    def __init__(
        self,
        app: FastAPI,
        settings: HotframeSettings,
        event_bus: AsyncEventBus,
        hooks: HookRegistry,
        slots: SlotRegistry,
        components: ComponentRegistry | None = None,
    ) -> None:
        """Initialize all sub-systems; S3 source is created only when MODULE_SOURCE='s3'.

        Args:
            app: The FastAPI application instance.
            settings: Hotframe settings carrying all configuration.
            event_bus: Async event bus for emitting lifecycle events.
            hooks: Hook registry for action/filter callbacks.
            slots: Slot registry for cross-module UI injection.
            components: Optional component registry. When provided, the
                module loader will drop the module's components on unload
                and rollback, symmetric to slot teardown.
        """
        self.app = app
        self.settings = settings
        self.bus = event_bus
        self.hooks = hooks
        self.slots = slots
        self.components = components

        # Sub-systems
        self.registry = ModuleRegistry()
        self.loader = ModuleLoader(
            app,
            self.registry,
            event_bus,
            hooks,
            slots,
            components=components,
        )
        self.state = ModuleStateDB()
        self.s3 = None
        if settings.MODULE_SOURCE == "s3" and settings.S3_MODULES_BUCKET:
            self.s3 = S3ModuleSource(
                bucket=settings.S3_MODULES_BUCKET,
                cache_dir=settings.MODULES_CACHE_DIR,
                region=settings.AWS_REGION,
            )
        self.deps = DependencyManager()
        self.lifecycle = ModuleLifecycleManager()
        self.migrations = ModuleMigrationRunner()
        self.watcher = ModuleWatcher()

    # ------------------------------------------------------------------
    # Boot (cold start)
    # ------------------------------------------------------------------

    async def boot(
        self,
        session: ISession,
        hub_id: UUID,
        skip_db_writes: bool = False,
    ) -> None:
        """
        Startup sequence: read DB → download from S3 → load all active.

        Called once during FastAPI lifespan startup.

        The /app/modules/ directory starts EMPTY on a new container.
        DB (hub_module) is the source of truth for what's installed.
        S3 is the source of truth for module code.

        Steps:
            1. Load cached S3 ETags from disk
            2. Query DB for active modules
            3. For each active module, ensure code exists at /app/modules/{id}/
               — if not, download from S3 and extract there
            4. Resolve dependency order (topological sort)
            5. Load each module via importlib + route mount
            6. Start dev watcher if DEBUG=True

        Args:
            session: Async SQLAlchemy session.
            hub_id: Tenant UUID (or ``None`` for single-tenant).
            skip_db_writes: When ``True``, the boot loop still mounts every
                module's routes into the FastAPI app (each worker needs its
                own in-memory routes) but NEVER issues any ``UPDATE`` or
                ``DELETE`` against ``hub_module``. Set by
                :meth:`boot_all_active_modules` on worker processes that
                lost the per-hub advisory lock, so only one worker writes
                to the shared row at boot and the rest no longer race into
                Postgres deadlocks on ``hub_module.manifest``.
        """
        start = time.monotonic()

        # 1. Restore ETag cache (only if S3 source is configured)
        if self.s3 is not None:
            self.s3.load_cached_etags()

        # 2. Query active modules
        active_modules = await self.state.get_active_modules(session, hub_id=hub_id)

        # Skip modules already loaded (e.g. via hot-reload).
        active_modules = [m for m in active_modules if not self.registry.is_loaded(m.module_id)]

        if not active_modules:
            logger.info("No active modules to load for hub %s", hub_id)
            return

        logger.info(
            "Booting %d active modules for hub %s",
            len(active_modules),
            hub_id,
        )

        # 2b. Resolve empty version from catalog (latest)
        from sqlalchemy import select

        HubModuleVersion = _get_module_version_model()

        boot_candidates = []
        for mod in active_modules:
            if not mod.version:
                if HubModuleVersion is not None:
                    cat_result = await session.execute(
                        select(HubModuleVersion)
                        .where(HubModuleVersion.module_id == mod.module_id)
                        .order_by(HubModuleVersion.released_at.desc())
                        .limit(1)
                    )
                    cat = cat_result.scalar_one_or_none()
                    if cat:
                        mod.version = cat.version
                        if not skip_db_writes:
                            await session.flush()
                        logger.info("Resolved %s to v%s", mod.module_id, mod.version)
                    else:
                        logger.warning(
                            "Skipping active module %s: version not found in catalog",
                            mod.module_id,
                        )
                        if not skip_db_writes:
                            await self.state.set_error(
                                session,
                                mod.module_id,
                                "Module version not found in catalog",
                                hub_id=hub_id,
                            )
                        continue
                else:
                    logger.warning(
                        "Skipping active module %s: version is empty and no catalog model configured",
                        mod.module_id,
                    )
                    if not skip_db_writes:
                        await self.state.set_error(
                            session,
                            mod.module_id,
                            "Module version not found in catalog",
                            hub_id=hub_id,
                        )
                    continue
            boot_candidates.append(mod)

        # 3. Download from S3 (parallel)
        paths = await self._ensure_module_code(boot_candidates)

        # 3b. Module migrations are handled by core Alembic migrations
        # (system modules) or by the install flow (user-installed modules).

        # 4. Resolve load order
        load_items = []
        for mod in boot_candidates:
            if mod.module_id not in paths:
                logger.error(
                    "Module %s code not available — setting to error",
                    mod.module_id,
                )
                if not skip_db_writes:
                    await self.state.set_error(
                        session,
                        mod.module_id,
                        "Module code not available after S3 download",
                        hub_id=hub_id,
                    )
                continue
            load_items.append(
                {
                    "module_id": mod.module_id,
                    "manifest": mod.manifest,
                    "path": paths[mod.module_id],
                    "version": mod.version,
                }
            )

        ordered = self.deps.resolve_load_order(load_items)

        # 5. Load each module
        loaded = 0
        for item in ordered:
            success = await self._load_from_path(
                session,
                hub_id,
                item["module_id"],
                item["path"],
                skip_db_writes=skip_db_writes,
            )
            if success:
                loaded += 1

        elapsed = (time.monotonic() - start) * 1000
        logger.info(
            "Boot complete: %d/%d modules loaded in %.0fms for hub %s",
            loaded,
            len(active_modules),
            elapsed,
            hub_id,
        )

        # 6. Dev watcher
        if self.settings.DEBUG:
            await self.watcher.start(
                self.settings.MODULES_DIR,
                lambda mid: self.hot_reload(mid),
            )

    async def boot_all_active_modules(self, session: ISession) -> int:
        """
        Mount every DB-active module's router into the running FastAPI app.

        Called once during FastAPI lifespan startup, after :class:`ModuleRuntime`
        has been constructed. Without this call, modules persisted with
        ``status='active'`` survive the DB but their HTTP routes at
        ``/m/<module_id>/`` return 404 until the user manually re-activates
        from the marketplace UI.

        Auto-detects multi-tenant vs. single-tenant deployments by inspecting
        the model configured via ``settings.MODULE_STATE_MODEL``:

        - Model has a ``hub_id`` column → iterate distinct hub_ids and call
          :meth:`boot` for each (multi-tenant / hub-aware projects).
        - Model has no ``hub_id`` column → call :meth:`boot` once with
          ``hub_id=None`` (framework built-in ``Module`` model).

        Failures on one hub do NOT abort the remaining hubs — each is logged
        and the next is attempted so a single broken tenant cannot brick
        startup for every tenant on a shared process.

        Concurrency (multi-worker deploys)
        ---------------------------------
        When uvicorn/gunicorn runs with ``--workers N``, every worker is a
        separate process that runs this method on startup. Each worker must
        still mount the module routes into its own in-memory FastAPI app,
        otherwise requests routed to that worker would 404. But only ONE
        worker should write to the shared ``hub_module`` row — multiple
        workers issuing concurrent ``UPDATE hub_module SET manifest=…``
        for the same ``module_id`` deadlock in Postgres and flip random
        modules to ``status='error'``.

        We serialize the DB-write side with a Postgres session-level
        advisory lock keyed by the hub UUID. The first worker to acquire
        the lock for a given hub performs the full boot (routes + DB
        writes). Losing workers still mount routes into their own process
        but skip every ``UPDATE``/``set_error`` against ``hub_module``.
        On non-Postgres backends (SQLite in tests) the lock is a no-op.

        Args:
            session: Async SQLAlchemy session used for all DB work.

        Returns:
            The total number of ``(hub_id, module)`` pairs the boot
            pipeline attempted to load (sum across hubs).
        """
        from sqlalchemy import select

        Model = self.state._model()
        has_hub_column = hasattr(Model, "hub_id")

        if not has_hub_column:
            # Single-tenant / framework-default: one boot pass, no filter.
            active = await self.state.get_active_modules(session)
            if not active:
                logger.info("No active modules to boot")
                return 0
            acquired = await self._try_acquire_boot_lock(session, hub_id=None)
            if not acquired:
                logger.info(
                    "Another worker holds the boot lock — mounting routes locally without DB writes"
                )
            await self.boot(session, hub_id=None, skip_db_writes=not acquired)  # type: ignore[arg-type]
            return len(active)

        # Multi-tenant: one boot pass per distinct hub with active modules.
        stmt = select(Model.hub_id).where(Model.status == "active").distinct()
        result = await session.execute(stmt)
        hub_ids = [row[0] for row in result.all() if row[0] is not None]

        if not hub_ids:
            logger.info("No hubs with active modules to boot")
            return 0

        total = 0
        for hub_id in hub_ids:
            try:
                active = await self.state.get_active_modules(session, hub_id=hub_id)
                acquired = await self._try_acquire_boot_lock(session, hub_id=hub_id)
                if not acquired:
                    logger.info(
                        "Hub %s: another worker holds the boot lock — "
                        "mounting routes locally without DB writes",
                        hub_id,
                    )
                await self.boot(session, hub_id, skip_db_writes=not acquired)
                total += len(active)
            except Exception:
                logger.exception(
                    "Boot failed for hub %s — skipping so other hubs still boot",
                    hub_id,
                )

        logger.info(
            "Boot pass complete: attempted %d module(s) across %d hub(s)", total, len(hub_ids)
        )
        return total

    async def _try_acquire_boot_lock(
        self,
        session: ISession,
        hub_id: UUID | None,
    ) -> bool:
        """
        Try to acquire a Postgres transaction-level advisory lock keyed by
        the hub so only one uvicorn worker performs DB writes during boot.

        Returns ``True`` if the lock was acquired (or the backend is not
        Postgres — SQLite in tests has no equivalent and does not need
        one, because tests run single-process). Returns ``False`` when
        another worker already holds the lock, in which case the caller
        must skip DB writes to avoid deadlocking on ``hub_module``.

        The lock is ``pg_try_advisory_xact_lock`` so it releases
        automatically at the end of the enclosing transaction — no
        explicit unlock call is required.
        """
        from sqlalchemy import text

        # Detect backend: on non-Postgres (SQLite in unit tests) skip the
        # lock entirely. The race this guards against only manifests when
        # multiple worker processes share a Postgres row.
        try:
            bind = session.get_bind()  # type: ignore[attr-defined]
            dialect_name = bind.dialect.name
        except Exception:
            # Older/fake sessions may not expose get_bind — be permissive
            # and assume no lock support; this matches SQLite behavior.
            return True

        if dialect_name != "postgresql":
            return True

        # Stable 64-bit key derived from the hub id (or the sentinel
        # "__global__" for single-tenant projects). We fold to a signed
        # 64-bit range to fit Postgres' advisory lock signature.
        key_source = str(hub_id) if hub_id is not None else "__global__"
        key = _hub_id_to_advisory_key(key_source)

        try:
            result = await session.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": key},
            )
            row = result.first()
            return bool(row[0]) if row is not None else False
        except Exception:
            # If the lock call itself fails (connection issue, etc.) fall
            # back to the old behavior — let this worker attempt the
            # writes. Worst case we reintroduce the original race, which
            # the follower would also have hit.
            logger.exception(
                "Advisory lock check failed for hub %s — continuing without serialization",
                hub_id,
            )
            return True

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------

    async def install(
        self,
        session: ISession,
        hub_id: UUID | None,
        module_id: str,
        version: str | None = None,
        checksum: str = "",
        source: str | None = None,
        auto_install_deps: bool = False,
        installed_by: UUID | None = None,
    ) -> InstallResult:
        """
        Full install orchestrated as a :class:`HotMountPipeline`.

        Each step is a named phase with its own :class:`RollbackHandle`.
        If any phase raises, :meth:`HotMountPipeline.rollback` undoes
        every previously completed phase in LIFO order.

        Phases:
            DOWNLOADING → VALIDATING (manifest + canonical rename)
            → VALIDATING (dep check) → MIGRATING (DB row + alembic upgrade)
            → IMPORTING (on_install hook) → MOUNTING (loader + templates)
            → STACK_REBUILD (on_activate + DB activate) → ACTIVE (commit).

        Args:
            session: Async SQLAlchemy session (must be flushed/committed by caller).
            hub_id: Tenant UUID; pass ``None`` for hub-less (global) installs.
            module_id: Module identifier as it appears in the catalog or filesystem.
            version: Specific version to install; resolved from marketplace when omitted.
            checksum: Expected SHA-256 of the archive for integrity verification.
            source: Explicit URL or local ``.zip`` path; bypasses marketplace resolution.
            auto_install_deps: When ``True``, inactive dependencies are allowed (installed externally).
            installed_by: UUID of the user triggering the install, stored for audit.

        Returns:
            :class:`InstallResult` with ``success``, final ``module_id``, ``version``, and ``error``.
        """

        result = InstallResult(module_id=module_id, version=version or "")

        # Pre-check (outside the pipeline — no side effects yet).
        if hub_id is not None:
            existing = await self.state.get_module(session, module_id, hub_id=hub_id)
            if existing is not None:
                result.error = f"Module {module_id} is already installed (status={existing.status})"
                return result

        pipeline = HotMountPipeline(module_id=module_id)
        manifest = None
        module_path: Path | None = None
        # Mutable context so phases can propagate a canonical module_id change.
        ctx: dict[str, Any] = {"module_id": module_id}

        try:
            download = await pipeline.run_phase(
                "DOWNLOADING",
                self._phase_download,
                module_id,
                version,
                checksum,
                source,
            )
            module_path = download.payload["module_path"]
            # Propagate resolved version/checksum from source resolution
            if "version" in download.payload:
                version = download.payload["version"]
            if "checksum" in download.payload:
                checksum = download.payload["checksum"]

            validate = await pipeline.run_phase(
                "VALIDATING",
                self._phase_validate,
                session,
                hub_id,
                module_id,
                version,
                module_path,
            )
            manifest = validate.payload["manifest"]
            module_path = validate.payload["module_path"]
            ctx["module_id"] = validate.payload["module_id"]
            module_id = ctx["module_id"]
            result.module_id = module_id

            await pipeline.run_phase(
                "VALIDATING",
                self._phase_check_deps,
                session,
                hub_id,
                manifest,
                auto_install_deps,
            )

            await pipeline.run_phase(
                "MIGRATING",
                self._phase_migrate,
                session,
                hub_id,
                module_id,
                version,
                checksum,
                installed_by,
                manifest,
                module_path,
            )

            await pipeline.run_phase(
                "IMPORTING",
                self._phase_on_install,
                session,
                hub_id,
                module_id,
            )

            await pipeline.run_phase(
                "MOUNTING",
                self._phase_mount,
                module_id,
                module_path,
                manifest,
            )

            await pipeline.run_phase(
                "STACK_REBUILD",
                self._phase_activate,
                session,
                hub_id,
                module_id,
                manifest,
            )

            await pipeline.commit()

            # Event is outside the pipeline — observers must not be able
            # to block the install, and there is nothing to "undo".
            await self.bus.emit(
                "module.installed",
                sender=self,
                module_id=module_id,
                version=version,
                hub_id=hub_id,
            )

            result.success = True
            logger.info(
                "Installed module %s v%s for hub %s",
                module_id,
                version,
                hub_id,
            )

        except Exception as e:
            logger.exception(
                "Install failed for %s v%s at phase %s",
                ctx["module_id"],
                version,
                pipeline.state.current_phase,
            )
            result.error = str(e)
            rollback_errors = await pipeline.rollback()
            if rollback_errors:
                logger.error(
                    "Rollback had %d error(s) during install cleanup of %s",
                    len(rollback_errors),
                    ctx["module_id"],
                )
            # Best-effort: persist error status if the DB row was created.
            if hub_id is not None:
                try:
                    await self.state.set_error(
                        session,
                        ctx["module_id"],
                        str(e),
                        hub_id=hub_id,
                    )
                except Exception as db_err:
                    logger.error(
                        "Cleanup: failed to set error status in DB for %s: %s",
                        ctx["module_id"],
                        db_err,
                    )

        return result

    # ------------------------------------------------------------------
    # Install — phase functions (internal)
    # ------------------------------------------------------------------
    #
    # Each ``_phase_*`` returns a :class:`PhaseResult` with a rollback
    # handle that undoes only its own side effects. The handles are
    # deliberately small closures over the values captured at
    # success-time so rollback is deterministic and LIFO-safe.

    async def _phase_download(
        self,
        module_id: str,
        version: str | None,
        checksum: str,
        source: str | None = None,
    ) -> PhaseResult:
        """
        Resolve module source and materialize into MODULES_DIR.

        Source resolution order:
        1. ``source`` is a URL → download via MarketplaceClient
        2. ``source`` is a local .zip path → extract directly
        3. Module already exists in MODULES_DIR → skip download
        4. ``MODULE_MARKETPLACE_URL`` is configured → resolve + download
        5. S3 fallback (legacy)
        """
        import shutil

        target_path = Path(self.settings.MODULES_DIR) / module_id
        resolved_version = version
        resolved_checksum = checksum

        # 1. Explicit URL source → download via MarketplaceClient
        if source and (source.startswith("http://") or source.startswith("https://")):
            from hotframe.engine.marketplace_client import MarketplaceClient

            client = MarketplaceClient("")
            cache_path = await client.download(source, self.settings.MODULES_CACHE_DIR, checksum)
            tmp_path = target_path.with_suffix(".tmp")
            if tmp_path.exists():
                shutil.rmtree(tmp_path)
            shutil.copytree(cache_path, tmp_path)
            if target_path.exists():
                shutil.rmtree(target_path)
            tmp_path.rename(target_path)

            class _UrlDownloadRollback:
                async def undo(self) -> None:
                    if target_path.exists():
                        shutil.rmtree(target_path, ignore_errors=True)

            return PhaseResult(
                phase_name="DOWNLOADING",
                rollback=_UrlDownloadRollback(),
                payload={"module_path": target_path},
            )

        # 2. Explicit local .zip source → extract directly
        if source and source.endswith(".zip") and Path(source).exists():
            from hotframe.engine.marketplace_client import MarketplaceClient

            cache_path = MarketplaceClient._extract_zip(
                Path(source), self.settings.MODULES_CACHE_DIR
            )
            tmp_path = target_path.with_suffix(".tmp")
            if tmp_path.exists():
                shutil.rmtree(tmp_path)
            shutil.copytree(cache_path, tmp_path)
            if target_path.exists():
                shutil.rmtree(target_path)
            tmp_path.rename(target_path)

            class _ZipDownloadRollback:
                async def undo(self) -> None:
                    if target_path.exists():
                        shutil.rmtree(target_path, ignore_errors=True)

            return PhaseResult(
                phase_name="DOWNLOADING",
                rollback=_ZipDownloadRollback(),
                payload={"module_path": target_path},
            )

        # 3. Module already on disk → skip download
        if target_path.exists() and (target_path / "module.py").exists():

            class _NoopDownloadRollback:
                async def undo(self) -> None:
                    return None

            return PhaseResult(
                phase_name="DOWNLOADING",
                rollback=_NoopDownloadRollback(),
                payload={"module_path": target_path},
            )

        # 4. Marketplace URL configured → resolve + download
        if self.settings.MODULE_MARKETPLACE_URL:
            from hotframe.engine.marketplace_client import MarketplaceClient

            client = MarketplaceClient(self.settings.MODULE_MARKETPLACE_URL)
            info = await client.resolve(module_id, version)
            cache_path = await client.download(
                info.download_url,
                self.settings.MODULES_CACHE_DIR,
                info.checksum_sha256,
            )
            resolved_version = info.version
            resolved_checksum = info.checksum_sha256
            tmp_path = target_path.with_suffix(".tmp")
            if tmp_path.exists():
                shutil.rmtree(tmp_path)
            shutil.copytree(cache_path, tmp_path)
            if target_path.exists():
                shutil.rmtree(target_path)
            tmp_path.rename(target_path)

            class _MarketplaceDownloadRollback:
                async def undo(self) -> None:
                    if target_path.exists():
                        shutil.rmtree(target_path, ignore_errors=True)

            return PhaseResult(
                phase_name="DOWNLOADING",
                rollback=_MarketplaceDownloadRollback(),
                payload={
                    "module_path": target_path,
                    "version": resolved_version,
                    "checksum": resolved_checksum,
                },
            )

        # 5. S3 fallback (legacy)
        if self.s3 is not None:
            if not version:
                raise ValueError(
                    f"Cannot download {module_id} from S3: explicit version required",
                )
            cache_path = await self.s3.download(module_id, version, checksum)
            tmp_path = target_path.with_suffix(".tmp")
            if tmp_path.exists():
                shutil.rmtree(tmp_path)
            shutil.copytree(cache_path, tmp_path)
            if target_path.exists():
                shutil.rmtree(target_path)
            tmp_path.rename(target_path)

            s3 = self.s3

            class _S3DownloadRollback:
                async def undo(self) -> None:
                    if target_path.exists():
                        shutil.rmtree(target_path, ignore_errors=True)
                    try:
                        s3.clear_cache(module_id)
                    except Exception as cache_err:
                        logger.error(
                            "Rollback: failed to clear S3 cache for %s: %s",
                            module_id,
                            cache_err,
                        )

            return PhaseResult(
                phase_name="DOWNLOADING",
                rollback=_S3DownloadRollback(),
                payload={"module_path": target_path},
            )

        raise RuntimeError(
            f"Module '{module_id}' not found and no download source configured "
            "(no source URL/zip, not in MODULES_DIR, no marketplace URL, no S3)"
        )

    async def _phase_validate(
        self,
        session: ISession,
        hub_id: UUID,
        module_id: str,
        version: str,
        module_path: Path,
    ) -> PhaseResult:
        """
        Validate the manifest and, if MODULE_ID differs from the catalog
        key, rename the extracted directory to the canonical id and
        update the catalog row accordingly.
        """
        import shutil as _shutil

        try:
            manifest = load_manifest(module_path)
        except Exception as e:
            raise RuntimeError(f"Manifest validation failed: {e}") from e

        canonical_id = manifest.MODULE_ID
        renamed_from: str | None = None

        if canonical_id != module_id:
            logger.warning(
                "MODULE_ID mismatch: catalog=%r, manifest=%r — using manifest ID as canonical",
                module_id,
                canonical_id,
            )
            if hub_id is not None:
                existing_canonical = await self.state.get_module(
                    session,
                    canonical_id,
                    hub_id=hub_id,
                )
                if existing_canonical is not None:
                    raise RuntimeError(
                        f"Module {canonical_id} (catalog key: {module_id}) is already "
                        f"installed (status={existing_canonical.status})"
                    )
            canonical_path = Path(self.settings.MODULES_DIR) / canonical_id
            if canonical_path.exists():
                _shutil.rmtree(canonical_path)
            module_path.rename(canonical_path)
            module_path = canonical_path

            from sqlalchemy import update as sa_update

            HubModuleVersion = _get_module_version_model()
            if HubModuleVersion is not None:
                await session.execute(
                    sa_update(HubModuleVersion)
                    .where(
                        HubModuleVersion.module_id == module_id,
                        HubModuleVersion.version == version,
                    )
                    .values(module_id=canonical_id)
                )
            renamed_from = module_id
            module_id = canonical_id

        # Validation has no independent side effects that need undoing
        # other than the catalog rename — and the download rollback will
        # remove the renamed directory regardless. We leave the catalog
        # row edit as-is (documented limitation: rare + self-heals on
        # the next publish).
        class _ValidateRollback:
            async def undo(self) -> None:
                if renamed_from is not None:
                    logger.info(
                        "Validate rollback: canonical rename %s→%s left in "
                        "HubModuleVersion catalog (no undo)",
                        renamed_from,
                        module_id,
                    )

        return PhaseResult(
            phase_name="VALIDATING",
            rollback=_ValidateRollback(),
            payload={
                "manifest": manifest,
                "module_id": module_id,
                "module_path": module_path,
            },
        )

    async def _phase_check_deps(
        self,
        session: ISession,
        hub_id: UUID,
        manifest: Any,
        auto_install_deps: bool,
    ) -> PhaseResult:
        """Verify dependencies (catalog + version + active) before mutating state."""

        if hub_id is not None:
            dep_check = await self.deps.check_install_deps(session, manifest, hub_id=hub_id)
            if not dep_check.ok:
                if dep_check.missing:
                    raise RuntimeError(
                        f"Missing dependencies (not in catalog): {dep_check.missing}"
                    )
                if dep_check.version_mismatch:
                    mismatches = [
                        f"{mid} requires {req}, installed {actual}"
                        for mid, req, actual in dep_check.version_mismatch
                    ]
                    raise RuntimeError(f"Version mismatch: {'; '.join(mismatches)}")
                if dep_check.inactive and not auto_install_deps:
                    raise RuntimeError(
                        f"Inactive dependencies (activate first): {dep_check.inactive}"
                    )

        class _NoopRollback:
            async def undo(self) -> None:
                return None

        return PhaseResult(
            phase_name="VALIDATING",
            rollback=_NoopRollback(),
            payload={},
        )

    async def _phase_migrate(
        self,
        session: ISession,
        hub_id: UUID,
        module_id: str,
        version: str,
        checksum: str,
        installed_by: UUID | None,
        manifest: Any,
        module_path: Path,
    ) -> PhaseResult:
        """Create DB row (status='installing') and run Alembic upgrade."""

        if hub_id is not None:
            await self.state.create(
                session,
                module_id,
                version,
                checksum=checksum,
                status="installing",
                hub_id=hub_id,
                installed_by=installed_by,
            )

        migrated = False
        if manifest.HAS_MODELS:
            db_url = self.migrations.get_sync_db_url(self.settings.DATABASE_URL)
            await self.migrations.upgrade(module_id, module_path, db_url)
            migrated = True

        migrations = self.migrations
        state = self.state
        settings = self.settings

        class _MigrateRollback:
            async def undo(self) -> None:
                # Reverse order inside the phase: downgrade first, then
                # delete the DB row so the downgrade still has context.
                if migrated:
                    try:
                        db_url = migrations.get_sync_db_url(settings.DATABASE_URL)
                        await migrations.downgrade(module_id, module_path, db_url)
                    except Exception as mig_err:
                        logger.error(
                            "Rollback: failed to downgrade migrations for %s: %s",
                            module_id,
                            mig_err,
                        )
                if hub_id is not None:
                    try:
                        await state.delete(session, module_id, hub_id=hub_id)
                    except Exception as db_err:
                        logger.error(
                            "Rollback: failed to delete DB row for %s: %s",
                            module_id,
                            db_err,
                        )

        return PhaseResult(
            phase_name="MIGRATING",
            rollback=_MigrateRollback(),
            payload={"migrated": migrated},
        )

    async def _phase_on_install(
        self,
        session: ISession,
        hub_id: UUID,
        module_id: str,
    ) -> PhaseResult:
        """Invoke ``on_install`` lifecycle hook."""

        if hub_id is not None:
            await self.lifecycle.call(module_id, "on_install", session, hub_id)

        class _OnInstallRollback:
            async def undo(self) -> None:
                # There is no symmetric on_install_rollback hook; modules
                # that need to clean up data should handle it in
                # on_uninstall, which the higher-level rollback path
                # doesn't invoke here by design (would duplicate the
                # migrate downgrade). Left as no-op intentionally.
                return None

        return PhaseResult(
            phase_name="IMPORTING",
            rollback=_OnInstallRollback(),
            payload={},
        )

    async def _phase_mount(
        self,
        module_id: str,
        module_path: Path,
        manifest: Any,
    ) -> PhaseResult:
        """Import module code via the loader and refresh Jinja2 template dirs."""

        await self.loader.load_module(module_id, module_path, manifest)
        self._refresh_templates()

        loader = self.loader
        registry = self.registry

        class _MountRollback:
            async def undo(self) -> None:
                if registry.is_loaded(module_id):
                    try:
                        await loader.unload_module(module_id)
                    except Exception as unload_err:
                        logger.error(
                            "Rollback: failed to unload module %s: %s",
                            module_id,
                            unload_err,
                        )

        return PhaseResult(
            phase_name="MOUNTING",
            rollback=_MountRollback(),
            payload={},
        )

    async def _phase_activate(
        self,
        session: ISession,
        hub_id: UUID,
        module_id: str,
        manifest: Any,
    ) -> PhaseResult:
        """Run ``on_activate`` hook and flip the DB row to ``active``."""

        if hub_id is not None:
            await self.lifecycle.call(module_id, "on_activate", session, hub_id)
            await self.state.activate(
                session,
                module_id,
                manifest_to_dict(manifest),
                hub_id=hub_id,
            )

        class _ActivateRollback:
            async def undo(self) -> None:
                # The MIGRATING rollback deletes the DB row entirely and
                # MOUNTING unloads the module — so flipping the status
                # back here would race with those handlers. No-op.
                return None

        return PhaseResult(
            phase_name="STACK_REBUILD",
            rollback=_ActivateRollback(),
            payload={},
        )

    # ------------------------------------------------------------------
    # Activate (re-activate a disabled module)
    # ------------------------------------------------------------------

    async def activate(
        self,
        session: ISession,
        hub_id: UUID,
        module_id: str,
    ) -> ActivateResult:
        """Re-activate a disabled module."""
        result = ActivateResult(module_id=module_id)

        try:
            # Check current state
            mod = await self.state.get_module(session, module_id, hub_id=hub_id)
            if mod is None:
                result.error = f"Module {module_id} is not installed"
                return result

            if mod.status == "active":
                result.error = f"Module {module_id} is already active"
                return result

            if mod.status not in ("disabled", "installed", "error"):
                result.error = f"Cannot activate module in status {mod.status!r}"
                return result

            # Ensure code is available at /app/modules/{module_id}/
            module_path = Path(self.settings.MODULES_DIR) / module_id
            if not module_path.exists() or not (module_path / "module.py").exists():
                if self.s3 is not None:
                    # Try S3 cache first, then download
                    cache_path = self.settings.MODULES_CACHE_DIR / module_id
                    if not cache_path.exists() or not (cache_path / "module.py").exists():
                        cache_path = await self.s3.download(
                            module_id,
                            mod.version,
                            mod.checksum_sha256,
                        )
                    import shutil

                    tmp_path = module_path.with_suffix(".tmp")
                    if tmp_path.exists():
                        shutil.rmtree(tmp_path)
                    shutil.copytree(cache_path, tmp_path)
                    if module_path.exists():
                        shutil.rmtree(module_path)
                    tmp_path.rename(module_path)
                else:
                    raise RuntimeError(
                        f"Module '{module_id}' code not found at {module_path} "
                        "and no S3 source configured to retrieve it"
                    )

            # Validate manifest
            manifest = load_manifest(module_path)

            # Check dependencies
            dep_check = await self.deps.check_install_deps(session, manifest, hub_id=hub_id)
            if not dep_check.ok:
                inactive = dep_check.inactive
                missing = dep_check.missing
                result.error = f"Cannot activate: missing deps {missing}, inactive deps {inactive}"
                return result

            # Load into runtime
            await self.loader.load_module(module_id, module_path, manifest)

            # Refresh template dirs so newly activated module templates are discoverable
            self._refresh_templates()

            # Lifecycle
            await self.lifecycle.call(module_id, "on_activate", session, hub_id)

            # Update DB
            await self.state.activate(session, module_id, manifest_to_dict(manifest), hub_id=hub_id)

            # Event
            await self.bus.emit(
                "module.activated",
                sender=self,
                module_id=module_id,
                hub_id=hub_id,
            )

            result.success = True
            logger.info("Activated module %s for hub %s", module_id, hub_id)

        except Exception as e:
            logger.exception("Activate failed for %s", module_id)
            result.error = str(e)

            # Unload from runtime if it was loaded before failure
            if self.registry.is_loaded(module_id):
                try:
                    await self.loader.unload_module(module_id)
                except Exception as unload_err:
                    logger.error(
                        "Cleanup: failed to unload module %s after activate error: %s",
                        module_id,
                        unload_err,
                    )

            # Set error status in DB
            try:
                await self.state.set_error(session, module_id, str(e), hub_id=hub_id)
            except Exception as db_err:
                logger.error(
                    "Cleanup: failed to set error status in DB for %s: %s",
                    module_id,
                    db_err,
                )

        return result

    # ------------------------------------------------------------------
    # Deactivate
    # ------------------------------------------------------------------

    async def deactivate(
        self,
        session: ISession,
        hub_id: UUID,
        module_id: str,
        cascade: bool = False,
    ) -> DeactivateResult:
        """
        Deactivate a module. Checks dependents first.

        If ``cascade=True`` and there are dependents, deactivates them all
        in reverse dependency order. Otherwise returns the dependent list
        so the UI can ask for confirmation.
        """
        result = DeactivateResult(module_id=module_id)

        try:
            # Check current state
            mod = await self.state.get_module(session, module_id, hub_id=hub_id)
            if mod is None:
                result.error = f"Module {module_id} is not installed"
                return result

            # System modules cannot be deactivated
            if mod.is_system:
                result.error = f"Cannot deactivate system module '{module_id}'"
                return result

            if mod.status != "active":
                result.error = f"Module {module_id} is not active (status={mod.status!r})"
                return result

            # Check dependents
            check = await self.deps.check_can_deactivate(session, module_id, hub_id=hub_id)

            if not check.can_deactivate:
                if not cascade:
                    result.dependents = check.dependents
                    result.cascade_order = check.cascade_order
                    result.error = f"Cannot deactivate: modules depend on this: {check.dependents}"
                    return result

                # Cascade deactivation (user confirmed)
                await self.deps.deactivate_cascade(session, module_id, self, hub_id=hub_id)
                result.cascaded = check.cascade_order

            # Lifecycle hook
            await self.lifecycle.call(module_id, "on_deactivate", session, hub_id)

            # Unload from runtime
            await self.loader.unload_module(module_id)

            # Update DB
            await self.state.deactivate(session, module_id, hub_id=hub_id)

            # Event
            await self.bus.emit(
                "module.deactivated",
                sender=self,
                module_id=module_id,
                hub_id=hub_id,
            )

            result.success = True
            logger.info("Deactivated module %s for hub %s", module_id, hub_id)

        except Exception as e:
            logger.exception("Deactivate failed for %s", module_id)
            result.error = str(e)

            # Set error status in DB so the module isn't left in a ghost state
            try:
                await self.state.set_error(session, module_id, str(e), hub_id=hub_id)
            except Exception as db_err:
                logger.error(
                    "Cleanup: failed to set error status in DB for %s: %s",
                    module_id,
                    db_err,
                )

        return result

    # ------------------------------------------------------------------
    # Uninstall
    # ------------------------------------------------------------------

    async def uninstall(
        self,
        session: ISession,
        hub_id: UUID,
        module_id: str,
    ) -> UninstallResult:
        """
        Uninstall a module completely.

        BLOCKED if any module (any status) depends on this one.
        NEVER cascade uninstall — user must uninstall dependents first.
        """
        result = UninstallResult(module_id=module_id)

        try:
            # Check current state
            mod = await self.state.get_module(session, module_id, hub_id=hub_id)
            if mod is None:
                result.error = f"Module {module_id} is not installed"
                return result

            # System modules cannot be uninstalled
            if mod.is_system:
                result.error = f"Cannot uninstall system module '{module_id}'"
                return result

            # Check dependents — NEVER cascade
            check = await self.deps.check_can_uninstall(session, module_id, hub_id=hub_id)
            if not check.can_uninstall:
                result.dependents = check.dependents
                result.error = (
                    "Cannot uninstall: other modules depend on this one. Uninstall them first."
                )
                return result

            # If active, unload from runtime first
            if self.registry.is_loaded(module_id):
                await self.lifecycle.call(module_id, "on_deactivate", session, hub_id)
                await self.loader.unload_module(module_id)

            # Lifecycle: on_uninstall
            module_path = Path(self.settings.MODULES_DIR) / module_id
            if module_path.exists():
                try:
                    await self.lifecycle.call(module_id, "on_uninstall", session, hub_id)
                except Exception as hook_err:
                    logger.error(
                        "on_uninstall hook failed for %s: %s — aborting uninstall to prevent data loss",
                        module_id,
                        hook_err,
                    )
                    result.error = (
                        f"Uninstall hook failed: {hook_err}. Fix the hook or force uninstall."
                    )
                    return result

            # Revert migrations
            if module_path.exists() and self.migrations.has_migrations(module_path):
                db_url = self.migrations.get_sync_db_url(self.settings.DATABASE_URL)
                await self.migrations.downgrade(module_id, module_path, db_url)

            # Delete from DB
            await self.state.delete(session, module_id, hub_id=hub_id)

            # Clean local cache
            if self.s3 is not None:
                self.s3.clear_cache(module_id)

            # Refresh templates after unload
            self._refresh_templates()

            # Event
            await self.bus.emit(
                "module.uninstalled",
                sender=self,
                module_id=module_id,
                hub_id=hub_id,
            )

            result.success = True
            logger.info("Uninstalled module %s from hub %s", module_id, hub_id)

        except Exception as e:
            logger.exception("Uninstall failed for %s", module_id)
            result.error = str(e)

            # Set error status in DB so the module isn't stuck in limbo
            try:
                await self.state.set_error(session, module_id, str(e), hub_id=hub_id)
            except Exception as db_err:
                logger.error(
                    "Cleanup: failed to set error status in DB for %s: %s",
                    module_id,
                    db_err,
                )

        return result

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(
        self,
        session: ISession,
        hub_id: UUID,
        module_id: str,
        new_version: str,
        checksum: str = "",
        source: str | None = None,
    ) -> UpdateResult:
        """
        Update a module to a new version.

        Steps: download new -> validate -> on_deactivate -> unload ->
        migrate -> load new -> on_activate -> update DB.

        Args:
            session: Async SQLAlchemy session.
            hub_id: Tenant UUID identifying which hub owns the module.
            module_id: Module to update.
            new_version: Target version string.
            checksum: Expected SHA-256 of the new archive.
            source: Explicit URL or ``.zip`` path; bypasses marketplace resolution.

        Returns:
            :class:`UpdateResult` with ``success``, ``from_version``, ``to_version``, and ``error``.
        """
        result = UpdateResult(module_id=module_id, to_version=new_version)
        was_active = False

        try:
            # Check current state
            mod = await self.state.get_module(session, module_id, hub_id=hub_id)
            if mod is None:
                result.error = f"Module {module_id} is not installed"
                return result

            result.from_version = mod.version
            was_active = mod.status == "active"

            # 1. Download new version — source resolution order:
            #    explicit URL → explicit .zip → marketplace → S3
            module_path: Path | None = None
            target_path = Path(self.settings.MODULES_DIR) / module_id

            if source and (source.startswith("http://") or source.startswith("https://")):
                from hotframe.engine.marketplace_client import MarketplaceClient

                client = MarketplaceClient("")
                cache_path = await client.download(
                    source, self.settings.MODULES_CACHE_DIR, checksum
                )
                import shutil as _shutil

                tmp_path = target_path.with_suffix(".tmp")
                if tmp_path.exists():
                    _shutil.rmtree(tmp_path)
                _shutil.copytree(cache_path, tmp_path)
                if target_path.exists():
                    _shutil.rmtree(target_path)
                tmp_path.rename(target_path)
                module_path = target_path
            elif source and source.endswith(".zip") and Path(source).exists():
                import shutil as _shutil

                from hotframe.engine.marketplace_client import MarketplaceClient

                cache_path = MarketplaceClient._extract_zip(
                    Path(source), self.settings.MODULES_CACHE_DIR
                )
                tmp_path = target_path.with_suffix(".tmp")
                if tmp_path.exists():
                    _shutil.rmtree(tmp_path)
                _shutil.copytree(cache_path, tmp_path)
                if target_path.exists():
                    _shutil.rmtree(target_path)
                tmp_path.rename(target_path)
                module_path = target_path
            elif self.settings.MODULE_MARKETPLACE_URL:
                import shutil as _shutil

                from hotframe.engine.marketplace_client import MarketplaceClient

                client = MarketplaceClient(self.settings.MODULE_MARKETPLACE_URL)
                info = await client.resolve(module_id, new_version or None)
                cache_path = await client.download(
                    info.download_url, self.settings.MODULES_CACHE_DIR, info.checksum_sha256
                )
                new_version = info.version
                checksum = info.checksum_sha256
                result.to_version = new_version
                tmp_path = target_path.with_suffix(".tmp")
                if tmp_path.exists():
                    _shutil.rmtree(tmp_path)
                _shutil.copytree(cache_path, tmp_path)
                if target_path.exists():
                    _shutil.rmtree(target_path)
                tmp_path.rename(target_path)
                module_path = target_path
            elif self.s3 is not None:
                module_path = await self.s3.download(
                    module_id,
                    new_version,
                    checksum,
                )
            else:
                result.error = (
                    f"Cannot update '{module_id}': no source, marketplace URL, or S3 configured"
                )
                return result

            # 2. Validate new manifest
            try:
                manifest = load_manifest(module_path)
            except Exception as e:
                result.error = f"New version manifest validation failed: {e}"
                return result

            # 3. If active, call on_deactivate + unload
            if was_active and self.registry.is_loaded(module_id):
                await self.lifecycle.call(module_id, "on_deactivate", session, hub_id)
                await self.loader.unload_module(module_id)

            # 4. Run migrations for new version
            if manifest.HAS_MODELS:
                db_url = self.migrations.get_sync_db_url(self.settings.DATABASE_URL)
                await self.migrations.upgrade(module_id, module_path, db_url)

            # 5. Call on_upgrade lifecycle hook
            await self.lifecycle.call(
                module_id,
                "on_upgrade",
                session,
                hub_id,
                from_version=mod.version,
                to_version=new_version,
            )

            # 6. Load new version
            await self.loader.load_module(module_id, module_path, manifest)

            # Refresh template dirs so updated module templates are discoverable
            self._refresh_templates()

            # 7. Call on_activate
            if was_active:
                await self.lifecycle.call(module_id, "on_activate", session, hub_id)

            # 8. Update DB
            await self.state.activate(session, module_id, manifest_to_dict(manifest), hub_id=hub_id)
            # Update version and S3 info via direct update
            from sqlalchemy import update as sa_update

            HubModule = _get_module_model()
            await session.execute(
                sa_update(HubModule)
                .where(HubModule.hub_id == hub_id, HubModule.module_id == module_id)
                .values(version=new_version, checksum_sha256=checksum)
            )

            # 9. Event
            await self.bus.emit(
                "module.updated",
                sender=self,
                module_id=module_id,
                from_version=mod.version,
                to_version=new_version,
                hub_id=hub_id,
            )

            result.success = True
            logger.info(
                "Updated module %s from v%s to v%s for hub %s",
                module_id,
                mod.version,
                new_version,
                hub_id,
            )

        except Exception as e:
            logger.exception("Update failed for %s", module_id)
            result.error = str(e)

            # If the module was unloaded for update but new version failed to load,
            # try to reload the old version so the module isn't left dead
            if was_active and not self.registry.is_loaded(module_id):
                try:
                    old_path = Path(self.settings.MODULES_DIR) / module_id
                    if old_path.exists():
                        old_manifest = load_manifest(old_path)
                        await self.loader.load_module(module_id, old_path, old_manifest)
                        logger.warning(
                            "Update rollback: reloaded previous version of %s",
                            module_id,
                        )
                except Exception as rollback_err:
                    logger.error(
                        "Cleanup: failed to rollback %s to previous version: %s",
                        module_id,
                        rollback_err,
                    )

            # Set error status in DB
            try:
                await self.state.set_error(session, module_id, str(e), hub_id=hub_id)
            except Exception as db_err:
                logger.error(
                    "Cleanup: failed to set error status in DB for %s: %s",
                    module_id,
                    db_err,
                )

        return result

    # ------------------------------------------------------------------
    # Hot reload (dev only)
    # ------------------------------------------------------------------

    async def hot_reload(self, module_id: str) -> bool:
        """
        Dev mode: re-import module code without restart.

        Preserves DB state — only reloads Python code and re-mounts routes.
        Re-validates dependencies in case DEPENDENCIES changed.
        """
        entry = self.registry.get(module_id)
        if entry is None:
            logger.warning("Cannot hot-reload %s: not loaded", module_id)
            return False

        try:
            manifest = load_manifest(entry.path)

            # Re-check dependencies (they may have changed in module.py)
            if manifest.DEPENDENCIES:
                for dep_id in manifest.DEPENDENCIES:
                    dep_str = dep_id if isinstance(dep_id, str) else str(dep_id)
                    dep_module_id = (
                        dep_str.split(">=")[0]
                        .split("<=")[0]
                        .split("==")[0]
                        .split("!=")[0]
                        .split(">")[0]
                        .split("<")[0]
                        .strip()
                    )
                    if not self.registry.is_loaded(dep_module_id):
                        logger.error(
                            "Hot-reload %s: dependency %s is not loaded — aborting",
                            module_id,
                            dep_module_id,
                        )
                        return False

            await self.loader.reload_module(module_id, entry.path, manifest)
            logger.info("Hot-reloaded module %s", module_id)
            return True
        except Exception:
            logger.exception("Hot-reload failed for %s", module_id)
            return False

    # ------------------------------------------------------------------
    # Template refresh
    # ------------------------------------------------------------------

    def _refresh_templates(self) -> None:
        """Refresh Jinja2 template dirs after module install/uninstall."""
        templates = getattr(self.app.state, "templates", None)
        if templates is None:
            return
        from hotframe.templating.engine import refresh_template_dirs

        refresh_template_dirs(templates, self.settings.MODULES_DIR)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Graceful shutdown: stop watcher."""
        await self.watcher.stop()
        logger.info("ModuleRuntime shutdown complete")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_from_path(
        self,
        session: ISession,
        hub_id: UUID,
        module_id: str,
        module_path: Path,
        skip_db_writes: bool = False,
    ) -> bool:
        """
        Load a single module from its local path.

        Validates the manifest and loads via :class:`ModuleLoader`.
        On failure, sets the module to error status in the DB.

        Defensively rolls back the session before any post-failure DB
        write so a transaction-level error on one module (e.g. a
        deadlock or constraint violation) cannot poison the shared
        boot session and cascade "current transaction is aborted"
        failures onto every subsequent module.

        When ``skip_db_writes`` is ``True``, the in-memory route mount
        still runs (each uvicorn worker owns its own FastAPI app and
        needs its local routes) but the ``hub_module.manifest`` and
        ``hub_module.status`` writes are skipped so multiple workers
        don't race into a Postgres deadlock on the shared row.
        """
        try:
            manifest = load_manifest(module_path)
            await self.loader.load_module(module_id, module_path, manifest)

            # Re-serialize manifest to DB so it stays in sync with the
            # current key format produced by manifest_to_dict(). Only the
            # leader worker writes — see boot() docstring.
            if not skip_db_writes:
                await self.state.update_manifest(
                    session,
                    module_id,
                    manifest_to_dict(manifest),
                    hub_id=hub_id,
                )

            return True
        except Exception as e:
            logger.exception("Failed to load module %s from %s", module_id, module_path)
            if skip_db_writes:
                # Follower worker: do not touch the DB. The leader worker
                # is responsible for persisting the error state.
                return False
            # Clear any in-flight transaction before writing the error
            # status — otherwise set_error itself raises "current
            # transaction is aborted" and leaves the session unusable
            # for the next module in the boot loop.
            try:
                await session.rollback()
            except Exception as rb_err:
                logger.debug("Best-effort session rollback failed: %s", rb_err)
            try:
                await self.state.set_error(session, module_id, str(e), hub_id=hub_id)
            except Exception as db_err:
                logger.error(
                    "Failed to set error status in DB for %s: %s",
                    module_id,
                    db_err,
                )
            return False

    async def _ensure_module_code(
        self,
        modules: list[Any],
    ) -> dict[str, Path]:
        """
        Ensure module code is available at /app/modules/{module_id}/.

        DB is the source of truth for what's installed.
        S3 is the source of truth for module code.
        /app/modules/ starts empty on a new container.

        For each module:
            1. If code already at /app/modules/{id}/ → use it (dev mount or warm container)
            2. If code in /tmp/modules/{id}/ (S3 cache) → copy to /app/modules/{id}/
            3. Otherwise → download from S3 to /tmp/modules/ cache → copy to /app/modules/{id}/

        Args:
            modules: List of module ORM instances.

        Returns:
            Dict mapping ``module_id`` to local :class:`Path`.
        """
        import shutil

        result: dict[str, Path] = {}
        to_download: list[tuple[str, str, str]] = []
        modules_dir = Path(self.settings.MODULES_DIR)
        modules_dir.mkdir(parents=True, exist_ok=True)

        for mod in modules:
            target_path = modules_dir / mod.module_id

            # Already in /app/modules/ (dev mount or warm container)
            if target_path.exists() and (target_path / "module.py").exists():
                result[mod.module_id] = target_path
                continue

            # Check S3 cache (/tmp/modules/)
            cache_path = self.settings.MODULES_CACHE_DIR / mod.module_id
            if cache_path.exists() and (cache_path / "module.py").exists():
                # Atomic copy: write to temp dir, then rename
                tmp_path = target_path.with_suffix(".tmp")
                if tmp_path.exists():
                    shutil.rmtree(tmp_path)
                shutil.copytree(cache_path, tmp_path)
                if target_path.exists():
                    shutil.rmtree(target_path)
                tmp_path.rename(target_path)
                result[mod.module_id] = target_path
                logger.debug("Copied %s from cache to %s (atomic)", mod.module_id, target_path)
                continue

            # Need to download from S3
            to_download.append(
                (
                    mod.module_id,
                    mod.version,
                    mod.checksum_sha256,
                )
            )

        if to_download:
            if self.s3 is None:
                logger.warning(
                    "Skipping S3 download of %d modules — no S3 source configured",
                    len(to_download),
                )
                downloaded = {}
            else:
                logger.info("Downloading %d modules from S3", len(to_download))
                # Downloads to /tmp/modules/ (S3 cache)
                downloaded = await self.s3.download_many(to_download)
            for module_id, cache_path in downloaded.items():
                target_path = modules_dir / module_id
                tmp_path = target_path.with_suffix(".tmp")
                if tmp_path.exists():
                    shutil.rmtree(tmp_path)
                shutil.copytree(cache_path, tmp_path)
                if target_path.exists():
                    shutil.rmtree(target_path)
                tmp_path.rename(target_path)
                result[module_id] = target_path
                logger.debug("Copied %s from S3 cache to %s (atomic)", module_id, target_path)

        return result


def _get_module_version_model() -> type[Any] | None:
    """
    Resolve the module version catalog model from settings.

    Returns the model class if ``settings.MODULE_VERSION_MODEL`` is set,
    otherwise returns ``None`` (catalog version resolution is skipped).

    Returns ``type[Any]`` for the same reason :func:`_get_module_model`
    does — the swappable model exposes SQLAlchemy column descriptors that
    cannot be statically typed without losing the descriptor magic.
    """
    import importlib

    from hotframe.config.settings import get_settings

    settings = get_settings()
    model_path = getattr(settings, "MODULE_VERSION_MODEL", None)
    if not model_path:
        return None
    module_path, class_name = model_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


def _hub_id_to_advisory_key(value: str) -> int:
    """
    Derive a stable signed 64-bit integer from a hub-id string.

    Postgres' ``pg_try_advisory_xact_lock(bigint)`` requires a signed
    64-bit key. We use the first 8 bytes of BLAKE2b over the input, then
    reinterpret as a signed little-endian int. The hash is deterministic
    across Python versions and worker processes, which is what the lock
    needs to identify the same hub from every worker.

    Python's built-in ``hash()`` is salted per-process since 3.3 and
    would give each worker a different key — we can't use it here.
    """
    import hashlib

    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    # signed=True so the result fits Postgres' bigint range.
    return int.from_bytes(digest, byteorder="little", signed=True)
