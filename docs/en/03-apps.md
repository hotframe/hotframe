# 3. Static apps (`apps/`)

> `apps/` defines the declarative contract for apps and modules: what they are, how they describe themselves, how they register in memory, and how they expose permissioned services. It is the conceptual backbone on which the engine and the discovery layer build everything else.

---

## What this folder is for

When the GUIDE talks about "Apps vs modules: the key concept", it is referring to the abstractions that live here. `apps/` provides:

1. **`AppConfig` and `ModuleConfig`** (`config.py`) — the base classes that every user-written `app.py` and `module.py` must subclass. They are the identity declaration for an app or module.
2. **`ModuleManifest`** (`config.py`) — the strict Pydantic schema that validates the loose attributes on a legacy-style `module.py` (the legacy contract).
3. **`AppRegistry` and `ModuleRegistry`** (`registry.py`) — the in-memory registries that the engine uses as the source of truth for what is currently loaded in the process.
4. **`ModuleService` and the `@action` decorator** (`service_facade.py`) — the service layer with declarative permissions, response helpers, and the global `SERVICE_REGISTRY`.

---

## File map

| File | Responsibility |
|---|---|
| [`__init__.py`](../src/hotframe/apps/__init__.py) | Re-exports all public symbols from the package. |
| [`config.py`](../src/hotframe/apps/config.py) | Defines `AppConfig`, `ModuleConfig`, `ModuleManifest`, `MenuConfig`, `NavigationItem`, `load_manifest()`, `manifest_to_dict()`. |
| [`registry.py`](../src/hotframe/apps/registry.py) | Defines `RegisteredModule`, `ModuleRegistry` (legacy) and `AppRegistry` (new contract). |
| [`service_facade.py`](../src/hotframe/apps/service_facade.py) | Defines `ModuleService`, `@action`, `ActionMeta`, `ActionEntry`, `ServiceEntry`, `SERVICE_REGISTRY`, `register_services()`, `unregister_module_services()`, `generate_module_context()`. |

---

## `config.py` — AppConfig, ModuleConfig, and ModuleManifest

### Navigation and menu sub-models

#### `MenuConfig`

```python
class MenuConfig(BaseModel):
    label: str
    icon: str = "cube-outline"
    order: int = 50
```

Configures the module's entry in the navigation sidebar. `order` controls position: lower numbers appear first. If a module does not define `MENU`, it does not appear in the sidebar.

#### `NavigationItem`

```python
class NavigationItem(BaseModel):
    label: str
    icon: str
    id: str
    view: str = ""
```

A tab within a module's internal navigation bar. `id` is the section identifier; `view` is the route or view name to load.

---

### `ModuleManifest`

```python
class ModuleManifest(BaseModel):
    MODULE_ID: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    MODULE_NAME: str
    MODULE_VERSION: str = Field(pattern=r"^\d+\.\d+\.\d+")
    MODULE_ICON: str = "cube-outline"
    MODULE_DESCRIPTION: str = ""
    MODULE_AUTHOR: str = ""
    HAS_MODELS: bool = False
    MENU: MenuConfig | None = None
    NAVIGATION: list[NavigationItem] = []
    PERMISSIONS: list[str] = []
    ROLE_PERMISSIONS: dict[str, list[str | tuple]] = {}
    DEPENDENCIES: list[str] = []
    MIDDLEWARE: str | None = None
    SCHEDULED_TASKS: list[dict] = []
    PRICING: dict | None = None
```

The **legacy** contract for `module.py`. Instead of subclassing `ModuleConfig`, older modules declare uppercase constants directly in `module.py`. `load_manifest()` extracts them and `ModuleManifest` validates them.

Key validation constraints:
- `MODULE_ID` must match `^[a-z][a-z0-9_]*$` — lowercase letters, digits, and underscores only, starting with a letter.
- `MODULE_VERSION` must be semver: `^\d+\.\d+\.\d+`.
- If `MODULE_ID` or `MODULE_VERSION` fail validation, the module is put into an `error` state and cannot be loaded.

#### `normalize_permissions` validator

