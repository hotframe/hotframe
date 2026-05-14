# 8. Migrations (`migrations/`)

> Alembic orchestration for a project where each app and each dynamic module carries its own
> migrations — without any namespace stomping on another's history. The runner solves the
> cross-foreign-key problem and executes module migrations safely inside an async process.

---

## What this folder is for

In hotframe, every unit of code (static apps and dynamic modules) has its own `migrations/`
directory with its own `versions/` subdirectory and its own Alembic version table. This enables:

- **Installing or uninstalling a module without touching core migrations** or those of other apps.
- **Running `hf migrate` to apply all pending migrations**, in order, without collisions.
- **`hf makemigrations <app>` to autogenerate** the correct revision for a single app, without
  Alembic getting confused by tables belonging to other modules.

This folder implements that orchestration in three pieces:

| Piece | Responsibility |
|---|---|
| `runner.py` | Run migrations for a single module (upgrade / downgrade) |
| `multi_namespace.py` | Coordinate migrations across all namespaces (core + apps) |
| `env_helpers.py` | Helpers for Alembic `env.py` files: import models that resolve cross-namespace FKs |

---

## File map

| File | Responsibility |
|---|---|
| [`__init__.py`](../src/hotframe/migrations/__init__.py) | Docstring + conceptual re-exports for the package |
| [`env_helpers.py`](../src/hotframe/migrations/env_helpers.py) | `import_all_app_models`, `import_module_dependencies` — prevent `NoReferencedTableError` during autogenerate |
| [`multi_namespace.py`](../src/hotframe/migrations/multi_namespace.py) | `MultiNamespaceRunner`, `MigrationNamespace`, `MigrationReport` — coordinates core + all apps |
| [`runner.py`](../src/hotframe/migrations/runner.py) | `ModuleMigrationRunner` — upgrade/downgrade for a single module, async-safe |

---

## What "multi-namespace" means

Standard Alembic assumes all migrations share **one** `alembic_version` table. In hotframe, each
namespace has its own:

| Namespace | Version table |
|---|---|
| core | `alembic_version` |
| `apps/shared/` | `alembic_shared_version` |
| `apps/auth/` | `alembic_auth_version` |
| `modules/notes/` | `alembic_notes` |
| `modules/shop/` | `alembic_shop` |

The table name follows this pattern:

- Core: `"alembic_version"` (standard convention, no suffix).
- Apps: `f"alembic_{app_name}_version"`.
- Modules: `f"alembic_{module_id}"`.

This means the database holds N version tables simultaneously. Each one has exactly one row with
the current `version_num` for its namespace. `alembic upgrade head` only looks at its own
namespace's table and never affects another's.

---

## `runner.py` — `ModuleMigrationRunner`

[`migrations/runner.py`](../src/hotframe/migrations/runner.py)

Manages migrations for **a single module**. Used by `ModuleRuntime` in the module lifecycle:
`upgrade` when activating, `downgrade` when uninstalling.

### `ModuleMigrationRunner`

```python
class ModuleMigrationRunner:
```

Has no instance state (no parameters in `__init__`). All parameters are passed per call. It is a
namespace of async and static methods.

### `upgrade(module_id, module_path, db_url)`

```python
async def upgrade(
    self,
    module_id: str,
    module_path: Path,
    db_url: str,
) -> None:
```

Runs `alembic upgrade head` for the module. The `db_url` argument must be the **synchronous** URL
(without `+asyncpg` or `+aiosqlite`) because Alembic uses synchronous SQLAlchemy internally.

Full algorithm:

1. Checks that `module_path / "migrations"` exists. If not, returns without error (modules without
   models do not need migrations).
2. Builds `version_table = f"alembic_{module_id}"`.
3. Calls `_build_config(module_id, module_path, db_url, version_table)`.
4. Appends `module_path.parent` to `sys.path` if not already present — so the module's `env.py`
   can import its models as `{module_id}.models`.
5. Defines `_run_upgrade()`, which creates a `create_engine(db_url, poolclass=NullPool)`, injects
   the engine into `config.attributes["connection"]`, and calls `command.upgrade(config, "head")`.
6. Calls `await asyncio.to_thread(_run_upgrade)` — Alembic's APIs are synchronous and blocking;
   `asyncio.to_thread` executes them in the thread pool without blocking the event loop.

Using `NullPool` in the migration engine ensures connections are opened and closed per operation —
the migration engine does not compete with the application's async connection pool.

### `downgrade(module_id, module_path, db_url)`

```python
async def downgrade(
    self,
    module_id: str,
    module_path: Path,
    db_url: str,
) -> None:
```

Runs `alembic downgrade base` — reverting **all** migrations for the module. Used during
`hf modules uninstall <name>` to clean up the schema before removing the code.

