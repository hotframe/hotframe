# 2. Convention-based auto-discovery (`discovery/`)

> `discovery/` is the framework's filesystem scanner: given a directory, it
> detects which files exist, imports them in a deterministic order, and
> returns a structured description of what it found — without mounting
> anything. Orchestration (route mounting, registration in the AppRegistry)
> is the responsibility of the layer above.

---

## What this folder is for

When hotframe starts up, it needs to know which apps and modules exist on
disk, which files they contain, and which of those files to import.
`discovery/` solves exactly that with two pieces:

1. **`conventions.py`** — the source of truth that defines which files are
   "conventional" and what role each one plays.
2. **`scanner.py`** — the engine that traverses the directory applying those
   conventions, imports the corresponding Python modules, and returns a list
   of `DiscoveryResult` objects.

The separation matters: if the conventions were hard-coded inside the
scanner they would be invisible and hard to test. By expressing them as data
in `conventions.py`, they are documentable, replaceable, and can be
inspected at runtime.

---

## File map

| File | Responsibility |
|---|---|
| [`__init__.py`](../src/hotframe/discovery/__init__.py) | Docstring only. The package re-exports nothing; consumers import directly from `scanner`. |
| [`conventions.py`](../src/hotframe/discovery/conventions.py) | Defines `Kind`, `Convention`, and `APP_CONVENTIONS`: the table that maps filename → semantic role. |
| [`scanner.py`](../src/hotframe/discovery/scanner.py) | Implements `scan()`, `_scan_subdir()`, and `find_entry_config()`. Produces a `DiscoveryResult` for each subdirectory found. |

---

## `conventions.py` — the source of truth

### Enum `Kind`

```python
class Kind(str, Enum):
    ENTRY_POINT = "entry_point"
    MODELS      = "models"
    ROUTES      = "routes"
    API         = "api"
    SCHEMAS     = "schemas"
    SERVICES    = "services"
    REPOSITORY  = "repository"
    SIGNALS     = "signals"
    MIGRATIONS  = "migrations"
    TEMPLATES   = "templates"
    STATIC      = "static"
    LOCALES     = "locales"
    TESTS       = "tests"
    MANAGEMENT  = "management"
```

Each value represents a semantic role within an app or module. The scanner
assigns a `Kind` to every artifact it detects; upper layers consult the
`Kind` to decide what to do with it (mount routes, register signals, etc.).

`Kind` inherits from `str` so that it can be serialised directly to JSON and
compared with string literals where convenient.

---

### Dataclass `Convention`

```python
@dataclass(frozen=True, slots=True)
class Convention:
    filename_or_dir: str          # e.g. "models.py" or "templates"
    kind: Kind
    is_directory: bool = False
    optional: bool = True         # if False, absence is an error
    required_exports: tuple[str, ...] = ()
```

`required_exports` implements **at-least-one-of** semantics: if the field is
non-empty, the imported module must expose at least one of the listed names.
This lets a single convention accept several historical forms:

- `routes.py` accepts both `urlpatterns` (Django style) and `router`
  (FastAPI style).
- `api.py` accepts both `router` and `api_router` (legacy alias).

If none of the names is present, the scanner raises `DiscoveryError`.

---

### Tuple `APP_CONVENTIONS`

The full table, in declaration order (affects only logging, not logic):

| `filename_or_dir` | `kind` | `is_directory` | `required_exports` |
|---|---|---|---|
| `app.py` | `ENTRY_POINT` | No | — |
| `module.py` | `ENTRY_POINT` | No | — |
| `models.py` | `MODELS` | No | — |
| `routes.py` | `ROUTES` | No | `("urlpatterns", "router")` |
| `api.py` | `API` | No | `("router", "api_router")` |
| `schemas.py` | `SCHEMAS` | No | — |
| `services.py` | `SERVICES` | No | — |
| `repository.py` | `REPOSITORY` | No | — |
| `signals.py` | `SIGNALS` | No | — |
| `migrations` | `MIGRATIONS` | Yes | — |
| `templates` | `TEMPLATES` | Yes | — |
| `static` | `STATIC` | Yes | — |
| `locales` | `LOCALES` | Yes | — |
| `tests` | `TESTS` | Yes | — |
| `management` | `MANAGEMENT` | Yes | — |