```python
@field_validator("PERMISSIONS", mode="before")
@classmethod
def normalize_permissions(cls, v: Any) -> list[str]:
    result = []
    for item in v:
        if isinstance(item, (tuple, list)):
            result.append(str(item[0]))
        else:
            result.append(str(item))
    return result
```

Accepts both plain strings `"codename"` and tuples `("codename", "description")`. The description is discarded here; only the permission code is kept.

---

### `load_manifest(module_path: Path) -> ModuleManifest`

```python
def load_manifest(module_path: Path) -> ModuleManifest:
```

Imports `module.py` from `module_path` using `importlib.util.spec_from_file_location` with a temporary name (`_manifest_loader_<name>`), executes the module, extracts attributes that match `ModuleManifest` fields, and constructs the validated instance. The temporary module is removed from `sys.modules` before returning to avoid name collisions.

This mechanism allows reading a module's manifest **without leaving that module loaded in the process**. It is the same strategy the engine uses to inspect a module before deciding whether to install it.

```python
try:
    manifest = load_manifest(Path("modules/invoice"))
    print(manifest.MODULE_NAME, manifest.MODULE_VERSION)
except FileNotFoundError:
    print("No module.py found")
except ValidationError as e:
    print("Invalid manifest:", e)
```

---

### `manifest_to_dict(manifest: ModuleManifest) -> dict`

```python
def manifest_to_dict(manifest: ModuleManifest) -> dict[str, Any]:
```

Serializes the manifest using short, readable keys instead of the Pydantic model's uppercase names. The mapping is:

| Pydantic key | Dict key |
|---|---|
| `MODULE_ID` | `module_id` |
| `MODULE_NAME` | `name` |
| `MODULE_VERSION` | `version` |
| `MODULE_ICON` | `icon` |
| `MODULE_DESCRIPTION` | `description` |
| `MODULE_AUTHOR` | `author` |
| `HAS_MODELS` | `has_models` |
| `MENU` | `menu` |
| `NAVIGATION` | `navigation` |
| `PERMISSIONS` | `permissions` |
| `ROLE_PERMISSIONS` | `role_permissions` |
| `DEPENDENCIES` | `dependencies` |
| `MIDDLEWARE` | `middleware` |
| `SCHEDULED_TASKS` | `scheduled_tasks` |
| `PRICING` | `pricing` |

The result is stored in the `manifest` column of the `hub_module` table (JSON), which lets templates and APIs access `manifest.name` instead of `manifest.MODULE_NAME`.

---

### `AppConfig`

```python
class AppConfig:
    name: str = ""
    verbose_name: str = ""
    mount_prefix: str = ""        # empty → f"/{name}/"
    media_path: str = ""          # empty → uses app name
    version: str = "0.1.0"
    depends: list[str] = []
    permissions: list[tuple[str, str]] = []
    role_permissions: dict[str, list[str]] = {}
    menu: dict | None = None
    navigation: list[dict] = []
    is_builtin: bool = False
    _abstract: bool = False
```

The base class that every `apps/<name>/app.py` must subclass. Unlike the constant-based contract of `ModuleManifest`, this contract is object-oriented: class attributes and a `ready()` method.

#### `__init_subclass__`

```python
def __init_subclass__(cls, **kwargs) -> None:
    super().__init_subclass__(**kwargs)
    if "_abstract" not in cls.__dict__:
        cls._abstract = False
    if cls._abstract:
        return
    if not cls.name:
        raise ValueError(f"{cls.__name__}: AppConfig subclass must define 'name'")
```

Runs at class definition time. If `_abstract` is not in the subclass's own `__dict__` (i.e., it was not declared explicitly), it is reset to `False`. This prevents `ModuleConfig`'s `_abstract=True` flag from being accidentally inherited by a concrete user module subclass. After that, if `_abstract=False` and `name` is empty, a `ValueError` is raised at import time — failing fast with a clear message.

#### `async def ready(self) -> None`

```python
async def ready(self) -> None:
    return None
```

Hook called once after all apps have been loaded and their routers mounted. Typical use is to import a signals module to register `@receiver` decorators. The base implementation is a no-op; subclasses override it when needed. It may be either `async def` or `def` — the bootstrap detects this with `inspect.iscoroutinefunction`.