Simpler than `upgrade`: builds the config and calls
`await asyncio.to_thread(command.downgrade, config, "base")`. No `sys.path` manipulation is
needed (it was already set during the earlier activation).

### `has_migrations(module_path)`

```python
def has_migrations(self, module_path: Path) -> bool:
```

Checks whether the module has a `migrations/versions/` directory containing at least one `.py`
file. The runtime uses this to decide whether to call `upgrade`, avoiding the overhead of
building an Alembic config unnecessarily.

### `_build_config(module_id, module_path, db_url, version_table)` (static)

```python
@staticmethod
def _build_config(
    module_id: str,
    module_path: Path,
    db_url: str,
    version_table: str,
) -> Config:
```

Builds the `alembic.Config` for the module:

1. If `migrations/alembic.ini` exists, uses it as a base.
2. Otherwise, creates an empty `Config()`.
3. Sets/overrides:
   - `script_location` → `module_path / "migrations"`
   - `sqlalchemy.url` → `db_url`
   - `version_table` → as a main_option AND as `config.attributes["version_table"]`
4. Also stores in `config.attributes`:
   - `module_id`
   - `module_path` (as a string)

`config.attributes` is the communication channel between the runner and the module's `env.py`. The
`env.py` reads these values to know which version table to use and what the `module_path` is.

### `get_sync_db_url(async_url)` (static)

```python
@staticmethod
def get_sync_db_url(async_url: str) -> str:
```

Converts an async URL to its synchronous equivalent:

- `postgresql+asyncpg://...` → `postgresql://...`
- `sqlite+aiosqlite://...` → `sqlite://...`

`ModuleRuntime` calls this helper before invoking `upgrade` or `downgrade`, because
`settings.DATABASE_URL` always carries the async driver prefix.

---

## `multi_namespace.py` — namespace coordination

[`migrations/multi_namespace.py`](../src/hotframe/migrations/multi_namespace.py)

Implements orchestration of migrations across all apps (core + static apps). Dynamic modules use
`ModuleMigrationRunner` directly from the runtime; this runner is used by the `hf migrate` CLI
command.

### `MigrationNamespace`

```python
@dataclass
class MigrationNamespace:
    name: str           # "core", "accounts", "shared"
    script_location: Path
    version_table: str

    @classmethod
    def core(cls, root: Path) -> MigrationNamespace: ...

    @classmethod
    def for_app(cls, apps_root: Path, app_name: str) -> MigrationNamespace: ...
```

A data object describing a namespace. The two class constructors generate the correct conventions:

- `MigrationNamespace.core(root)` → name=`"core"`, script=`root/migrations`,
  version_table=`"alembic_version"`.
- `MigrationNamespace.for_app(apps_root, "auth")` → name=`"auth"`,
  script=`apps_root/auth/migrations`, version_table=`"alembic_auth_version"`.

### `MigrationReport`

```python
@dataclass
class MigrationReport:
    namespace: str
    applied: bool = False
    skipped: bool = False
    reason: str | None = None
    error: str | None = None
```

The result of applying migrations for a namespace. `applied=True` means Alembic completed without
an exception. `error` contains the exception message if it failed. The `skipped` field is reserved
for when the namespace has no pending migrations (currently unused — Alembic handles this
internally).

### `MultiNamespaceRunner`

```python
class MultiNamespaceRunner:
    def __init__(self, db_url: str, project_root: Path) -> None:
        self.db_url = db_url
        self.project_root = project_root
```

#### `discover_namespaces()`

```python
def discover_namespaces(self) -> list[MigrationNamespace]:
```

Discovers existing namespaces on the filesystem:

1. Always adds the `core` namespace (`{project_root}/migrations/`).
2. Iterates over `{project_root}/apps/*/` in alphabetical order.
3. For each subdirectory, checks that both `migrations/env.py` and `migrations/versions/` exist.
   Only namespaces where both are present are included (apps without migrations, or newly created
   apps without a `versions/` directory, are skipped).

Alphabetical ordering guarantees reproducibility: two calls to `discover_namespaces` always return
the same list.

#### `build_alembic_config(ns)`

```python
def build_alembic_config(self, ns: MigrationNamespace) -> AlembicConfig:
```

Builds an `AlembicConfig` for the namespace:

```python
cfg = AlembicConfig()
cfg.set_main_option("script_location", str(ns.script_location))
cfg.set_main_option("sqlalchemy.url", self.db_url)
cfg.attributes["version_table"] = ns.version_table
cfg.attributes["namespace_name"] = ns.name
```

`cfg.attributes` is the channel to `env.py` — the namespace's env.py reads
`context.config.attributes["version_table"]` to configure Alembic with the correct table.

#### `upgrade(namespace=None, revision="head")`

```python
def upgrade(
    self, namespace: str | None = None, revision: str = "head"
) -> list[MigrationReport]:
```

