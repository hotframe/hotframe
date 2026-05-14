# 19. Development autoreload (`dev/`)

> `dev/` is the hotframe sub-package that adds development capabilities that
> have no place in production. Its centrepiece is `ModuleWatcher`: a file
> watcher that detects changes in dynamic modules and reloads them
> **without restarting the process**. In production this sub-package is a
> complete no-op.

---

## What this folder is for

When `DEBUG=True`, hotframe can activate a watcher that monitors the
`modules/` directory for changes. Each time the code of an active module
changes, the watcher calls the `ModuleRuntime`'s hot-reload callback, which
unmounts the module, clears `sys.modules`, and mounts it again — all inside
the same running uvicorn process.

This is different from uvicorn's reload (which kills and relaunches the
entire process): hotframe's module hot-reload is **surgical** — it affects
only the module whose code changed and leaves all others untouched.

---

## File map

| File | Responsibility |
|---|---|
| [`dev/__init__.py`](../src/hotframe/dev/__init__.py) | Docstring describing the sub-package and its key exports. Imports nothing at module level — intentionally lazy. |
| [`dev/autoreload.py`](../src/hotframe/dev/autoreload.py) | The `ModuleWatcher` class with the asynchronous watch loop. |

---

## `dev/__init__.py` — lazy design

The sub-package's `__init__.py` does **not** import `ModuleWatcher` directly.
It only documents the package via its docstring. The import happens where the
code needs it:

```python
from hotframe.dev.autoreload import ModuleWatcher
```

This is intentional: if `watchfiles` is not installed, importing
`hotframe.dev` does not fail. The failure only occurs when
`ModuleWatcher._watch_loop` is actually used — which is only when it is
truly needed.

---

## `ModuleWatcher`

**Location:** [`dev/autoreload.py`](../src/hotframe/dev/autoreload.py)

### Purpose

Recursively watches a `modules/` directory for changes to `.py`, `.html`,
`.json`, and `.jinja2` files. When a change is detected, it identifies which
module is affected and calls a callback with the `module_id`.

### Class attributes

```python
WATCH_EXTENSIONS = frozenset({".py", ".html", ".json", ".jinja2"})
```

Only changes to these file types are processed. Changes to `.pyc`, `.db`,
`.lock`, or other files are silently ignored.

### Instance state

```python
def __init__(self) -> None:
    self._task: asyncio.Task | None = None
    self._stop_event: asyncio.Event = asyncio.Event()
```

- `_task`: the asyncio task running the watch loop. `None` when the watcher
  is stopped.
- `_stop_event`: an asyncio event that is set when `stop()` is called. It
  is passed to `watchfiles.awatch()` as a clean-stop signal.

### `start(modules_dir, on_change)`

**Signature:**
```python
async def start(
    self,
    modules_dir: Path,
    on_change: Callable[[str], object],
) -> None
```

Starts the watch loop as a background asyncio task.

**Arguments:**
- `modules_dir`: path to the root modules directory (typically
  `<project>/modules/`).
- `on_change`: callable that receives the `module_id` of the changed module.
  It may be synchronous or a coroutine — `_watch_loop` detects the type with
  `asyncio.iscoroutine()` and `await`s it if necessary.

**Idempotency behaviour:** if the watcher is already running, logs a warning
and returns without starting a second task.

```python
if self._task is not None:
    logger.warning("ModuleWatcher already running")
    return
```

### `stop()`

**Signature:** `async def stop(self) -> None`

Stops the watcher cleanly:

1. Sets `_stop_event` — `watchfiles.awatch()` detects this and terminates
   its generator.
2. Cancels the `_task` asyncio task.
3. Waits for the task to finish, absorbing `CancelledError`.
4. Resets `_task = None`.

### `is_running` (property)

```python
@property
def is_running(self) -> bool:
    return self._task is not None and not self._task.done()
```

Useful for health checks or guard logic.