Example:

```python
class SharedConfig(AppConfig):
    name = "shared"
    verbose_name = "Shared"
    is_builtin = True

    async def ready(self) -> None:
        import apps.shared.signals  # registers receivers on import
```

---

### `ModuleConfig`

```python
class ModuleConfig(AppConfig):
    _abstract: bool = True

    requires_restart: bool = False
    is_system: bool = False
    has_views: bool = True
    has_api: bool = True
    media_path: str = ""
    s3_key: str | None = None
    sha256: str | None = None
```

Inherits from `AppConfig` and adds attributes specific to dynamic modules. The `_abstract=True` flag is deliberate: `ModuleConfig` must not have a `name` (modules are abstract until the user subclasses them with a concrete `name`).

Additional attributes:

| Attribute | Type | Default | Meaning |
|---|---|---|---|
| `requires_restart` | `bool` | `False` | If `True`, changes to the module cannot be applied hot and require a process restart. |
| `is_system` | `bool` | `False` | If `True`, the module cannot be uninstalled from the UI (it is a system module). |
| `has_views` | `bool` | `True` | The module has HTML routes (mounts `routes.py`). |
| `has_api` | `bool` | `True` | The module has REST routes (mounts `api.py`). |
| `s3_key` | `str \| None` | `None` | Explicit S3 key. If empty, derived from `name + version`. |
| `sha256` | `str \| None` | `None` | Explicit SHA256 hash of the package for integrity verification. |

#### Lifecycle hooks

```python
async def install(self, ctx: Any) -> None: ...
async def uninstall(self, ctx: Any) -> None: ...
async def activate(self, ctx: Any) -> None: ...
async def deactivate(self, ctx: Any) -> None: ...
```

All are no-ops in the base class. The engine calls them at the corresponding lifecycle points. `ctx` is the operation context (DB session, settings, etc.).

- `install` — seed initial data on first installation.
- `uninstall` — idempotent cleanup on uninstall.
- `activate` — setup that must run each time the module is activated (e.g., registering scheduled tasks).
- `deactivate` — state cleanup when the module is deactivated but not uninstalled.

A complete `ModuleConfig` in a real project:

```python
# modules/notes/module.py
from hotframe.apps import ModuleConfig

class NotesModule(ModuleConfig):
    name = "notes"
    verbose_name = "Notes"
    version = "1.0.0"
    has_views = True
    has_api = True
    is_system = False
    requires_restart = False
    menu = {"label": "Notes", "icon": "document-text-outline", "order": 30}

    async def install(self, ctx):
        await ctx.db.execute("INSERT INTO ...")  # seed data

    async def ready(self):
        import modules.notes.signals
```

---

## `registry.py` — In-memory registries

### `RegisteredModule`

```python
@dataclass(slots=True)
class RegisteredModule:
    module_id: str
    manifest: ModuleManifest
    router: APIRouter | None = None
    api_router: APIRouter | None = None
    middleware: Any | None = None
    path: Path = field(default_factory=Path)
    loaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
```

A snapshot of a dynamic module's state once it is loaded into the process. Uses `slots=True` to reduce memory overhead when many modules are loaded. `loaded_at` is used for diagnostics and metrics.

---

### `ModuleRegistry` (legacy contract)

```python
class ModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, RegisteredModule] = {}
        self._version: int = 0
```

Registry of dynamic modules loaded through the legacy pipeline (`ModuleManifest`). Not persistent: rebuilt on every cold start. Thread-safety: accessed from a single asyncio event loop, so a plain dict is sufficient.

#### Mutation methods

##### `register(...) -> RegisteredModule`

```python
def register(
    self,
    module_id: str,
    manifest: ModuleManifest,
    router: APIRouter | None,
    api_router: APIRouter | None,
    middleware: Any | None,
    path: Path,
) -> RegisteredModule:
```

Creates a `RegisteredModule`, stores it in `_modules[module_id]`, and increments `_version`. Logs the registration with name and version.

##### `unregister(module_id: str) -> None`