If `namespace` is `None`, upgrades all namespaces. If specified, filters to only that namespace.
For each discovered namespace:

1. Builds the config with `build_alembic_config`.
2. Calls `alembic_command.upgrade(cfg, revision)`.
3. Appends `MigrationReport(namespace=ns.name, applied=True)` on success, or
   `MigrationReport(namespace=ns.name, error=str(e))` on failure.

**Important**: the loop continues even if a namespace fails. You receive one report per namespace
and can inspect which ones failed. The `hf migrate` CLI prints the reports and exits with an error
code if any report has `error` set.

#### `current(namespace=None)`

```python
def current(self, namespace: str | None = None) -> dict[str, str | None]:
```

Returns the current `version_num` for each namespace by querying the database tables directly.
Rather than using Alembic's own mechanism, it issues a
`SELECT version_num FROM {version_table} LIMIT 1` query to avoid initializing the full Alembic
environment just to check the current revision.

Creates a temporary synchronous engine from `self.db_url` by stripping the async driver prefixes
(`+asyncpg`, `+aiosqlite`). If the table does not exist (namespace never migrated), returns `None`
for that namespace.

---

## `env_helpers.py` — resolving cross-namespace FKs during autogenerate

[`migrations/env_helpers.py`](../src/hotframe/migrations/env_helpers.py)

This module is not executed at runtime — only during `hf makemigrations`. It solves the problem
that Alembic's autogenerate needs **all referenced tables** present in `Base.metadata` in order to
inspect cross-namespace foreign keys.

### The problem

Each module's `env.py` imports only its own models:

```python
# modules/shop/migrations/env.py
from shop.models import Order, OrderLine
```

If `Order` has a FK to `users` (a table from `apps/auth`), and `users` is not in `Base.metadata`
when autogenerate runs, Alembic raises `NoReferencedTableError`.

### `import_all_app_models(project_root=None)`

```python
def import_all_app_models(project_root: Path | None = None) -> list[str]:
```

Iterates over `{project_root}/apps/*/models.py` in alphabetical order and imports each one. The
desired side effect is that every `Model = Table(...)` defined with SQLAlchemy registers itself in
`Base.metadata`.

Precautions:

- Appends `project_root` to `sys.path` if not already present (so that `apps.auth.models` is
  importable).
- Is idempotent: a second call with the same `project_root` is a no-op (Python caches imported
  modules).
- Errors in any `models.py` are logged as warnings and the loop continues — a broken app does not
  block migrations for others. If the error was needed to resolve a FK, Alembic will fail later
  with a clear message.

**Typical usage in a module's `env.py`**:

```python
# modules/shop/migrations/env.py
from hotframe.migrations.env_helpers import import_all_app_models, import_module_dependencies

import_all_app_models()                    # registers tables from apps/
import_module_dependencies("shop")         # registers tables from dependent modules

from shop import models as _  # registers this module's own tables
```

### `import_module_dependencies(module_id, project_root=None)`

```python
def import_module_dependencies(
    module_id: str,
    project_root: Path | None = None,
) -> list[str]:
```

Imports the `models.py` files of all **transitive** dependencies of the module, without importing
the module itself (its `env.py` already does that).

Walk algorithm:

1. Reads the specified module's `module.py` to obtain its `DEPENDENCIES`.
2. For each dependency, recursively reads its `module.py` and its own deps.
3. Once there are no more unvisited nodes, imports `{dep_id}.models` for each discovered
   dependency.

The graph is walked with a `visited` set to avoid cycles. Modules not in the dependency tree are
**not** imported — this is deliberate to avoid side effects from unrelated modules (decorators,
signal registrations, etc.).

### `_read_dependencies(manifest)` (private)

```python
def _read_dependencies(manifest: Path) -> list[str]:
```

Parses `DEPENDENCIES` from `module.py` using `ast.parse` **without importing the file**. It looks
for an assignment such as:

```python
DEPENDENCIES = ["auth", "catalog"]
# or with annotation:
DEPENDENCIES: list[str] = ["auth", "catalog"]
```

And extracts the string elements from the list. If the file has a syntax error or the field does
not exist, returns `[]`. Choosing AST over `importlib` avoids executing the module's code (which
might have imports that fail in a migration context).

---

## Structure of a module with migrations

```
modules/notes/
└── migrations/
    ├── __init__.py
    ├── alembic.ini        ← (optional) overrides defaults
    ├── env.py             ← Alembic configuration for this module
    └── versions/
        ├── 001_initial.py
        └── 002_add_tags.py
```

The minimal `env.py` for a module:

