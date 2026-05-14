# 17. The Module Engine (`engine/`)

> The *hot-mount module engine* is the component that makes it possible to install,
> activate, deactivate, uninstall, and update Python modules **at
> runtime, without restarting the process**. This is the defining feature
> that sets hotframe apart from any conventional Python web framework.

---

## What this folder is for

In a traditional framework, adding a plugin requires a server restart:
the new code does not exist in `sys.modules`, routes are not registered
in the app, and database tables have not been created.

hotframe solves this problem with an orchestration engine that treats
each module as an artifact with **persistent state in the DB** and
**in-memory state** that can be managed independently. The DB is the
source of truth for what is installed; the Python process holds in RAM
the mounted routes, subscribed events, hooks, slots, and imported classes.

The operations the engine supports are:

| Operation   | What it does |
|-------------|--------------|
| `install`   | Downloads the code, validates it, runs DB migrations, calls `on_install`, mounts routes. |
| `activate`  | Re-mounts a deactivated module without re-downloading the code. |
| `deactivate`| Unmounts routes, clears `sys.modules`, updates status in the DB. |
| `uninstall` | Unmounts, reverts migrations, deletes the DB row. |
| `update`    | Downloads the new version, unmounts the old one, migrates, mounts the new one. |
| `boot`      | On process startup, restores all modules that are active in the DB into memory. |

All of this happens without `kill`, without `SIGTERM`, without restarting uvicorn.

---

## File map

