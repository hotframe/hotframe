# 18. The CLI (`management/`)

> `management/` is hotframe's command-line tooling layer. It exposes the `hf` executable (and its alias `hotframe`) that a developer uses throughout the entire project lifecycle: from initial scaffolding to production migrations, through the interactive REPL and the full lifecycle of dynamic modules.

---

## What this folder is for

`management/` encapsulates all CLI logic. The goal is that a developer **never needs to write ad-hoc scripts**: the same `hf` binary handles creating projects, generating scaffolding, migrating the database, installing/activating/deactivating modules, and opening a debug console. The philosophy is the same as Django or Rails: **convention over configuration**, and **a single entry point** for managing the project.

The folder contains exactly two files:

---

## File map

| File | Responsibility |
|---|---|
| [`management/__init__.py`](../src/hotframe/management/__init__.py) | Empty module with a usage docstring. Marks the package. |
| [`management/cli.py`](../src/hotframe/management/cli.py) | The entire CLI (~1,640 lines). Defines every `hf` subcommand, the `hf modules` group, and the scaffolding and migration helper functions. |

---

## General CLI architecture

The CLI is built on **[Typer](https://typer.tiangolo.com/)**, which wraps Click. The root app is declared as:

```python
app = typer.Typer(
    name="hotframe",
    help="Hotframe — Modular Python web framework CLI.",
    no_args_is_help=True,
)
```

The `no_args_is_help=True` flag makes `hf` without arguments display the help text, just like `hf --help`. The `hf modules` subcommand group is created with a second `Typer` instance attached to the main one:

```python
modules_app = typer.Typer(help="Module lifecycle management.")
app.add_typer(modules_app, name="modules")
```

In hotframe's `pyproject.toml`, the entry point is registered as:

```toml
[project.scripts]
hf = "hotframe.management.cli:app"
hotframe = "hotframe.management.cli:app"
```

After `pip install hotframe`, both commands are available on the `PATH`.

---

## Helper function: `_load_project_settings()`

**Signature:** `_load_project_settings() -> HotframeSettings`

This is the first function that nearly every subcommand calls. Its purpose is to locate the project's `settings.py` (not hotframe's own) and inject it into the framework's global context.

```python
def _load_project_settings():
    import sys
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        import importlib
        mod = importlib.import_module("settings")
        project_settings = getattr(mod, "settings", None)
        if project_settings is not None:
            from hotframe.config.settings import set_settings
            set_settings(project_settings)
            return project_settings
    except ImportError:
        pass

    from hotframe.config.settings import get_settings
    return get_settings()
```

**What it does, step by step:**

1. Adds the current working directory to `sys.path` if it is not already there, so that `import settings` can find the project's `settings.py`.
2. Dynamically imports the `settings` module with `importlib.import_module`.
3. If the module exposes a `settings` attribute (the `HotframeSettings` object), registers it globally with `set_settings()` and returns it.
4. If there is no `settings.py` in the current directory (e.g., the developer is outside the project root), falls through the `except ImportError` and returns hotframe's default configuration.

**Important gotcha:** `_load_project_settings()` depends on the current working directory (`Path.cwd()`). Subcommands that touch the database must be run **from the project root**, or they will use hotframe's default configuration (with an in-memory SQLite database), which can be confusing.

---

## `hf startproject`

**Signature:** `startproject(name: str) -> None`

Creates the complete directory structure for a new hotframe project.

### Usage

```bash
hf startproject myapp         # creates the myapp/ directory
hf startproject .             # creates in the current directory (must be empty)
```

### Flags

None. The only positional argument is the project name.

### Special case: `name == "."`

If the name is `.`, hotframe creates the project in the current directory. Before doing so it verifies that the directory is "empty enough" — the following entries are permitted: `.venv`, `pyproject.toml`, `uv.lock`, `.git`, `.gitignore`, `__pycache__`, and `.python-version`. Any other file or folder causes the command to fail with a clear error:

```
Error: directory is not empty. Found: src, README.md
```

### Generated files