```python
# modules/notes/migrations/env.py
from alembic import context
from hotframe.migrations.env_helpers import import_all_app_models

import_all_app_models()  # resolves FKs pointing to apps/

from notes import models as _  # registers this module's tables

config = context.config
version_table = (
    config.attributes.get("version_table")
    or config.get_main_option("version_table")
    or f"alembic_notes"
)

# Offline / online configuration depending on Alembic mode
def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        version_table=version_table,
        target_metadata=_.Base.metadata,
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            version_table=version_table,
            target_metadata=_.Base.metadata,
        )
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

The key trick: `config.attributes.get("connection")` — `ModuleMigrationRunner` injects the engine
into `config.attributes["connection"]` before calling `command.upgrade`. The `env.py` only has to
read it.

---

## Full module migration lifecycle

```
hf modules install notes
  └── ModuleRuntime.install()
        └── [copies files, registers in DB]

hf modules activate notes
  └── ModuleRuntime.activate()
        ├── imports the module's Python package
        ├── mounts routes at /m/notes/
        └── ModuleMigrationRunner.upgrade(
                module_id="notes",
                module_path=Path(".../modules/notes"),
                db_url="postgresql://..."   # synchronous
            )
            └── asyncio.to_thread(_run_upgrade)
                  └── alembic upgrade head (in thread pool)
                      → creates notes table, applies pending revisions

hf modules uninstall notes
  └── ModuleRuntime.uninstall()
        └── ModuleMigrationRunner.downgrade(
                module_id="notes",
                module_path=...,
                db_url=...
            )
            └── alembic downgrade base
                → drops all tables belonging to the module
```

---

## How this fits with the rest of the framework

### CLI `hf migrate`

The `hf migrate` command uses `MultiNamespaceRunner` to run migrations for core and all apps.
Module migrations are not run by this command — each module runs them automatically when activated
via `ModuleMigrationRunner`.

```bash
hf migrate              # runs core + apps, upgrade head
hf migrate --namespace auth   # only the auth namespace
```

### CLI `hf makemigrations <app>`

Configures Alembic for the app's namespace, calls `import_all_app_models()` and
`import_module_dependencies()` (if it is a module), and launches
`alembic revision --autogenerate`.

### `ModuleRuntime` (engine/module_runtime.py)

Imports `ModuleMigrationRunner` and calls it in lifecycle hooks:

- `activate` → `runner.upgrade(module_id, module_path, sync_url)`
- `uninstall` → `runner.downgrade(module_id, module_path, sync_url)`

The synchronous URL is obtained via `ModuleMigrationRunner.get_sync_db_url(settings.DATABASE_URL)`.

### SQLAlchemy engine (async)

Migrations use synchronous SQLAlchemy (`create_engine`, not `create_async_engine`). This is
correct — Alembic does not support async. The migration engine uses `NullPool` so it does not
interfere with the application's async connection pool.

---

## Gotchas and design decisions

### 1. `asyncio.to_thread` for Alembic

Alembic is synchronous and blocking. Calling it directly inside an async handler would block the
event loop. `asyncio.to_thread` executes it in the system thread pool, allowing the loop to
continue processing other requests while migrations run. The trade-off is that the migration
blocks one thread-pool thread for its entire duration.

### 2. A module may or may not have its own `alembic.ini`

`_build_config` detects whether `migrations/alembic.ini` exists. If it does, it is used as a
base (which allows overriding logging configuration, etc.). If it does not exist, an empty
`Config()` is created and everything is configured programmatically. The values the runner needs
(url, script_location, version_table) are always overwritten afterwards, so an existing
`alembic.ini` cannot interfere with them.

### 3. `DEPENDENCIES` is parsed with AST, never imported

Importing `module.py` to read its dependencies would have side effects (executing decorators,
registering signals, loading models). Using `ast.parse` is a trade-off: it only works with simple
literals in `DEPENDENCIES = [...]`, but that is sufficient — the framework convention is that
`DEPENDENCIES` is always a list of string literals.

### 4. Each module has its own version table, not a column in a shared table

The alternative (a single `alembic_version` table with an additional `namespace` column) would
make queries simpler but would require patching Alembic. The separate-tables solution is pure
standard Alembic — each namespace is a completely independent Alembic instance.

### 5. `MultiNamespaceRunner` is synchronous, `ModuleMigrationRunner` is async

`MultiNamespaceRunner` is used from the CLI (`hf migrate`), which runs in a synchronous process
outside the event loop. That is why its methods are `def`, not `async def`.
`ModuleMigrationRunner` must be async because `ModuleRuntime` calls it from inside the
application's event loop.

### 6. `downgrade base` wipes the entire module history

On uninstall, `ModuleMigrationRunner.downgrade` runs `alembic downgrade base`, which applies all
downgrade scripts in reverse order. If the module has not implemented its downgrade scripts
correctly, the uninstall may fail or leave the database in an inconsistent state. The framework
convention is that modules must always implement downgrade steps.