### `_watch_loop(modules_dir, on_change)` — internal implementation

**Signature:**
```python
async def _watch_loop(
    self,
    modules_dir: Path,
    on_change: Callable[[str], object],
) -> None
```

This is the heart of the watcher. Its structure:

```python
try:
    from watchfiles import awatch
except ImportError:
    logger.warning("watchfiles not installed — hot-reload disabled. ...")
    return
```

If `watchfiles` is not installed, the watcher disables itself silently with
a log warning. **No exception is raised** — this is deliberate so that the
framework does not fail in minimal environments without `watchfiles`.

The main loop:

```python
debounce_ms = 300
recently_reloaded: dict[str, float] = {}

async for changes in awatch(
    modules_dir,
    stop_event=self._stop_event,
    debounce=debounce_ms,
    recursive=True,
):
    ...
```

`watchfiles.awatch()` is an asynchronous generator that uses native OS
mechanisms (FSEvents on macOS, inotify on Linux, ReadDirectoryChangesW on
Windows) to notify of changes. The `debounce=300` parameter causes `awatch`
to accumulate changes for 300 ms before emitting them as a batch —
preventing multiple reloads when an editor saves several files at once.

### Double deduplication and debounce

Inside the loop, hotframe applies its own deduplication layer **on top of**
watchfiles' debounce:

```python
changed_modules: set[str] = set()

for _change_type, changed_path in changes:
    path = Path(changed_path)
    if path.suffix not in self.WATCH_EXTENSIONS:
        continue
    module_id = self._extract_module_id(modules_dir, path)
    if module_id:
        changed_modules.add(module_id)

now = asyncio.get_event_loop().time()
for module_id in changed_modules:
    last = recently_reloaded.get(module_id, 0)
    if now - last < 1.0:  # debounce: skip if reloaded less than 1 s ago
        continue
    recently_reloaded[module_id] = now
    ...
    result = on_change(module_id)
    if asyncio.iscoroutine(result):
        await result
```

There are two debounce levels:

1. **watchfiles (300 ms):** accumulates OS events into a batch before
   emitting them.
2. **`recently_reloaded` (1 s):** hotframe prevents the same module from
   being reloaded more than once per second. This guards against editors
   that perform multiple writes (backup file, swap file, final write).

Errors in the `on_change` callback are caught with `logger.exception` and
do not abort the loop — the watcher keeps watching for changes.

### `_extract_module_id(modules_dir, changed_path)` — static method

**Signature:**
```python
@staticmethod
def _extract_module_id(modules_dir: Path, changed_path: Path) -> str | None
```

Extracts the `module_id` from the absolute path of the changed file:

```
modules_dir  = /home/user/myapp/modules
changed_path = /home/user/myapp/modules/inventory/routes.py
  → relative = inventory/routes.py
  → parts[0] = "inventory"
  → return "inventory"
```

If `changed_path` is not under `modules_dir` (e.g. a spurious event),
catches `ValueError` and returns `None`.

---

## How to integrate `ModuleWatcher` in the bootstrap

The watcher does not start automatically. The hotframe bootstrap integrates
it during the lifespan, conditional on `DEBUG`:

```python
# Usage pattern in hotframe.bootstrap (lifespan)
if settings.DEBUG:
    from hotframe.dev.autoreload import ModuleWatcher
    watcher = ModuleWatcher()
    await watcher.start(
        modules_dir=Path(settings.MODULES_DIR),
        on_change=module_runtime.hot_reload,  # ModuleRuntime method
    )
```

In the lifespan shutdown:

```python
if settings.DEBUG and watcher.is_running:
    await watcher.stop()
```

The typical `on_change` is `module_runtime.hot_reload`, which is a
coroutine: it unmounts the module with `deactivate`, clears `sys.modules`,
and re-mounts it with `activate`.

---

## Full lifecycle of a file edit