| Path | Contents |
|---|---|
| `main.py` | `create_app(settings)` — entrypoint for uvicorn |
| `asgi.py` | Re-exports `app` from `main` with a `# uvicorn asgi:app` comment |
| `settings.py` | `Settings(HotframeSettings)` class with all configuration groups commented out, ready to uncomment |
| `manage.py` | Delegates to `hotframe.management.cli:app` — useful for projects that prefer `python manage.py` |
| `.env` | `DATABASE_URL=sqlite+aiosqlite:///./app.db`, `SECRET_KEY`, `DEBUG=true` |
| `.gitignore` | Standard Python plus `.env`, `*.db` |
| `pyproject.toml` | Only if it does not already exist. Minimal dependencies: `hotframe`. Dev dependencies: `pytest`, `pytest-asyncio`, `ruff`. Configures `asyncio_mode = "auto"` for pytest |
| `apps/__init__.py` | Empty |
| `apps/shared/__init__.py` | Empty |
| `apps/shared/app.py` | `SharedConfig(AppConfig)` |
| `apps/shared/routes.py` | `GET /` route that serves `shared/index.html` or a basic fallback HTML response |
| `apps/shared/templates/shared/base.html` | Complete base template with CSP nonce support, Trusted Types, Iconify CDN, `live_assets()`, and a toast container with inline JS |
| `apps/shared/templates/shared/index.html` | Welcome page that extends `base.html` |
| `apps/shared/templates/errors/404.html` | Extends `base.html` |
| `apps/shared/templates/errors/500.html` | Extends `base.html` |
| `apps/shared/components/alert/template.html` | Example component: `{% component 'alert' type='warning' %}` |
| `apps/shared/components/badge/template.html` | Example component: `{{ render_component('badge', text='New') }}` |
| `modules/` | Empty folder for dynamic modules |
| `tests/__init__.py` | Empty |
| `tests/conftest.py` | `app` and `db` fixtures ready to use with `hotframe.testing` |

**Design decision:** hotframe generates `base.html` with the toast container and inline JS from the very start, because nearly every real application needs notifications, and having it ready is more useful than having to look it up in the documentation.

---

## `hf startapp`

**Signature:** `startapp(name: str) -> None`

Creates a new **static app** inside `apps/`.

### Usage

```bash
hf startapp accounts
hf startapp billing
```

### Generated files in `apps/<name>/`

| Path | Contents |
|---|---|
| `__init__.py` | Empty |
| `app.py` | `{Name}Config(AppConfig)` with `name`, `verbose_name`, and an empty `ready()` |
| `models.py` | Imports `Base`, comment prompting model definitions |
| `routes.py` | `router = APIRouter(prefix="/{name}", tags=["{name}"])` |
| `api.py` | `api_router = APIRouter(prefix="/api/v1/{name}", tags=["{name}"])` |
| `templates/{name}/pages/` | Empty folder |
| `templates/{name}/partials/` | Empty folder |
| `migrations/versions/` | Empty folder |
| `migrations/env.py` | Generated by `_generate_env_py(name)` |
| `migrations/script.py.mako` | Generated by `_generate_script_mako()` |
| `tests/__init__.py` | Empty |

**Difference from `startmodule`:** apps have no `module.py` (they are not dynamic modules), no `has_views`/`has_api` flags, and their routes are mounted at process startup rather than at runtime.

---

## `hf startmodule`

**Signature:**
```python
startmodule(
    name: str,
    api_only: bool = typer.Option(False, "--api-only", ...),
    system: bool  = typer.Option(False, "--system", ...),
) -> None
```

Creates a new **dynamic module** inside `modules/`.

### Usage

```bash
hf startmodule blog                  # views + API (default)
hf startmodule payments --api-only   # API only, no HTML views
hf startmodule audit --system        # system module (is_system=True)
```

### Flag logic

```python
has_views = not api_only and not system
has_api   = not system
```

A `--system` module is assumed to have no views or API of its own (framework infrastructure only, not user-uninstallable). An `--api-only` module has an API but no templates or `routes.py`.

### Generated files in `modules/<name>/`