Removes the module from the dict and increments `_version`. Silent if the module was not registered.

#### Query methods

| Method | Description |
|---|---|
| `get(module_id)` | Returns `RegisteredModule` or `None`. |
| `get_all()` | Defensive copy of the dict. |
| `is_loaded(module_id)` | Boolean. |
| `get_loaded_module_ids()` | List of currently loaded IDs. |

#### Derived data

##### `get_menu_items() -> list[dict]`

```python
def get_menu_items(self) -> list[dict]:
    items = [
        {"module_id": ..., "label": ..., "icon": ..., "order": ...}
        for entry in self._modules.values()
        if entry.manifest.MENU is not None
    ]
    items.sort(key=lambda m: (m["order"], m["label"]))
    return items
```

Only includes modules that declare a `MENU`. Sorted first by `order` (ascending) then alphabetically by `label` for deterministic tie-breaking.

##### `get_navigation(module_id) -> list[dict]`

Returns the module's `NavigationItem` entries as a list of dicts. Empty if the module does not exist.

##### `get_module_middleware() -> list[Any]`

Returns all middleware from all loaded modules. Alias: `get_all_middleware` (compatibility with `module_middleware.py`).

##### `get_permissions(module_id) -> list[str]`

Permissions declared by a specific module.

##### `get_all_permissions() -> list[str]`

All permissions from all modules, as a flat list.

#### Registry versioning

```python
@property
def version(self) -> int:
    return self._version
```

Monotonically increasing counter. Incremented on every `register` / `unregister` call. Consumers (template loaders, OpenAPI cache, menu cache) store the last `version` they saw and compare against it to decide whether to rebuild.

---

### `AppRegistry` (new contract, Phase 3+)

```python
class AppRegistry:
    def __init__(self) -> None:
        self._apps: dict[str, AppConfig] = {}
        self._lock: asyncio.Lock | None = None
```

The new registry, designed to coexist with `ModuleRegistry` during migration and eventually replace it. Unlike `ModuleRegistry`, it stores `AppConfig` instances (not manifests and routers separately), which allows direct access to all config attributes.

**Improved thread-safety:** uses a lazy `asyncio.Lock` (created only when needed). The lock is lazy because most startups involve only a single registration event, and creating the lock unnecessarily has a cost.

#### `async register(config: AppConfig) -> None`

```python
async def register(self, config: AppConfig) -> None:
    async with self._get_lock():
        if config.name in self._apps:
            raise ValueError(f"App {config.name!r} already registered")
        self._apps[config.name] = config
```

Raises `ValueError` if the name already exists. Protected by a lock to be safe under async concurrency.

#### `async unregister(name: str) -> AppConfig | None`

Removes and returns the config. Returns `None` if it was not registered.

#### `get(name: str) -> AppConfig | None`

Synchronous read (the hot path). O(1).

#### `all() -> list[AppConfig]`

Snapshot of all registered configs.

#### `by_kind(*, builtin: bool | None = None) -> list[AppConfig]`

```python
def by_kind(self, *, builtin: bool | None = None) -> list[AppConfig]:
    items = self._apps.values()
    if builtin is None:
        return list(items)
    return [c for c in items if c.is_builtin is builtin]
```

Filters by kind:
- `builtin=True` → core project apps (live in `apps/`, have `is_builtin=True`).
- `builtin=False` → dynamic modules (installed from the marketplace, `is_builtin=False`).
- `builtin=None` → all.

Supports `in` and `len()` directly (`__contains__`, `__len__`).

---

## `service_facade.py` — Permissioned services

### The `@action` decorator

```python
@dataclass(frozen=True, slots=True)
class ActionMeta:
    permission: str
    mutates: bool = False
    description: str = ""

def action(*, permission: str, mutates: bool = False, description: str = "") -> Any:
    def decorator(fn: Any) -> Any:
        fn._action_meta = ActionMeta(
            permission=permission, mutates=mutates, description=description
        )
        return fn
    return decorator
```

Decorates methods on `ModuleService`, marking them as externally invocable actions. The `_action_meta` attribute is read by `register_services()` to populate the `SERVICE_REGISTRY`. `mutates=True` signals that the action modifies data (useful for audit logs and for prompting the UI to show a confirmation warning).