All items are `optional=True`. No file is mandatory except for the XOR
constraint on `app.py` / `module.py` that the scanner enforces explicitly.

---

### Helper function `conventions_by_kind()`

```python
def conventions_by_kind() -> dict[Kind, tuple[Convention, ...]]:
```

Groups the `APP_CONVENTIONS` table by `Kind`. Useful when an upper layer
wants to query "which conventions correspond to `Kind.TEMPLATES`?" without
iterating the full table.

---

## `scanner.py` — the discovery engine

### Class `DiscoveryError`

```python
class DiscoveryError(Exception):
```

Raised when a directory violates the conventions: it has both `app.py` and
`module.py`, or a file with `required_exports` does not expose any of the
expected names. This is a programming error (the project structure is
incorrect), not a runtime error.

---

### Dataclass `FileArtifact`

```python
@dataclass(slots=True)
class FileArtifact:
    convention: Convention
    path: Path
    imported_module: ModuleType | None = None
```

Represents a file or directory detected inside an app. `imported_module` is
populated only when `import_side_effects=True` and the import succeeds. On
failure, the error is stored in `DiscoveryResult.errors`.

---

### Dataclass `DiscoveryResult`

```python
@dataclass(slots=True)
class DiscoveryResult:
    name: str            # e.g. "accounts"
    root_path: Path      # e.g. /path/to/apps/accounts
    package_name: str    # e.g. "apps.accounts"
    entry_point: FileArtifact | None = None
    artifacts: list[FileArtifact] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
```

The `entry_point` (`app.py` or `module.py`) is stored separately from
`artifacts` because it has special semantics: it is the file that contains
the `AppConfig` or `ModuleConfig`. All other artifacts (models, routes,
signals, …) go into the `artifacts` list.

#### Method `find(kind: Kind) -> FileArtifact | None`

```python
def find(self, kind: Kind) -> FileArtifact | None:
    if kind == Kind.ENTRY_POINT:
        return self.entry_point
    for a in self.artifacts:
        if a.convention.kind == kind:
            return a
    return None
```

Shortcut for checking whether an artifact of a given `Kind` is present. For
example, to check whether an app has migrations:

```python
result = scan(apps_dir, package_prefix="apps")[0]
if result.find(Kind.MIGRATIONS):
    print("has migrations/")
```

#### Property `has_entry_point`

```python
@property
def has_entry_point(self) -> bool:
    return self.entry_point is not None
```

---

### Constant `_SKIP_DIRS`

```python
_SKIP_DIRS: frozenset[str] = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".git",
})
```

Directories that are always ignored during scanning. In addition, any
directory whose name starts with `.` is also skipped.

---

### Function `scan(root, *, package_prefix, import_side_effects=True) -> list[DiscoveryResult]`

```python
def scan(
    root: Path,
    *,
    package_prefix: str,
    import_side_effects: bool = True,
) -> list[DiscoveryResult]:
```

The main entry point. Iterates the subdirectories of `root` in alphabetical
order (to guarantee determinism) and calls `_scan_subdir` for each one.

**Parameters:**

- `root` — root directory to scan. Must exist and be a directory; otherwise
  raises `DiscoveryError`.
- `package_prefix` — Python prefix for constructing import names. If `root`
  is `apps/` and `package_prefix="apps"`, modules are imported as
  `apps.<app_name>.<file>`.
- `import_side_effects` — if `False`, the scanner only collects paths
  without calling `import_module`. Useful in unit tests where you do not
  want to execute user code.

**Return value:** a list of `DiscoveryResult`, one per eligible
subdirectory. Subdirectories in `_SKIP_DIRS` or starting with `.` are
silently skipped.

Usage example (from the engine guide):

```python
from pathlib import Path
from hotframe.discovery.scanner import scan, Kind

results = scan(Path("apps"), package_prefix="apps")
for result in results:
    if result.errors:
        print(f"[{result.name}] errors: {result.errors}")
    routes = result.find(Kind.ROUTES)
    if routes and routes.imported_module:
        router = getattr(routes.imported_module, "router", None)
        # mount router in FastAPI...
```

---

### Internal function `_scan_subdir`

```python
def _scan_subdir(
    subdir: Path,
    *,
    package_prefix: str,
    import_side_effects: bool,
) -> DiscoveryResult:
```