| Path | Condition | Contents |
|---|---|---|
| `__init__.py` | always | Empty |
| `module.py` | always | `{Name}Module(ModuleConfig)` class with `name`, `verbose_name`, `version`, `is_system`, `has_views`, `has_api`, `requires_restart`, `dependencies`, `ready()`, `install()`, `uninstall()` |
| `models.py` | always | Imports `Base` |
| `routes.py` | if `has_views` | Router with `GET /m/{name}/` that renders `{name}/pages/index.html` |
| `templates/{name}/pages/index.html` | if `has_views` | Extends `shared/base.html`, displays the module name |
| `templates/{name}/partials/` | if `has_views` | Empty folder |
| `api.py` | if `has_api` | `api_router` with `GET /api/v1/{name}/` returning `{"module": name, "items": []}` |
| `migrations/versions/` | always | Empty folder |
| `migrations/env.py` | always | Generated by `_generate_env_py(name)` |
| `migrations/script.py.mako` | always | Generated by `_generate_script_mako()` |
| `tests/__init__.py` | always | Empty |

The command output indicates the mode that was created:

```
Created module 'modules/blog/' (views + API)
Created module 'modules/payments/' (API)
Created module 'modules/audit/' (system)
```

---

## `hf runserver`

**Signature:**
```python
runserver(
    host: str  = "0.0.0.0",
    port: int  = 8000,
    reload: bool = True,
) -> None
```

Starts uvicorn pointing at `main:app` with auto-reload enabled by default.

### Usage

```bash
hf runserver
hf runserver --host 127.0.0.1 --port 9000
hf runserver --no-reload
```

### Implementation

```python
import uvicorn

cwd = str(Path.cwd())
if cwd not in sys.path:
    sys.path.insert(0, cwd)

uvicorn.run("main:app", host=host, port=port, reload=reload)
```

Adds the cwd to `sys.path` before calling uvicorn so that `import main` works without installing the project as a package. Reload is based on uvicorn's own mechanism (inotify/FSEvents), **not** on the `ModuleWatcher` in `dev/autoreload.py` (which is separate and operates at the dynamic module level).

**Gotcha:** Do not use `hf runserver` in production. Use `uvicorn asgi:app --workers N` directly, or gunicorn with the uvicorn worker class.

---

## `hf migrate`

**Signature:**
```python
migrate(
    name: str = typer.Argument(None, help="App or module. Omit to migrate everything.")
) -> None
```

Runs pending Alembic migrations for all apps and modules, or for a specific one.

### Usage

```bash
hf migrate                 # all apps/ + modules/
hf migrate accounts        # apps/accounts/ only
hf migrate sales           # modules/sales/ only
```

### Internal flow

1. Loads settings with `_load_project_settings()`.
2. Instantiates `ModuleMigrationRunner` (from `hotframe.migrations.runner`).
3. Obtains a synchronous database URL with `runner.get_sync_db_url(settings.DATABASE_URL)` — converts `+asyncpg` or `+aiosqlite` to their sync equivalents.
4. If `name` is specified, searches in `apps/<name>` and then in `modules/<name>`.
5. If `name` is not specified:
   - Collects all apps with a `migrations/` folder inside `apps/`.
   - Collects all modules with a `migrations/` folder inside `modules/` and **topologically sorts** them with `_topo_sort_modules()`.
6. For each target, if `runner.has_migrations(path)` is true, calls `await runner.upgrade(mid, mpath, db_url)`.

### Topological sorting of modules

The function `_topo_sort_modules(module_targets)` solves the problem of foreign keys between modules. If the `commissions` module has a FK to a table in the `services` module, its migrations must run **after** those of `services`. Kahn's algorithm is used, and it aborts with a clear message if a cycle is detected.

`_extract_module_dependencies(module_path)` reads the module's `module.py` **without importing it** (using `ast.parse`) to avoid side effects. It extracts the `DEPENDENCIES` list (as a literal, supporting both `DEPENDENCIES = [...]` and the annotated form `DEPENDENCIES: list[str] = [...]`).

### Per-app/module version tables

Each app and module uses its own Alembic table: `alembic_<name>` (e.g., `alembic_accounts`, `alembic_sales`). This ensures that migrations from different modules are completely independent and do not collide in a shared `alembic_version` table.

---

## `hf makemigrations`

**Signature:**
```python
makemigrations(
    name: str = typer.Argument(..., help="App or module"),
    message: str = typer.Option("auto", "-m", "--message", help="Message"),
) -> None
```