---

### `ModuleService`

```python
class ModuleService:
    module_id: str = ""

    def __init__(self, db: ISession, hub_id: UUID) -> None:
        self.db = db
        self.hub_id = hub_id
```

Base class for module services. Receives the database session and the `hub_id` (tenant identifier in multi-tenant architectures) in the constructor.

#### Data-access methods

##### `q(model: type) -> IQueryBuilder`

```python
def q(self, model: type) -> IQueryBuilder:
    return HubQuery(model, self.db, self.hub_id)
```

Returns a `HubQuery` filtered by `hub_id`. Every query made through `self.q()` is automatically scoped to the current tenant.

##### `repo(model, *, search_fields=None, default_order="created_at") -> IRepository`

```python
def repo(
    self,
    model: type,
    *,
    search_fields: list[str] | None = None,
    default_order: str = "created_at",
) -> IRepository[Any]:
    return BaseRepository(model, self.db, self.hub_id, ...)
```

Returns a hub-scoped `BaseRepository`. The return type is `IRepository[Any]` (the protocol), not `BaseRepository`, so the service is not coupled to the concrete implementation.

#### Response helpers

##### `success(**fields) -> dict`

```python
@staticmethod
def success(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}
```

Builds a consistently shaped success response. Always includes `"ok": True`. Usage:

```python
return self.success(id=str(todo.id), created=True)
# → {"ok": True, "id": "abc", "created": True}
```

##### `error(message, *, code="", **fields) -> dict`

```python
@staticmethod
def error(message: str, *, code: str = "", **fields: Any) -> dict[str, Any]:
    body = {"ok": False, "error": message}
    if code:
        body["code"] = code
    body.update(fields)
    return body
```

Builds an error response. `code` is a machine-readable identifier so clients can branch without parsing the human-readable message.

#### Parsing helpers

| Method | What it does |
|---|---|
| `parse_uuid(value)` | `str \| UUID \| None` → `UUID \| None`. Returns `None` for empty values, raises `ValueError` for malformed strings. |
| `parse_date(value, *, fmt="%Y-%m-%d")` | ISO string → `date`. Empty → `None`. |
| `parse_decimal(value)` | String → `Decimal`. Empty → `None`. |

#### Lookup helpers

##### `async get_or_none(model, id_value) -> Any`

Looks up by UUID primary key. Returns the row or `None` without touching the DB for empty values.

##### `async get_or_error(model, id_value, *, not_found_message, code) -> tuple`

```python
async def get_or_error(
    self,
    model: type,
    id_value: str | UUID | None,
    *,
    not_found_message: str = "Not found",
    code: str = "not_found",
) -> tuple[Any, dict[str, Any] | None]:
```

Idiomatic pattern for handlers:

```python
todo, err = await self.get_or_error(Todo, todo_id)
if err:
    return err
# todo is the object, guaranteed non-None
```

Returns `(row, None)` on success or `(None, error_dict)` on failure.

#### `atomic()`

```python
def atomic(self) -> Any:
    from hotframe.orm.transactions import atomic as _atomic
    return _atomic(self.db)
```

Shortcut for explicit transactions:

```python
async with self.atomic():
    await self.repo(Invoice).create(...)
    await self.repo(Line).create(...)
```

#### `serialize(obj) / serialize_list(items)`

Delegates to the `serialize` and `serialize_list` functions from `hotframe.repository.base`. Converts ORM objects to flat, JSON-serializable dicts.

---

### The global `SERVICE_REGISTRY`

```python
SERVICE_REGISTRY: dict[str, dict[str, ServiceEntry]] = {}
```

Structure: `{module_id: {ClassName: ServiceEntry}}`.

```python
@dataclass
class ServiceEntry:
    cls: type[ModuleService]
    description: str
    actions: dict[str, ActionEntry] = field(default_factory=dict)

@dataclass
class ActionEntry:
    method_name: str
    permission: str
    mutates: bool
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
```

---

### `register_services(module_id: str) -> int`

```python
def register_services(module_id: str) -> int:
```