1. The developer saves `modules/inventory/routes.py` in their editor.
2. The OS emits a change event through FSEvents/inotify.
3. `watchfiles.awatch()` accumulates the event for 300 ms and emits it as
   `{(ChangeType.modified, "/path/to/inventory/routes.py")}`.
4. `_watch_loop` receives the batch, extracts `module_id = "inventory"`.
5. Checks that `.py` is in `WATCH_EXTENSIONS` — yes.
6. Checks that it was not reloaded less than 1 s ago — OK.
7. Calls `on_change("inventory")` — in practice
   `module_runtime.hot_reload("inventory")`.
8. `hot_reload` unmounts the module: clears FastAPI routes, clears
   `sys.modules["modules.inventory"]` and sub-modules, releases memory.
9. `hot_reload` re-activates the module: imports the fresh package, mounts
   routes, registers events/hooks/slots, runs `ready()`.
10. The next HTTP request to the module uses the new code.

All of this happens **inside the same uvicorn process** — no restart, no
loss of WebSocket session state, no impact on other modules.

---

## Optional dependencies

| Dependency | Usage | Installation |
|---|---|---|
| `watchfiles` | File-watching engine (FSEvents/inotify) | `pip install watchfiles` |

`watchfiles` is an **optional** dependency. If it is not installed, the
watcher simply does not work (a warning is logged) but the rest of the
framework is fully operational.

---

## How this fits into the rest of the framework

| Component | Relationship with `dev/` |
|---|---|
| `hotframe.engine.module_runtime.ModuleRuntime` | Its `hot_reload(module_id)` method is the natural `on_change` callback |
| `hotframe.bootstrap.create_app` | Starts and stops `ModuleWatcher` during the lifespan when `DEBUG=True` |
| `hotframe.config.settings.HotframeSettings` | `DEBUG` and `MODULES_DIR` determine whether the watcher is activated and which directory it watches |
| `management/cli.py` → `runserver` | Starts uvicorn with `reload=True` — this is **separate** from `ModuleWatcher`: uvicorn reload restarts the entire process, `ModuleWatcher` only reloads individual modules |

---

## Gotchas and design decisions

**1. Two reload mechanisms in development.**
`hf runserver` starts uvicorn with `reload=True`, which restarts the entire
process whenever any Python file in the application changes. `ModuleWatcher`
only reloads the affected module within the running process. In practice
they coexist: uvicorn reloads static application code (code in `apps/`) and
the watcher reloads dynamic modules (code in `modules/`). Both are active
when `DEBUG=True`.

**2. The callback can be synchronous or asynchronous.**
`_watch_loop` detects whether the result of `on_change(module_id)` is a
coroutine via `asyncio.iscoroutine(result)` and `await`s it if necessary.
This allows passing simple synchronous callbacks (for testing or extensions)
without needing to wrap them.

**3. Errors during hot-reload do not abort the watcher.**
If `hot_reload` raises an exception (e.g. a syntax error in the newly edited
module), `logger.exception` records it and the loop continues. The module
is left deactivated (the `deactivate` already ran) and the developer will
see the error in the log. Saving a corrected file triggers another event and
the reload is retried.

**4. `recently_reloaded` is never cleaned up.**
The `recently_reloaded` dictionary grows indefinitely with the IDs of
modules that have been reloaded. In projects with many modules and long
development sessions this has no practical impact (the values are short
strings), but it is a design note: in v1.0 there is no GC for that
dictionary.

**5. Only `modules/` is watched, not `apps/`.**
Changes in `apps/` are handled by uvicorn's reload (which watches the
entire project directory). `ModuleWatcher` is specific to dynamic modules.

**6. `WATCH_EXTENSIONS` includes `.jinja2`.**
Templates also trigger a module hot-reload. When a developer edits a
module's template, the module is reloaded. This is necessary because the
Jinja2 engine may cache templates; reactivating the module clears those
caches.