Generates a new Alembic revision with auto-detection of model changes.

### Usage

```bash
hf makemigrations accounts
hf makemigrations accounts -m "add email field"
hf makemigrations sales -m "initial"
```

### Internal flow

1. Finds the directory at `apps/<name>` or `modules/<name>`.
2. Creates `migrations/`, `migrations/versions/`, `migrations/env.py`, and `migrations/script.py.mako` if they do not exist.
3. Builds an in-memory `alembic.config.Config` (no `alembic.ini` file):
   - `script_location` → the app/module's `migrations/` folder.
   - `sqlalchemy.url` → synchronous database URL (strip `+asyncpg`/`+aiosqlite`).
   - `version_table` → `alembic_<name>`.
4. Calls `command.revision(config, message=message, autogenerate=True)` inside `asyncio.to_thread` to avoid blocking the event loop.

### Helper function: `_generate_env_py(name: str) -> str`

Generates the complete text of the Alembic `env.py`. Key points:

- Adds `parents[3]` to `sys.path` so that `env.py` can import project models from any nesting depth.
- Imports `Base` from `hotframe.models.base`.
- Attempts to import the app or module's models with `importlib`, trying `apps.<name>.models` first and then `modules.<name>.models`.
- Supports **online mode with an injected connection** (for `ModuleMigrationRunner`): `config.attributes.get("connection")`. If a connection is injected, it is used directly instead of creating a new engine.
- Uses `render_as_batch=True` for SQLite compatibility (which does not support `ALTER TABLE` directly).

### Helper function: `_generate_script_mako() -> str`

Generates the standard Mako template for Alembic migration files. Contains the `upgrade()` and `downgrade()` blocks with Alembic variables (`${up_revision}`, `${down_revision}`, etc.).

---

## `hf shell`

**Signature:**
```python
shell(
    no_startup: bool = typer.Option(False, "--no-startup", ...),
    settings_path: str = typer.Option("", "--settings", ...),
    plain: bool = typer.Option(False, "--plain", ...),
) -> None
```

Opens an interactive REPL with the hotframe application fully initialized.

### Usage

```bash
hf shell                              # IPython if available, with DB and registries
hf shell --plain                      # code.interact() instead of IPython
hf shell --no-startup                 # no lifespan (no DB, no registries)
hf shell --settings myproject.settings  # explicit settings
```

### Variables pre-injected into the namespace

| Variable | Type | Description |
|---|---|---|
| `app` | `FastAPI` | The fully constructed FastAPI application |
| `settings` | `HotframeSettings` | The project settings instance |
| `db` | `AsyncSession` | An open database session |
| `events` | `AsyncEventBus` | The application event bus |
| `hooks` | `HookRegistry` | The hook registry |
| `slots` | `SlotRegistry` | The slot registry |
| `runtime` | `ModuleRuntime` | The dynamic module runtime |
| `SlotEntry` | class | For creating slot entries manually |

With `--no-startup`, only `app` and `settings` are injected.

### Internal flow

1. `_resolve_shell_settings(settings_path)` loads the settings object (from a dotted path or via auto-discovery).
2. `create_app(settings_obj)` builds the FastAPI app.
3. If `--no-startup` is not set, executes the full lifespan with `fastapi_app.router.lifespan_context(fastapi_app).__aenter__()`, which initializes the database engine, registries, `ModuleRuntime`, and so on.
4. Opens a database session with `get_session_factory()`.
5. Calls `_launch_repl(namespace, version, plain, loop)`.

### REPL with IPython

If IPython is installed and `--plain` is not active:

```python
from IPython.terminal.embed import InteractiveShellEmbed
shell_instance = InteractiveShellEmbed(banner1=banner, user_ns=namespace)
shell_instance.run_line_magic("autoawait", "asyncio")
shell_instance()
```

`%autoawait asyncio` allows using `await` directly at the IPython prompt:

```python
In [1]: users = await db.execute(select(User))
```

### REPL with `code.interact()`

If IPython is not available, an extra `run(coro)` function is injected:

```python
def run(coro):
    return loop.run_until_complete(coro)
```

Usage in the REPL:

```python
>>> users = run(db.execute(select(User)))
```

### Startup banner