| File | Responsibility |
|------|----------------|
| [`__init__.py`](#initpy--public-api) | Re-exports the public API of the sub-package. |
| [`models.py`](#modelspy--state-model) | SQLAlchemy `Module` model (table `hotframe_module`). |
| [`state.py`](#statepy--crud-layer) | `ModuleStateDB` — all reads/writes against the state table. |
| [`pipeline.py`](#pipelinepy--state-machine-with-lifo-rollback) | `HotMountPipeline` — phase primitive with LIFO rollback. |
| [`import_manager.py`](#import_managerpy--precise-sysmodules-management) | `ImportManager` — imports packages, tracks submodules in `sys.modules`, and detects zombie classes. |
| [`loader.py`](#loaderpy--loading-and-unloading-in-fastapi) | `ModuleLoader` — mounts and unmounts FastAPI routes, events, hooks, slots, components, middleware, and locales. |
| [`lifecycle.py`](#lifecyclepy--module-hooks) | `ModuleLifecycleManager` — calls the `on_install/activate/deactivate/uninstall/upgrade` hooks. |
| [`dependency.py`](#dependencypy--dependency-manager) | `DependencyManager` — topological ordering, version checking, deactivation cascades. |
| [`s3_source.py`](#s3_sourcepy--downloading-from-s3) | `S3ModuleSource` — downloads, verifies SHA-256, and caches modules from AWS S3. |
| [`marketplace_client.py`](#marketplace_clientpy--marketplace-http-client) | `MarketplaceClient` — resolves and downloads modules from any marketplace server. |
| [`boundary.py`](#boundarypy--error-isolation-barrier) | `ModuleBoundaryMiddleware` — catches exceptions from module routes, preventing them from taking down the Hub Core. |
| [`module_runtime.py`](#module_runtimepy--the-central-orchestrator) | `ModuleRuntime` — the single orchestrator that coordinates all of the above. |

---

## `__init__.py` — Public API

Re-exports exactly what an external developer needs to work with the
engine:

```python
from hotframe.engine import (
    ModuleRuntime,       # orchestrator
    ModuleLoader,        # load/unload
    ImportManager,       # sys.modules management
    ImportedBundle,      # result of an import
    PurgeReport,         # result of a purge
    HotMountPipeline,    # phase primitive
    PhaseResult,
    PhaseStatus,
    PipelineState,
    RollbackHandle,
    InstallResult,
    ActivateResult,
    DeactivateResult,
    UninstallResult,
    UpdateResult,
)
```

`__init__.py` contains no logic, only imports. This is deliberate: the
engine is testable piece by piece because each module has minimal
dependencies.

---

## `models.py` — State model

Defines the SQLAlchemy `Module` model stored in the `hotframe_module`
table. This is the *default* model; projects can replace it with
`settings.MODULE_STATE_MODEL` (required in multi-tenant environments
that add a `hub_id` field).

```python
class Module(Base):
    __tablename__ = "hotframe_module"

    id: Mapped[uuid.UUID]           # PK
    module_id: Mapped[str]          # "invoice", "loyalty", etc. — unique
    version: Mapped[str]            # "1.4.2"
    status: Mapped[str]             # "installing" | "active" | "disabled" | "error" | "degraded"
    checksum_sha256: Mapped[str]    # SHA-256 of the zip file
    manifest: Mapped[dict]          # JSON — snapshot of ModuleManifest at activation time
    config: Mapped[dict]            # JSON — per-hub/tenant configuration
    error_message: Mapped[str|None] # message from the last exception, if any
    is_system: Mapped[bool]         # True → cannot be deactivated or uninstalled
    installed_at: Mapped[datetime]
    activated_at: Mapped[datetime|None]
    disabled_at: Mapped[datetime|None]
```

### `status` value lifecycle

```
installing → active → disabled → active ...
             ↓
             error
             ↓
             degraded
```

- `installing` — transition during `install()`. If the process dies
  midway, the module is left in this state; the operator must clean up
  manually or reinstall.
- `active` — fully operational. `get_active_modules` returns it during
  boot.
- `disabled` — the user deactivated it; the code remains on disk but is
  not mounted in memory.
- `error` — install/activate/uninstall failed; cannot be used.
- `degraded` — `ModuleBoundaryMiddleware` detected too many errors in
  production; still mounted but the UI warns the user.

---

## `state.py` — CRUD layer

`ModuleStateDB` centralises all read/write operations against the state
table. It contains no business logic — only SQL.

```python
class ModuleStateDB:
    def _model(self) -> type: ...                     # resolves the model from settings
    async def get_active_modules(session, **filters)  # SELECT WHERE status='active'
    async def get_all_modules(session, **filters)
    async def get_module(session, module_id, **filters) -> Any | None
    async def create(session, module_id, version, *, checksum, status, **extra)
    async def activate(session, module_id, manifest_dict, **filters)
    async def deactivate(session, module_id, **filters)
    async def set_status(session, module_id, status, error, **filters)
    async def set_error(session, module_id, error_message, **filters)
    async def set_degraded(session, module_id, error_message, **filters)
    async def update_manifest(session, module_id, manifest_dict, **filters)
    async def delete(session, module_id, **filters)
```

### `**filters` as multi-tenancy

All methods accept arbitrary `**filters` that are translated into
`WHERE` clauses. In multi-tenant projects the typical filter is
`hub_id=<uuid>`:

```python
await state.get_active_modules(session, hub_id=hub_id)
await state.activate(session, module_id, manifest, hub_id=hub_id)
```

This allows `ModuleStateDB` to be used with any custom model that has
`hub_id` or other partition fields, without touching the engine code.

### `ModuleAlreadyInstallingError`

The `create` function calls `session.flush()` after inserting. If there
is a `UNIQUE` constraint on `module_id` (and optionally `hub_id`),
SQLAlchemy raises `IntegrityError`, which `create` catches and re-raises
as `ModuleAlreadyInstallingError`. The runtime uses this to detect
concurrent installations of the same module.

### `_get_module_model()`

A module-level function that resolves the configured ORM class. This
allows the entire `state.py` layer to work with project-defined models
without changing a single line:

```python
def _get_module_model() -> type:
    settings = get_settings()
    if settings.MODULE_STATE_MODEL:
        # "myproject.modules.HubModule" → imports and returns the class
        module_path, class_name = settings.MODULE_STATE_MODEL.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)
    from hotframe.engine.models import Module
    return Module
```

---

## `pipeline.py` — State machine with LIFO rollback

`HotMountPipeline` is a general-purpose primitive for executing a
sequence of steps where each one can fail and needs to undo only its own
effect. It is the core mechanism by which `ModuleRuntime.install`
guarantees it never leaves the system in a half-completed state.

### Key concepts

**`RollbackHandle`** — a protocol with a single method `async def undo()`.
Each pipeline phase produces one.

```python
@runtime_checkable
class RollbackHandle(Protocol):
    async def undo(self) -> None: ...
```

**`PhaseResult`** — what each phase returns on completion:

```python
@dataclass(slots=True)
class PhaseResult:
    phase_name: str         # "DOWNLOADING", "MIGRATING", etc.
    rollback: RollbackHandle
    payload: dict           # data the phase wants to pass to the next one
```

**`PipelineState`** — the mutable state of the pipeline:

```python
@dataclass(slots=True)
class PipelineState:
    module_id: str
    current_phase: str | None
    completed_phases: list[str]
    rollback_stack: list[RollbackHandle]  # ← pushed in execution order
    status: PhaseStatus                   # PENDING | RUNNING | ACTIVE | ERROR
    error: Exception | None
```

**`HotMountPipeline`** — the main class:

```python
class HotMountPipeline:
    PHASES = [
        "INIT", "DOWNLOADING", "EXTRACTING", "VALIDATING",
        "MIGRATING", "IMPORTING", "MOUNTING", "STACK_REBUILD", "ACTIVE",
    ]

    async def run_phase(self, phase_name, fn, *args, **kwargs) -> PhaseResult
    async def commit(self) -> None
    async def rollback(self) -> list[Exception]

    @property
    def state(self) -> PipelineState
```

### Execution flow

```
pipeline = HotMountPipeline("invoice")

r1 = await pipeline.run_phase("DOWNLOADING", fn_download, ...)
# → rollback_stack = [r1.rollback]

r2 = await pipeline.run_phase("MIGRATING", fn_migrate, ...)
# → rollback_stack = [r1.rollback, r2.rollback]

r3 = await pipeline.run_phase("MOUNTING", fn_mount, ...)
# → rollback_stack = [r1.rollback, r2.rollback, r3.rollback]

# If r3 fails before reaching here:
errors = await pipeline.rollback()
# → executes: r3.undo(), r2.undo(), r1.undo()  (LIFO)
```

Rollback is *best-effort*: if `r2.undo()` raises, the exception is
collected and execution continues with `r1.undo()`. At the end, the list
of exceptions is returned so the orchestrator can log them.

### Why LIFO

Reverse order guarantees that effects are undone in the correct
dependency order: if `MOUNTING` loaded into memory code that uses a
table created by `MIGRATING`, the `undo` for `MOUNTING` must unload that
code *before* `MIGRATING` removes it from the DB.

---

## `import_manager.py` — Precise `sys.modules` management

`ImportManager` solves a subtle problem: when Python imports a package,
it adds not only the package itself to `sys.modules` but all of its
submodules too. If when deactivating a module we only remove the
`"invoice"` entry from `sys.modules` but not `"invoice.routes"`,
`"invoice.models"`, etc., we are left with orphaned references that
prevent garbage collection and cause errors on the next installation.

### Main classes

**`ImportedBundle`** — the result of an import:

```python
@dataclass(slots=True)
class ImportedBundle:
    module_id: str
    package_name: str
    base_path: Path
    imported_submodules: list[str]    # everything that appeared in sys.modules
    exported_classes: list[weakref.ref]  # weak refs to registered classes
```

**`PurgeReport`** — the result of a purge:

```python
@dataclass(slots=True)
class PurgeReport:
    module_id: str
    purged_count: int           # entries removed from sys.modules
    zombie_classes: list[str]   # classes whose weakref survived gc.collect()
```

**`ImportManager`** — the main class:

```python
class ImportManager:
    def import_package(self, module_id, package_name, base_path) -> ImportedBundle
    def register_exported_class(self, module_id, cls: type) -> None
    def purge(self, module_id) -> PurgeReport
    def get_bundle(self, module_id) -> ImportedBundle | None
```

### `import_package` in detail

```python
def import_package(self, module_id, package_name, base_path):
    with self._lock:
        # 1. Add base_path.parent to sys.path if not already present
        # 2. Snapshot sys.modules BEFORE the import
        before = set(sys.modules.keys())
        # 3. importlib.import_module(package_name)
        # 4. Snapshot AFTER
        after = set(sys.modules.keys())
        new_modules = sorted(after - before)
        # 5. Save bundle with the exact list of new entries
        bundle = ImportedBundle(module_id=..., imported_submodules=new_modules)
        self._bundles[module_id] = bundle
```

If the import fails, it cleans up any entries that were already added
before re-raising the exception. The module is never left partially
registered in `sys.modules`.

### Zombie detection with `weakref`

After purging `sys.modules` and calling `gc.collect()`, if a registered
class is still alive (the `weakref` is not `None`), some external cache
is retaining it. The most common causes are:

- The SQLAlchemy mapper registry.
- The internal Pydantic cache.
- Signal subscribers in the `AsyncEventBus`.

`PurgeReport.zombie_classes` is not a fatal error — it is informational
— but if it appears consistently, it indicates the module has a leak and
may require a process restart for a complete cleanup.

### Thread-safety

`ImportManager` protects its `_bundles` dict with `threading.Lock`. This
is necessary because `asyncio` event loops can run on different threads
in tests or multi-worker configurations.

---

## `loader.py` — Loading and unloading in FastAPI

`ModuleLoader` is the only component that touches the `FastAPI` instance
and its registries: routes, events, hooks, slots, components, middleware,
and locales. It operates strictly at the Python/FastAPI level; it knows
nothing about S3, the DB, or the marketplace.

### Constructor

```python
class ModuleLoader:
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
    ) -> None
```

- `components` is optional for CLI compatibility (`ModuleRuntime` can be
  instantiated without them).
- `import_manager` and `stack_manager` are created internally if not
  injected, which makes testing with isolated instances straightforward.

### `load_module` — the 16 steps

```python
async def load_module(
    self,
    module_id: str,
    module_path: Path,
    manifest: ModuleManifest,
) -> RegisteredModule
```

The steps, in order:

1. **Package import** via `ImportManager.import_package`. If a bundle was
   already registered (reload), it is purged first.
2. **Registration of exported ORM classes** (`_register_exported_models`):
   inspects `{module_id}.models`, registers each subclass of `Base` as a
   `weakref` in `ImportManager`, and stores `(classes, tables)` in
   `_module_metadata`.
3. **`routes.py`** — imports `{module_id}.routes` and retrieves `router`
   (`APIRouter`).
4. **`api.py`** — imports `{module_id}.api` and retrieves `api_router`.
5. **Mount HTML router** at `/m/{module_id}` (checks for route conflicts
   before adding).
6. **Mount API router** at `/api/v1/m/{module_id}`.
7. **Events**: imports `{module_id}.events`, calls
   `register_events(bus, module_id)`.
8. **Hooks**: imports `{module_id}.hooks`, calls
   `register_hooks(hooks, module_id)`.
9. **Slots**: imports `{module_id}.slots`, calls
   `register_slots(slots, module_id)`.
10. **Services**: calls `register_services(module_id)` on the services
    facade.
11. **Middleware**: imports the middleware class from
    `manifest.MIDDLEWARE` (dotted path `"module.ClassName"`).
12. **i18n locales**: registers the `module_path/locales/` directory.
13. **Static files**: mounts `module_path/static/{module_id}/` at
    `/static/m/{module_id}/`.
13b. **Components**: discovers, registers, and mounts component routers
    and statics via `discover_module_components`.
14. **OpenAPI cache bust**: `app.openapi_schema = None`.
15. **Registration in `ModuleRegistry`**.
16. **Add middleware to the Starlette stack** via `MiddlewareStackManager`.

### `load_module` rollback

If *any* of steps 3–16 fails, the `except` block undoes exactly what
was completed, in reverse order:

- Removes mounted routes from `app.routes`.
- Calls `bus.unsubscribe_module(module_id)`.
- Calls `hooks.remove_module_hooks(module_id)`.
- Calls `slots.unregister_module(module_id)`.
- Unmounts component routers and statics.
- Removes the module's HTTP clients.
- Unregisters locales.
- Removes the middleware from the stack.
- Removes the static files mount.
- Calls `_drop_module_metadata(module_id)` to clean up SQLAlchemy.
- Calls `_purge_module(module_id)` to clean up `sys.modules`.
- Busts the OpenAPI cache.

```python
# Excerpt from the except block in load_module:
for mount in mounted_routes:
    try:
        self.app.routes.remove(mount)
    except ValueError:
        pass

if events_registered:
    try:
        await self.bus.unsubscribe_module(module_id)
    except Exception:
        pass
# ... (continues with hooks, slots, components, locales, middleware, metadata, purge)
```

### `unload_module` — the unloading steps

```python
async def unload_module(self, module_id: str) -> None
```

1. Removes routes `/m/{module_id}` and `/api/v1/m/{module_id}` from
   `app.routes`.
2. `bus.unsubscribe_module(module_id)`.
3. `hooks.remove_module_hooks(module_id)`.
4. `slots.unregister_module(module_id)`.
4b. Unmounts component routers and statics, calls
   `components.unregister_module(module_id)`.
4c. Removes the module's HTTP clients.
5. Unregisters locales.
6. Removes the static files mount.
7. `unregister_module_services(module_id)`.
8. `_drop_module_metadata(module_id)` — removes tables from `Base.metadata`
   and disposes SQLAlchemy mappers.
9. `_purge_module(module_id)` — removes entries from `sys.modules` via
   `ImportManager`, calls `gc.collect()`, detects zombies.
9b. **Second cleanup pass**: if `_verify_metadata_cleared` detects
   residual tables (left behind because of the drop→purge ordering),
   it explicitly forces `Base.registry._dispose_cls` and
   `Base.metadata.remove`.
8b. Removes middleware from the Starlette stack and calls an extra
   `gc.collect()` to break reference cycles created by
   `BaseHTTPMiddleware`.
9. `registry.unregister(module_id)`.
10. Busts the OpenAPI cache.
11. Final `gc.collect()`.

### `_drop_module_metadata` and `_verify_metadata_cleared`

These two private functions are the answer to the most common error
encountered during reinstallations:

```
InvalidRequestError: Table 'invoice_item' is already defined for this MetaData instance.
```

`_drop_module_metadata` uses the `_module_metadata` dictionary that
`load_module` populated with the module's classes and tables:

```python
def _drop_module_metadata(self, module_id: str) -> None:
    classes, tables = self._module_metadata.pop(module_id, ([], []))
    for tbl in tables:
        Base.metadata.remove(tbl)        # removes the table from MetaData
    for cls in classes:
        mapper = cls.__mapper__
        mapper._dispose()                 # disconnects the mapper
        Base.registry._dispose_cls(cls)  # removes from the central registry
```

`_verify_metadata_cleared` performs a second check after `_purge_module`:
it searches `Base.metadata.tables` for any table whose owning module
matches `module_id`. If it finds any, `unload_module` forces an emergency
cleanup and logs it as a `WARNING`.

### `reload_module`

```python
async def reload_module(self, module_id, module_path, manifest) -> RegisteredModule:
    await self.unload_module(module_id)
    return await self.load_module(module_id, module_path, manifest)
```

Used by `hot_reload` in development mode: reloads code on the fly while
preserving DB state.

---

## `lifecycle.py` — Module hooks

`ModuleLifecycleManager` calls optional functions defined in
`{module_id}/lifecycle.py`. A module is not required to have them.

```python
# Valid hooks (frozenset)
LIFECYCLE_HOOKS = {"on_install", "on_activate", "on_deactivate", "on_uninstall", "on_upgrade"}
```

```python
class ModuleLifecycleManager:
    async def call(
        self,
        module_id: str,
        hook_name: str,
        session: ISession,
        hub_id: UUID,
        **kwargs,       # e.g. from_version, to_version for on_upgrade
    ) -> None

    async def has_hook(self, module_id: str, hook_name: str) -> bool
```

### How `call` works

1. Validates that `hook_name` is in `LIFECYCLE_HOOKS`.
2. Attempts `importlib.import_module(f"{module_id}.lifecycle")`.
   If it does not exist (`ModuleNotFoundError`), returns silently.
3. Gets `getattr(lifecycle_mod, hook_name, None)`. If the module exists
   but does not define the hook, logs a debug message and returns.
4. If the hook is a coroutine (`iscoroutinefunction`), it is `await`ed.
   If it is synchronous, it is called directly (for compatibility with
   simple hooks).
5. If the hook raises, logs it with `logger.exception` and re-raises so
   the orchestrator can decide whether to abort.

### Example `lifecycle.py` in a module

```python
# modules/invoice/lifecycle.py
async def on_install(session, hub_id):
    """Seed initial data."""
    await session.execute("INSERT INTO invoice_settings ...")

async def on_uninstall(session, hub_id):
    """Clean up before tables are dropped."""
    await session.execute("DELETE FROM ...")

async def on_upgrade(session, hub_id, from_version, to_version):
    """Data migration."""
    if from_version < "2.0.0":
        await migrate_invoice_format(session, hub_id)
```

---

## `dependency.py` — Dependency manager

`DependencyManager` handles three problems related to inter-module
dependencies:

1. Verifying that a module's dependencies are installed and active before
   installing it.
2. Preventing a module from being deactivated while another active module
   depends on it (or deactivating them in order if a cascade is
   requested).
3. Topologically sorting modules at boot time so that each dependency is
   loaded before whatever needs it.

### Dependency format

```python
# In ModuleManifest.DEPENDENCIES:
DEPENDENCIES = ["customers", "inventory>=2.0.0", "billing==1.5.0"]
```

The `_DEP_PATTERN` regex extracts `(module_id, op, version)`:

```python
_DEP_PATTERN = re.compile(
    r"^(?P<module_id>[a-z][a-z0-9_]*)"
    r"(?:(?P<op>>=|<=|==|!=|>|<)(?P<version>\d+\.\d+\.\d+))?$"
)
```

### `DependencyCheckResult`

```python
@dataclass
class DependencyCheckResult:
    ok: bool = True
    missing: list[str]          # not present in the hub's DB
    inactive: list[str]         # installed but not active
    version_mismatch: list[tuple[str, str, str]]  # (dep_id, required, actual)
    auto_installable: list[str]
```

### Main methods

**`check_install_deps(session, manifest, **filters)`**

Iterates `manifest.DEPENDENCIES`, looks up each `module_id` in the DB,
and checks its status and version. Returns `DependencyCheckResult.ok=True`
only if all dependencies are active and their versions are compatible.

**`check_can_deactivate(session, module_id, **filters)`**

Searches the DB for all active modules whose `manifest["dependencies"]`
contains `module_id`. If any exist, it builds the cascade order (BFS)
and returns `DeactivateCheckResult(can_deactivate=False, dependents=[...])`.

**`check_can_uninstall(session, module_id, **filters)`**

More restrictive than deactivate: searches for modules in status
`active|installed|disabled` that depend on this one. Returns
`UninstallCheckResult`. Uninstall is never cascaded; the user must
uninstall dependents first.

**`resolve_load_order(modules: list[dict]) -> list[dict]`**

Pure topological sort for boot. Algorithm:

1. Removes modules whose dependencies are not in the available set.
2. Computes the in-degree of each node.
3. Kahn's algorithm: queue of nodes with in-degree=0, processes in order.
4. If any nodes remain unprocessed at the end, there is a cycle; it is
   logged as an error and those modules are excluded.

**`deactivate_cascade(session, module_id, runtime, **filters)`**

When the user confirms a cascade deactivation, calls
`runtime.deactivate(session, hub_id, mid, cascade=False)` for each
dependent module in the order computed by `_build_cascade_order`.

---

## `s3_source.py` — Downloading from S3

`S3ModuleSource` manages the download, verification, and caching of
modules stored in AWS S3. It is the code source in production
environments where modules are distributed as artifacts in an S3 bucket.

### S3 key convention

```
cloud/modules/{module_id}/v{version}.zip
```

The key is built with `build_module_object_key(module_id, version)`.
The module never stores the full URL in the DB; it reconstructs it on
demand.

### Constructor

```python
class S3ModuleSource:
    def __init__(
        self,
        bucket: str,
        cache_dir: Path,   # /tmp/modules/ on ECS
        region: str | None = None,
    )
```

Requires `aioboto3` (installed with `pip install aioboto3`). Raises
`ImportError` if it is not available, preventing silent failures.

### Main API

**`download(module_id, version, expected_sha256="") -> Path`**

1. Builds the S3 key.
2. Checks whether the local cache directory exists AND the ETag matches
   the stored one. Cache hit → returns the local path without downloading.
3. Downloads the object bytes from S3 with exponential retry (3 attempts,
   delays of 1, 2, 4 seconds).
4. Verifies the SHA-256 with `hashlib.sha256`. Raises `IntegrityError`
   if it does not match.
5. Extracts the archive (ZIP or tar.gz) to `cache_dir/{module_id}/`.
6. Saves the ETag in memory and on disk (`.{module_id}.etag`).

**`download_many(modules: list[tuple[str,str,str]]) -> dict[str, Path]`**

Parallel download via `asyncio.gather`. Individual failures are logged
and excluded from the result; they do not abort the rest.

**`load_cached_etags()`**

On process startup, reads all `.{module_id}.etag` files from disk to
restore the ETag cache. Without this, a warm container re-downloads all
modules on every startup.

**`clear_cache(module_id=None)`**

Deletes the cache directory and the ETag. If `module_id=None`, clears
everything.

### Safe extraction

`_extract` filters out entries with absolute paths or `..` to prevent
path traversal attacks:

```python
if info.filename.startswith("/") or ".." in info.filename:
    logger.warning("Skipping unsafe zip member: %s", info.filename)
    continue
```

It also detects and strips the common directory prefix in ZIPs
(e.g. `assistant/module.py` → `module.py`).

---

## `marketplace_client.py` — Marketplace HTTP client

`MarketplaceClient` implements the HTTP protocol to resolve and download
modules from any server that implements the endpoint:

```
GET {base_url}/{module_id}/resolve/
GET {base_url}/{module_id}/resolve/?version=2.4.7

Response:
{
    "module_id": "sales",
    "version": "2.4.7",
    "download_url": "https://cdn.example.com/modules/sales/v2.4.7.zip",
    "checksum_sha256": "abc123...",
    "dependencies": ["customers>=2.0.0", "inventory"],
    "size_bytes": 204800
}
```

### `ModuleDownloadInfo`

```python
@dataclass
class ModuleDownloadInfo:
    module_id: str
    version: str
    download_url: str
    checksum_sha256: str = ""
    dependencies: list[str] = field(default_factory=list)
    size_bytes: int = 0
```

### `MarketplaceClient`

```python
class MarketplaceClient:
    def __init__(self, base_url: str, timeout: float = 60.0)

    async def resolve(self, module_id, version=None) -> ModuleDownloadInfo
    async def download(self, download_url, dest_dir, checksum="") -> Path
    async def resolve_all_dependencies(
        self, module_id, version=None, *, already_installed=None
    ) -> list[ModuleDownloadInfo]
    @staticmethod
    def _extract_zip(zip_path, dest_dir) -> Path
```

**`resolve`** — issues `GET {base_url}/{module_id}/resolve/`, raises
`MarketplaceError` for 404 and other HTTP errors.

**`download`** — downloads the ZIP to a temporary file, verifies the
SHA-256 checksum, extracts with `_extract_zip`, returns the path to the
module directory.

**`resolve_all_dependencies`** — BFS over the dependency graph, visiting
the root module and all its transitive dependencies. At the end it
topologically sorts the results (dependencies first). Handles cycles with
a warning log and continues.

**`_extract_zip`** — extracts the ZIP to `dest_dir`. Detects path
traversal via absolute paths or `..`. Looks for `module.py` in the root
or one level down. Derives the module name from the directory found
(strips version suffixes with `-`).

---

## `boundary.py` — Error isolation barrier

`ModuleBoundaryMiddleware` is a Starlette `BaseHTTPMiddleware` that acts
as a firewall between a module's code and the Hub Core.

### The problem it solves

Without this middleware, an unhandled exception in a module route
(`/m/invoice/orders`) can:

1. Propagate to the FastAPI/Starlette global error handler.
2. Expose a traceback in the response.
3. In the worst case, leave the app in an inconsistent state.

And if the module has a systematic bug (e.g. fails on every request),
there is no mechanism to detect it and notify the operator.

### Scope

The middleware intercepts **only** module routes:

```python
_MODULE_URL = re.compile(r"^/(?:api/v1/)?m/([a-z0-9_-]+)(?:/|$)")
```

- `/m/{module_id}/...` — HTML routes.
- `/api/v1/m/{module_id}/...` — API routes.

Hub Core routes (`/health`, `/dashboard`, `/ws/_live`, etc.) pass
through untouched.

### `_ModuleErrorTracker`

For each module, it maintains a sliding window of errors:

```python
@dataclass
class _ModuleErrorTracker:
    threshold: int = 10
    window_seconds: float = 60.0
    errors: deque[float] = field(default_factory=lambda: deque(maxlen=50))

    def record(self) -> bool:
        """Adds a timestamp; returns True if the threshold has been reached."""
        now = time.monotonic()
        self.errors.append(now)
        cutoff = now - self.window_seconds
        while self.errors and self.errors[0] < cutoff:
            self.errors.popleft()
        return len(self.errors) >= self.threshold

    def reset(self) -> None:
        self.errors.clear()
```

Uses `time.monotonic()` (not `time.time()`) to be immune to system clock
jumps (NTP adjustments). The `deque` is capped at 50 elements to keep
memory usage O(threshold) per module.

### `dispatch` flow

```python
async def dispatch(self, request, call_next):
    module_id = self._extract_module_id(request.url.path)
    if module_id is None:
        return await call_next(request)  # not a module route → pass-through

    try:
        return await call_next(request)
    except Exception as exc:
        logger.exception(...)
        await self._handle_error(request, module_id, exc)
        return self._render_error(request, module_id, exc)
```

### `_handle_error`

1. Records the error in `_ModuleErrorTracker.record()`.
2. Emits `module.error` on the `AsyncEventBus` (if available in
   `app.state.event_bus`).
3. If the threshold is reached, calls `_mark_degraded` and emits
   `module.degraded`.

### `_mark_degraded`

Attempts to persist `status='degraded'` in the DB:
1. First uses `request.state.session` (the request session, if a DB
   middleware is present).
2. If not, opens a transient session with `get_session_factory()`.

Both branches are best-effort; if they fail, the error is logged but the
contained response still reaches the client.

### `_render_error`

Returns a contained response without using Jinja2 (avoids a double
failure if the template engine is broken). If the request is an API
route or the client accepts JSON:

```json
{
    "error": "module_unavailable",
    "module_id": "invoice",
    "detail": "Module 'invoice' raised an unhandled exception...",
    "error_type": "RuntimeError"
}
```

For HTML routes: a minimal hardcoded `<html>` with an error message.

### Public API

```python
class ModuleBoundaryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, threshold=10, window_seconds=60.0)
    def reset_tracker(self, module_id: str) -> None
    async def dispatch(self, request, call_next) -> Response
```

`reset_tracker` is called by the marketplace reactivation endpoint when
the user decides to give the module a second chance.

---

## `module_runtime.py` — The central orchestrator

`ModuleRuntime` is the heart of the engine. It brings all the subsystems
together into a single entry point that both REST endpoints and the
marketplace UI HTML views can use.

### Constructor

```python
class ModuleRuntime:
    def __init__(
        self,
        app: FastAPI,
        settings: HotframeSettings,
        event_bus: AsyncEventBus,
        hooks: HookRegistry,
        slots: SlotRegistry,
        components: ComponentRegistry | None = None,
    ) -> None:
        self.registry = ModuleRegistry()
        self.loader = ModuleLoader(app, registry, event_bus, hooks, slots, components)
        self.state = ModuleStateDB()
        self.s3 = S3ModuleSource(...) if settings.MODULE_SOURCE == "s3" else None
        self.deps = DependencyManager()
        self.lifecycle = ModuleLifecycleManager()
        self.migrations = ModuleMigrationRunner()
        self.watcher = ModuleWatcher()
```

### Result dataclasses

Each operation returns a specific dataclass:

```python
@dataclass
class InstallResult:
    success: bool
    module_id: str
    version: str
    error: str | None
    auto_installed: list[str]

@dataclass
class ActivateResult:
    success: bool; module_id: str; error: str | None

@dataclass
class DeactivateResult:
    success: bool; module_id: str; error: str | None
    dependents: list[str]       # modules blocking the deactivation
    cascade_order: list[str]    # proposed order if a cascade is confirmed
    cascaded: list[str]         # modules that were deactivated in the cascade

@dataclass
class UninstallResult:
    success: bool; module_id: str; error: str | None
    dependents: list[tuple[str, str]]   # (module_id, status) that are blocking

@dataclass
class UpdateResult:
    success: bool; module_id: str
    from_version: str; to_version: str; error: str | None
```

### `boot` and `boot_all_active_modules`

**`boot(session, hub_id, skip_db_writes=False)`**

Startup sequence for a hub:

1. Restores ETags from disk (if S3 is configured).
2. Queries `ModuleStateDB.get_active_modules(session, hub_id=hub_id)`.
3. For modules without a version, attempts to resolve it from the
   catalogue.
4. Calls `_ensure_module_code` to guarantee the code is on disk
   (downloading from S3 if necessary).
5. Topologically sorts with `DependencyManager.resolve_load_order`.
6. Loads each module with `_load_from_path`.
7. In `DEBUG` mode, starts `ModuleWatcher` for automatic hot-reload.

The `skip_db_writes` parameter is an optimisation for multi-worker
environments: a worker that did not acquire the Postgres advisory lock
still needs to mount routes in *its* FastAPI process, but must not write
to the DB to avoid deadlocks.

**`boot_all_active_modules(session) -> int`**

Automatically detects whether the model has `hub_id` (multi-tenant) and,
if so, iterates all hubs with active modules. Uses a per-hub Postgres
advisory lock to serialise DB writes in multi-worker environments.

```python
# Advisory lock: BLAKE2b of hub_id → signed int64
key = _hub_id_to_advisory_key(str(hub_id))
result = await session.execute(
    text("SELECT pg_try_advisory_xact_lock(:key)"),
    {"key": key},
)
acquired = bool(result.first()[0])
```

BLAKE2b is used instead of `hash()` deliberately: `hash()` is salted
per process in Python 3.3+ and would produce different keys in each
worker.

### `install` — phase pipeline

```python
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
) -> InstallResult
```

Internally uses a `HotMountPipeline` with 6 phases:

| Phase | Private method | Effect | Rollback |
|-------|---------------|--------|----------|
| `DOWNLOADING` | `_phase_download` | Downloads code to `MODULES_DIR` | `shutil.rmtree(target_path)` |
| `VALIDATING` | `_phase_validate` | Validates manifest; renames dir if `MODULE_ID` ≠ catalogue key | Log (DB row does not exist yet to undo) |
| `VALIDATING` | `_phase_check_deps` | Checks dependencies in DB | No-op |
| `MIGRATING` | `_phase_migrate` | Creates row in DB (`status='installing'`); runs Alembic `upgrade` | Alembic `downgrade`; deletes DB row |
| `IMPORTING` | `_phase_on_install` | Calls `lifecycle.on_install` | No-op (see note) |
| `MOUNTING` | `_phase_mount` | `loader.load_module`; refreshes Jinja2 templates | `loader.unload_module` |
| `STACK_REBUILD` | `_phase_activate` | `lifecycle.on_activate`; `state.activate` (DB to `active`) | No-op (MIGRATING undoes it) |

The download source resolution in `_phase_download` follows this order:
1. `source` is a URL → `MarketplaceClient.download`.
2. `source` is a local `.zip` → `MarketplaceClient._extract_zip`.
3. Module already exists in `MODULES_DIR` → no-op.
4. `MODULE_MARKETPLACE_URL` configured → `MarketplaceClient.resolve +
   download`.
5. `S3ModuleSource` configured → `S3ModuleSource.download`.
6. If none applies → `RuntimeError`.

### `activate` — re-activating a deactivated module

```python
async def activate(self, session, hub_id, module_id) -> ActivateResult
```

Does not use a pipeline (it is a more linear operation). Steps:

1. Verifies the module is in state `disabled|installed|error`.
2. Ensures the code is on disk (downloads from S3 if missing).
3. Validates the manifest.
4. Checks dependencies.
5. `loader.load_module`.
6. Refreshes templates.
7. `lifecycle.on_activate`.
8. `state.activate` (DB to `active`).
9. Emits `module.activated`.

If anything fails, attempts `loader.unload_module` if the module had
already been loaded, and calls `state.set_error`.

### `deactivate`

```python
async def deactivate(self, session, hub_id, module_id, cascade=False) -> DeactivateResult
```

1. Verifies the module is `active`.
2. Rejects `is_system` modules.
3. Checks active dependents with `DependencyManager.check_can_deactivate`.
4. If `cascade=False` and there are dependents → returns an error with
   the list (the UI can display a confirmation dialog).
5. If `cascade=True` → `DependencyManager.deactivate_cascade` (deactivates
   in reverse order).
6. `lifecycle.on_deactivate`.
7. `loader.unload_module`.
8. `state.deactivate` (DB to `disabled`).
9. Emits `module.deactivated`.

### `uninstall`

```python
async def uninstall(self, session, hub_id, module_id) -> UninstallResult
```

More destructive than deactivate; never cascaded:

1. Rejects `is_system` modules.
2. Checks that **no** module (regardless of status) depends on this one.
3. If it was active: `lifecycle.on_deactivate` + `loader.unload_module`.
4. `lifecycle.on_uninstall` (if it fails → aborts to prevent data loss).
5. Reverts Alembic migrations if any exist.
6. `state.delete` (deletes the DB row).
7. `S3ModuleSource.clear_cache` if S3 is configured.
8. Refreshes templates.
9. Emits `module.uninstalled`.

### `update`

```python
async def update(self, session, hub_id, module_id, new_version, checksum="", source=None) -> UpdateResult
```

Update process with partial rollback:

1. Downloads the new version (same resolution logic as `install`).
2. Validates the new manifest.
3. If the module was active: `lifecycle.on_deactivate` + `loader.unload_module`.
4. Runs Alembic `upgrade` with the new code.
5. `lifecycle.on_upgrade(from_version=..., to_version=...)`.
6. `loader.load_module` with the new version.
7. If it was active: `lifecycle.on_activate`.
8. `state.activate` + updates `version` and `checksum_sha256` in the DB.
9. Emits `module.updated`.

If step 6 (`load_module`) fails, it attempts to reload the old version:

```python
# Partial rollback in the except block:
if was_active and not self.registry.is_loaded(module_id):
    old_path = Path(self.settings.MODULES_DIR) / module_id
    if old_path.exists():
        old_manifest = load_manifest(old_path)
        await self.loader.load_module(module_id, old_path, old_manifest)
        logger.warning("Update rollback: reloaded previous version of %s", module_id)
```

### `hot_reload` (development mode)

```python
async def hot_reload(self, module_id: str) -> bool
```

Reloads Python code without touching the DB or S3. Only available when
`DEBUG=True`:

1. Validates the module is loaded.
2. Reloads the manifest from disk.
3. Checks that dependencies are still loaded.
4. `loader.reload_module` (unload + load).

### Emitted events

| Event | When |
|-------|------|
| `module.installed` | On successful completion of `install` |
| `module.activated` | On successful completion of `activate` |
| `module.deactivated` | On successful completion of `deactivate` |
| `module.uninstalled` | On successful completion of `uninstall` |
| `module.updated` | On successful completion of `update` |
| `module.error` | On each exception caught by `ModuleBoundaryMiddleware` |
| `module.degraded` | When the `ModuleBoundaryMiddleware` tracker crosses the threshold |

---

## The complete module lifecycle

### State diagram

```
Not installed
     │
     │ install()
     ▼
  installing ──────────── error (failure in any phase)
     │                       │
     │ (pipeline completes)  │ activate() (if the problem is fixed)
     ▼                       │
   active ◄──────────────────┘
     │  ▲
     │  │ activate()
     ▼  │
  disabled
     │
     │ uninstall()
     ▼
Not installed

active ──── many failed requests ──► degraded
```

### `install` flow step by step

```
User / CLI / Marketplace UI
    │
    │ ModuleRuntime.install(session, hub_id, "invoice", version="2.0.0")
    │
    ├─ HotMountPipeline("invoice")
    │
    ├─ DOWNLOADING (_phase_download)
    │   ├─ direct URL → MarketplaceClient.download()
    │   ├─ local .zip → MarketplaceClient._extract_zip()
    │   ├─ already on disk → no-op
    │   ├─ MODULE_MARKETPLACE_URL → MarketplaceClient.resolve + download
    │   └─ S3 → S3ModuleSource.download()
    │       └─ → /app/modules/invoice/
    │
    ├─ VALIDATING (_phase_validate)
    │   ├─ load_manifest(module_path) → ModuleManifest
    │   └─ if MODULE_ID ≠ key → rename dir + update HubModuleVersion
    │
    ├─ VALIDATING (_phase_check_deps)
    │   └─ DependencyManager.check_install_deps()
    │       ├─ missing → RuntimeError
    │       ├─ version_mismatch → RuntimeError
    │       └─ inactive + auto_install_deps=False → RuntimeError
    │
    ├─ MIGRATING (_phase_migrate)
    │   ├─ ModuleStateDB.create(status='installing')
    │   └─ ModuleMigrationRunner.upgrade() → Alembic upgrade head
    │
    ├─ IMPORTING (_phase_on_install)
    │   └─ ModuleLifecycleManager.call("on_install") → invoice/lifecycle.py
    │
    ├─ MOUNTING (_phase_mount)
    │   ├─ ModuleLoader.load_module()
    │   │   ├─ ImportManager.import_package("invoice")
    │   │   ├─ Mounts /m/invoice/ + /api/v1/m/invoice/
    │   │   ├─ Registers events, hooks, slots, services, components
    │   │   └─ Adds middleware to Starlette stack
    │   └─ _refresh_templates() → Jinja2 rescanning
    │
    ├─ STACK_REBUILD (_phase_activate)
    │   ├─ ModuleLifecycleManager.call("on_activate")
    │   └─ ModuleStateDB.activate(status='active', manifest=...)
    │
    ├─ pipeline.commit()
    │
    └─ bus.emit("module.installed")
```

### `deactivate` flow step by step

```
ModuleRuntime.deactivate(session, hub_id, "invoice")
    │
    ├─ ModuleStateDB.get_module() → verifies status=='active'
    │
    ├─ DependencyManager.check_can_deactivate()
    │   └─ if active dependents exist AND cascade=False → return error
    │
    ├─ [cascade=True] DependencyManager.deactivate_cascade()
    │   └─ deactivates in LIFO order: billing → invoice
    │
    ├─ ModuleLifecycleManager.call("on_deactivate")
    │
    ├─ ModuleLoader.unload_module()
    │   ├─ Removes routes from app.routes
    │   ├─ bus.unsubscribe_module("invoice")
    │   ├─ hooks.remove_module_hooks("invoice")
    │   ├─ slots.unregister_module("invoice")
    │   ├─ Unmounts components, locales, statics
    │   ├─ _drop_module_metadata("invoice") → SQLAlchemy cleanup
    │   ├─ ImportManager.purge("invoice") → sys.modules cleanup
    │   ├─ gc.collect() × 2
    │   └─ registry.unregister("invoice")
    │
    ├─ ModuleStateDB.deactivate(status='disabled')
    │
    └─ bus.emit("module.deactivated")
```

### `update` flow with version rollback

```
ModuleRuntime.update(session, hub_id, "invoice", "2.1.0")
    │
    ├─ Download new version → /app/modules/invoice/ (overwrites)
    ├─ Validate new manifest
    ├─ lifecycle.on_deactivate + loader.unload_module  (if it was active)
    ├─ Alembic upgrade head (v2.1.0 migrations)
    ├─ lifecycle.on_upgrade(from_version="2.0.0", to_version="2.1.0")
    │
    ├─ loader.load_module  ←── if this FAILS:
    │                              attempts to reload v2.0.0 from disk
    │
    ├─ lifecycle.on_activate
    ├─ state.activate + updates version/checksum in DB
    └─ bus.emit("module.updated")
```

---

## How it fits into the rest of the framework

### Bootstrap (`create_app`)

In `hotframe/bootstrap.py`, `create_app` creates the `ModuleRuntime` and
stores it in `app.state.module_runtime`. During the lifespan startup it
calls `runtime.boot_all_active_modules(session)`.

```python
# In create_app (simplified):
module_runtime = ModuleRuntime(
    app=app,
    settings=settings,
    event_bus=event_bus,
    hooks=hook_registry,
    slots=slot_registry,
    components=component_registry,
)
app.state.module_runtime = module_runtime

@app.lifespan
async def lifespan(_app):
    async with get_db_session() as session:
        await module_runtime.boot_all_active_modules(session)
    yield
    await module_runtime.shutdown()
```

### `ModuleRegistry`

`ModuleRuntime` uses `ModuleRegistry` (from `hotframe/apps/registry.py`)
as the in-memory registry of currently loaded modules.
`registry.is_loaded(module_id)` lets you check whether a module is in
memory before attempting to load or unload it.

### `ModuleMigrationRunner`

Accessed as `self.migrations`, it runs each module's Alembic migrations
in isolation. Each module has its own `migrations/` directory with its
own `env.py`. The runner converts an async URL (`asyncpg`) to a
synchronous one (`psycopg2`) so Alembic can use it.

### `MiddlewareStackManager`

When a module declares `MIDDLEWARE = "invoice.middleware.InvoiceMiddleware"`
in its manifest, `ModuleLoader` delegates to `MiddlewareStackManager` to
add or remove the middleware class from the Starlette stack
*atomically*: it builds the entire new stack and installs it in one
operation, with no window of inconsistency.

### `AsyncEventBus`

`ModuleLoader` calls `bus.unsubscribe_module(module_id)` on unload. This
requires the bus to implement a per-module tracking mechanism: when a
module subscribes to events in its `events.py`, it registers the
`module_id` as the owner of each subscription. On deactivation, all
subscriptions for that module are removed in bulk.

### `SlotRegistry` and `ComponentRegistry`

Analogous to the bus: `SlotRegistry` has `unregister_module(module_id)`
and `ComponentRegistry` has `unregister_module(module_id)`. Both remove
all of the module's registrations in bulk, ensuring that templates that
render slots or use module components simply find no entries (silent
behaviour, no exception).

### Jinja2 templates

`_refresh_templates()` calls `refresh_template_dirs(templates,
MODULES_DIR)`, which rescans `modules/*/templates/` and updates the
`search_path` of the Jinja2 loader. Without this step, the templates of
a newly activated module would be invisible.

---

## Gotchas and design decisions

### 1. `sys.modules` matching is exact, not prefix-based

In earlier versions of the engine the approach was:
```python
# Bad: deletes "invoiceapp" if the module is named "invoice"
for key in list(sys.modules.keys()):
    if key == "invoice" or key.startswith("invoice."):
        del sys.modules[key]
```

`ImportManager` solves this with a before/after snapshot of the import:
it only removes exactly the entries that *that* import created.

### 2. `Table 'x' is already defined` — the SQLAlchemy leak

The most common error during reinstallations. It occurs when:
1. `invoice.models` is imported → SQLAlchemy registers `invoice_item` in
   `Base.metadata`.
2. `sys.modules["invoice.models"]` is purged, but the `Table` object and
   the mapper remain in `Base.metadata` and `Base.registry`.
3. `invoice.models` is imported again → SQLAlchemy finds the table
   already registered.

The fix is the sequence `_drop_module_metadata` → `_purge_module` →
`_verify_metadata_cleared` with an emergency cleanup if the verification
fails.

### 3. Memory leaks — three vectors (see `test_unload_leaks.py`)

The test `test_install_uninstall_cycle_stable_memory` runs 50 cycles and
measures RSS. The budget is 256 KB/cycle. The three known vectors:

**Vector A — HTTP clients**: a module that registers a named HTTP client
and does not unregister it in `on_deactivate`. The loader calls
`http_clients.unregister_module(module_id)` as a safety net.

**Vector B — Starlette middleware stack**: when rebuilding the stack,
`BaseHTTPMiddleware` creates closures that can retain the old middleware
via reference cycles. The loader calls `gc.collect()` immediately after
`stack_manager.remove_and_rebuild`.

**Vector C — SQLAlchemy mappers**: if dispose is not called in the
correct order, the mapper registry retains references to module classes
through internal weak sets that are only released with `gc.collect()`.
The loader calls `gc.collect()` at the end of `unload_module`.

### 4. Postgres advisory lock for multi-worker boot

With `uvicorn --workers 4`, all four workers execute `boot` in parallel.
Without the lock, all four would concurrently write
`UPDATE hub_module SET manifest=...` for the same rows, causing deadlocks
and flipping modules to `error`.

The solution: `pg_try_advisory_xact_lock` with a key derived from
`hub_id` via BLAKE2b. The first worker to acquire it performs the DB
writes. The others mount routes in their process (necessary, since each
worker has its own FastAPI app in memory) but do not touch the DB.

### 5. `MODULE_STATE_MODEL` — swappable model

The engine never directly references `hotframe.engine.models.Module` in
its queries. It always uses `_get_module_model()`. This allows
multi-tenant projects to swap the model for one that includes `hub_id`:

```python
# settings.py
MODULE_STATE_MODEL = "myproject.modules.HubModule"
```

```python
# myproject/modules.py
class HubModule(Base):
    __tablename__ = "hub_module"
    hub_id: Mapped[UUID]
    module_id: Mapped[str]
    # ... all fields from Module + hub_id
```

### 6. `is_system` — non-uninstallable modules

System modules (auth, core, shared) can declare `IS_SYSTEM=True` in
their `ModuleManifest`. `deactivate` and `uninstall` check this field
and return an error before doing anything:

```python
if mod.is_system:
    result.error = f"Cannot deactivate system module '{module_id}'"
    return result
```

### 7. No rollback for `on_install`

The rollback for the `IMPORTING` phase is a deliberate no-op. If
`on_install` creates data in the DB and the pipeline then fails at
`MOUNTING`, that data is left in place. The rationale: `on_install` must
be idempotent and its cleanup is the responsibility of `on_uninstall`,
which runs in the normal uninstall flow. Calling `on_uninstall` from the
install rollback makes no sense because the migrations would not yet have
been reverted.

### 8. Degraded vs. error

`degraded` means "still mounted but failing too often": routes are still
responding (with 503), the module is still in `sys.modules`, but the UI
warns the user and suggests deactivating it. `error` means "failed to
load": the routes do not exist at all.

`get_active_modules` does not return `degraded` rows, so the next
process restart leaves the module unmounted until the operator explicitly
reactivates it (which resets the tracker via `reset_tracker`).

### 9. Hot-reload in development

`ModuleWatcher` (from `hotframe/dev/autoreload.py`) watches for changes
in `MODULES_DIR` and calls `runtime.hot_reload(module_id)` automatically
when `DEBUG=True`. Hot-reload does **not** touch the DB or run
migrations; it only reloads Python code. Model changes require
`hf makemigrations + hf migrate` to be run manually.

---

## Reference tests

### `test_boundary.py`

Builds a minimal Starlette app with a fake session middleware and a
module route that raises an exception. Verifies:

- Contained 503 response without affecting `/health`.
- JSON vs HTML based on the route (`/api/v1/m/...` vs `/m/...`).
- `module.error` and `module.degraded` on the bus.
- `_ModuleErrorTracker.record()` does not degrade before the threshold.
- `reset_tracker` clears the history.

### `test_module_metadata_lifecycle.py`

Unit test for `_register_exported_models` and `_drop_module_metadata`
using SQLAlchemy models created dynamically with `type()`. Cases:

- Register + drop removes the table from `Base.metadata`.
- Two install→unload→install cycles do not raise "Table already defined".
- `_drop_module_metadata` on an unknown module is idempotent.
- `_verify_metadata_cleared` detects residual tables but ignores those
  belonging to other modules.

### `test_unload_leaks.py`

Creates a fake module on disk with real (not mocked) `routes.py` and
`models.py`, runs 50 `load_module`/`unload_module` cycles, and measures
RSS with `psutil` (or `resource.getrusage` if not available). Assertions:

- `fake_leakcheck` is not in `Base.metadata.tables` at the end.
- RSS growth is `< 256 KB/cycle`.