Imports `<module_id>.services`, iterates its attributes looking for subclasses of `ModuleService`, and for each one collects all methods with `_action_meta`. Populates `SERVICE_REGISTRY` and returns the number of services registered.

The full permission name is built as `f"{module_id}.{meta.permission}"`, so `"view"` in module `"notes"` becomes `"notes.view"`.

If `<module_id>.services` does not exist, returns `0` silently (the module simply has no services).

Called by the engine during module activation.

---

### `unregister_module_services(module_id: str) -> int`

Removes the module's entries from `SERVICE_REGISTRY`. Called during deactivation.

---

### `generate_module_context(module_id: str) -> str`

```python
def generate_module_context(module_id: str) -> str:
```

Serializes a module's `SERVICE_REGISTRY` entries into a Markdown-like string suitable for injection as context into an LLM. Output format:

```
### TodoService
Manages the user's TODOs

- **list_todos**() → Lists all TODOs | READ
- **create_todo**(text: string, done?: boolean = False) → Creates a TODO | WRITE
```

This method is the bridge between the service registry and hotframe's integrated AI assistant.

---

### `generate_all_contexts() -> dict[str, str]`

Applies `generate_module_context` to all registered modules and returns a dict `{module_id: context_string}`.

---

## How this fits into the rest of the framework

**`bootstrap.py`** calls `_auto_discover_apps(app)`, which searches for `app.py` in each subdirectory of `apps/`. When it finds an `AppConfig` subclass, it instantiates the config and calls `ready()`. Routes are mounted directly (without going through `AppRegistry` on that code path — full `AppRegistry` integration is part of the ongoing migration to the new contract).

**`discovery/scanner.py`** uses `find_entry_config()` to extract the `AppConfig` or `ModuleConfig` class from a `DiscoveryResult`. The scanner does not register anything; it only discovers.

**`engine/module_runtime.py`** uses `ModuleRegistry` as the source of truth for active modules. When activating a module, it calls `registry.register(...)` with the manifest and routers; when deactivating, it calls `registry.unregister(...)`. It also calls `register_services(module_id)` during activation and `unregister_module_services(module_id)` during deactivation.

**The middleware manager** calls `registry.get_module_middleware()` to build the dynamic middleware stack.

**The template loader** compares `registry.version` against the last value it saw to invalidate the template cache when a module is mounted or unmounted.

---

## Gotchas and design decisions

**Two registries coexist during the migration.** `ModuleRegistry` (legacy, `ModuleManifest`-based) and `AppRegistry` (new, `AppConfig`-based) live side by side in the package. Comments in the code label them "Phase 3+" and "Phase 4+ will unify them". If you see code using one or the other, it reflects the stage of the framework lifecycle at which it was written.

**`AppConfig.name` is required at class definition time.** `__init_subclass__` raises `ValueError` if `name` is empty. This means the error fires at import time, not when the engine tries to register the class. The failure is immediate and the message is clear.

**`ModuleConfig._abstract=True` is reset in subclasses.** This is the subtlest mechanism in the package. `__init_subclass__` resets `_abstract=False` on each subclass unless the subclass explicitly declares `_abstract=True`. This prevents `ModuleConfig` from passing its flag down to concrete subclasses. Without this mechanism, `class NotesModule(ModuleConfig): name = "notes"` would be marked abstract and `__init_subclass__` would not validate `name`.

**`SERVICE_REGISTRY` is a mutable module-level global.** This is a pragmatic decision: services are registered when modules activate and unregistered when they deactivate, which requires a global store. In a multi-process environment, this store is not shared across workers.

**`ModuleService` always receives `hub_id`.** Even in applications that are not multi-tenant, the parameter exists. In that case, `hub_id` can be a fixed UUID or simply ignored. The reason is that the framework is designed to be multi-tenant from the ground up, and changing the `__init__` signature later would be a breaking change.

**There is no `INSTALLED_APPS`.** Apps are discovered automatically by scanning `apps/`. If you need to control what gets mounted, use `EXTRA_ROUTERS` in settings to add external routers, or simply do not place the directory under `apps/`. There is no blocklist.