```
Hotframe 1.0.0 shell (IPython)
Variables: app, settings, db, events, hooks, slots, runtime, SlotEntry
Tip: await works directly (autoawait asyncio).
```

### Guaranteed cleanup

The `finally` block closes the database session and executes the lifespan `__aexit__` even if the REPL is closed with Ctrl+D, an error, or a signal.

---

## `hf modules list`

**Signature:** `modules_list() -> None`

Prints a table of all modules found in `modules/`.

### Usage

```bash
hf modules list
```

### Output

```
Module               Status       Version    Views  API
------------------------------------------------------------
blog                 available    1.0.0      yes    yes
payments             available    2.1.0      no     yes
audit                available (system) 1.0.0  no   no
```

### Implementation

Iterates `modules/` looking for folders that contain a `module.py`. For each one, dynamically imports the module and finds the class whose `name` attribute matches the directory name. Extracts `version`, `has_views`, `has_api`, and `is_system`. If the import fails (syntax error, unmet dependencies), the module is displayed with empty fields rather than aborting.

**Gotcha:** `modules list` imports `module.py` (unlike `_extract_module_dependencies`, which uses AST), so it has import side effects. If a module has top-level code that fails, the status column simply appears empty.

**Limitation in v1.0:** `modules list` does not query the database, so it cannot show the actual state (installed/active/inactive). It only detects which modules exist on the filesystem and shows `"available"` for all of them.

---

## `hf modules install`

**Signature:** `modules_install(source: str) -> None`

Installs a module from a source (local module name, `.zip` file, URL, or marketplace).

### Usage

```bash
hf modules install shop
hf modules install /tmp/shop-1.2.0.zip
hf modules install https://marketplace.hotframe.dev/shop-1.2.0.zip
```

### Internal flow

```python
async def _install():
    settings = _load_project_settings()
    runtime = ModuleRuntime(
        app=None, settings=settings,
        event_bus=AsyncEventBus(), hooks=HookRegistry(), slots=SlotRegistry()
    )
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # create tables if they don't exist
    async with factory() as session:
        result = await runtime.install(session, hub_id=None, module_id=source, source=source)
        ...
```

Creates a "headless" `ModuleRuntime` (no `FastAPI` app, `app=None`) because there is no running server in the CLI context. Creates hotframe's metadata tables before attempting the installation. The result is an object with `result.success`, `result.module_id`, and `result.version`.

---

## `hf modules update`

**Signature:** `modules_update(source: str) -> None`

Updates a module to a new version. Delegates to `runtime.update(session, hub_id=None, module_id=source, new_version=None, source=source)`. The backup and rollback logic lives in `ModuleRuntime`, not in the CLI.

### Usage

```bash
hf modules update shop
hf modules update /tmp/shop-2.0.0.zip
```

---

## `hf modules activate`

**Signature:** `modules_activate(name: str) -> None`

Activates a module that was deactivated. Calls `runtime.activate(session, hub_id=None, module_id=name)`.

**Note:** In the CLI (outside a running server), activating means updating the state in the database. Routes are not mounted in any process until the server starts and runs its lifespan.

### Usage

```bash
hf modules activate shop
```

---

## `hf modules deactivate`

**Signature:** `modules_deactivate(name: str) -> None`

Deactivates a module without deleting its data. Calls `runtime.deactivate(session, hub_id=None, module_id=name)`.

### Usage

```bash
hf modules deactivate shop
```

---

## `hf modules uninstall`

**Signature:**
```python
modules_uninstall(
    name: str,
    keep_data: bool = typer.Option(False, "--keep-data", help="Keep tables"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None
```

Uninstalls a module. Prompts for interactive confirmation by default.

### Usage

```bash
hf modules uninstall shop
hf modules uninstall shop --keep-data
hf modules uninstall shop -y                 # no confirmation (CI/CD)
hf modules uninstall shop --keep-data -y
```

### Interactive confirmation

```
Uninstall module 'shop'? (including database tables) [y/N]:
```

With `--keep-data`, the message changes to `(keeping data)`. If the user answers "no", it prints `Cancelled.` and exits with code 0.

Delegates to `runtime.uninstall(session, hub_id=None, module_id=name)`.

---

## `hf version`

**Signature:** `version() -> None`