Scans a single subdirectory. The flow is:

1. Builds `package_name = f"{package_prefix}.{name}"`.
2. Enforces the XOR constraint: `app.py` and `module.py` cannot coexist. If
   both are present, raises `DiscoveryError` immediately.
3. Iterates `APP_CONVENTIONS` in order:
   - For **directory** conventions (`is_directory=True`): if the directory
     exists, creates a `FileArtifact` and appends it to `artifacts`. No
     import is performed.
   - For **file** conventions: if the file exists, attempts to import it
     with `importlib.import_module(f"{package_name}.{stem}")`. On failure,
     appends the error to `result.errors` (no exception is raised, to avoid
     aborting startup due to one broken module). If `required_exports` is
     set, verifies that at least one name is present; otherwise raises
     `DiscoveryError`.
4. If the artifact is of `Kind.ENTRY_POINT`, assigns it to
   `result.entry_point`; otherwise appends it to `result.artifacts`.

A subtle point: import errors are accumulated in `result.errors` but do not
interrupt the scan. The orchestrating layer (the engine) decides how to
handle those errors: it can log and continue, or put the module into an
`error` state.

---

### Function `find_entry_config(result: DiscoveryResult) -> type`

```python
def find_entry_config(result: DiscoveryResult) -> Any:
```

Once `scan()` has imported the `entry_point` (the `app.py` or `module.py`),
this function extracts the `AppConfig` or `ModuleConfig` class declared in
it.

The logic:
1. Verifies that `result.entry_point` exists and that its module was
   imported.
2. Imports `hotframe.apps.config` lazily (to avoid creating a static
   dependency on an upper layer — see "Design decisions").
3. Iterates the module's members looking for classes that:
   - Are defined in that module (not imported from elsewhere).
   - Inherit from `AppConfig`.
   - Are not `AppConfig` or `ModuleConfig` themselves (concrete subclasses
     only).
4. If exactly one candidate is found, returns it. If zero or more than one
   is found, raises `DiscoveryError`.

This is what allows the engine to instantiate the config without knowing in
advance which class the project uses:

```python
config_cls = find_entry_config(result)
config = config_cls()
await registry.register(config)
```

---

## How this fits into the rest of the framework

**During static app bootstrap**, `bootstrap.py` calls
`_auto_discover_apps(app)`, which does the same work as `scan()` but more
directly via `importlib` (the formal scanner is what the engine uses for
dynamic modules). Both paths honour the same conventions defined in
`APP_CONVENTIONS`.

**During dynamic module activation**, `engine/module_runtime.py` calls
`scan()` with `modules/` as `root` and `package_prefix="modules"`. The
resulting `DiscoveryResult` tells it which artifacts to mount (routes,
signals, components, statics) and `find_entry_config()` gives it the config
class to register in the `AppRegistry`.

**In tests**, `import_side_effects=False` allows verifying that the
directory structure is correct without executing any import. This makes
structural tests instantaneous.

---

## Gotchas and design decisions

**The scanner is a deliberate middle layer.**
The comment in `scanner.py` makes this explicit: "this module must NOT
statically import `hotframe.apps` because `hotframe.apps` lives in an upper
layer". That is why `find_entry_config()` uses
`importlib.import_module("hotframe.apps.config")` inside the function body
instead of a module-level import. Breaking this rule would create a circular
dependency.

**Import errors do not stop startup.**
`_scan_subdir` catches `ImportError` and `Exception` and accumulates them in
`result.errors` instead of propagating the exception. This is intentional: a
broken module must not prevent the rest of the application from starting. The
orchestrating layer defines the error policy.

**The `app.py` / `module.py` XOR constraint is strict.**
If a directory contains both files, `DiscoveryError` is raised immediately.
This prevents any ambiguity about what type of entity the directory
represents.

**Scan order is alphabetical.**
`sorted(root.iterdir(), key=lambda p: p.name)` guarantees that the discovery
order is deterministic on any filesystem. This is important so that route
mounting order is predictable and reproducible across restarts.

**`required_exports` is at-least-one-of, not all-of.**
A `routes.py` that exposes both `urlpatterns` and `router` is valid (though
unusual). It only fails if it exposes neither. This provides room to
manoeuvre during transitions between API styles.