Prints the installed version of hotframe.

### Usage

```bash
hf version
# → hotframe 1.0.0
```

Imports `__version__` from the `hotframe` package.

---

## Code generation helper functions

### `_generate_env_py(name: str) -> str`

Generates the complete contents of the Alembic `env.py` for an app or module. Highlights:

- Adds `parents[3]` (project root) to `sys.path`.
- Imports `hotframe.models.base.Base`.
- Imports the app/module's models with `importlib`, with a silent fallback if the module cannot be imported.
- Supports offline mode (`context.is_offline_mode()`).
- Supports injected connections (`config.attributes.get("connection")`).
- Uses `render_as_batch=True` for SQLite compatibility.
- Uses `compare_type=True` to detect column type changes.

### `_generate_script_mako() -> str`

Generates the standard Alembic Mako template. Contains the header with `Revision ID`, `Revises`, and `Create Date`, and the `upgrade()` and `downgrade()` blocks.

### `_topo_sort_modules(module_targets: list[tuple[str, Path]]) -> list[tuple[str, Path]]`

Sorts modules using Kahn's algorithm to respect the dependencies declared in `module.py`. Ignores dependencies that point to modules not present in the list (optional, removed, or referencing apps). Detects cycles and aborts.

### `_extract_module_dependencies(module_path: Path) -> list[str]`

Reads `module.py` without importing it (using `ast.parse`) and extracts the `DEPENDENCIES` list. Returns `[]` if the file does not exist, has a syntax error, or does not define `DEPENDENCIES`.

---

## How this fits into the rest of the framework

| What the CLI does | Which component it interacts with |
|---|---|
| `migrate` / `makemigrations` | `hotframe.migrations.runner.ModuleMigrationRunner` |
| `modules install/activate/deactivate/uninstall/update` | `hotframe.engine.module_runtime.ModuleRuntime` |
| `shell` | `hotframe.bootstrap.create_app`, `hotframe.config.database.get_session_factory`, `hotframe.templating.slots.SlotEntry` |
| `runserver` | `uvicorn` directly, pointing at `main:app` |
| `startproject` / `startapp` / `startmodule` | Filesystem + inline templates; creates files that the bootstrap (`create_app`) will discover at startup |
| `_load_project_settings()` | `hotframe.config.settings.set_settings` / `get_settings` |

---

## Gotchas and design decisions

**1. Headless `ModuleRuntime` for module commands.**
The `modules install/activate/deactivate/uninstall/update` commands instantiate `ModuleRuntime(app=None, ...)`. This means the runtime cannot mount routes (there is no FastAPI app), but it can modify the database and filesystem. Routes will be mounted when the server starts its lifespan.

**2. Synchronous URL for migrations.**
Alembic does not support async drivers. `makemigrations` and `migrate` convert the async URL (`+asyncpg`, `+aiosqlite`) to its sync equivalent by stripping the suffix. This requires the sync drivers to be installed (`psycopg2`, `aiosqlite` in sync mode).

**3. `startproject .` rejects pre-existing files.**
The list of permitted files is fixed. If you have a file not on the list (e.g., `README.md`), the command fails. This is intentional: it prevents accidentally overwriting an existing project.

**4. No `INSTALLED_APPS` in the generated `settings.py`.**
`startproject` generates `settings.py` without `INSTALLED_APPS`. Apps are discovered automatically by scanning `apps/`. You only need `INSTALLED_APPS` if you want to restrict discovery.

**5. `pyproject.toml` is never overwritten.**
If `pyproject.toml` already exists (e.g., because the project was created with `uv init`), `startproject` respects it and does not overwrite it. This allows running `hf startproject .` in a `uv` project without losing declared dependencies.

**6. `hf shell` runs the full lifespan.**
Unlike Django's shell (which only imports code), `hf shell` executes the complete server startup: initializes the database engine, registries, and `ModuleRuntime`. This makes the shell more faithful to production behavior, at the cost of a slower startup.

**7. Two separate debounce mechanisms.**
`runserver` uses uvicorn's own debounce to reload the process. `dev/autoreload.py` uses a 300ms debounce with `watchfiles` to hot-reload individual modules. These are independent mechanisms operating at different levels of granularity.
