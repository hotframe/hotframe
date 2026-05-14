# 10. Stateless components (components/)

> The `components/` subsystem implements the simplest reusable UI unit in hotframe:
> a directory containing a `template.html` and, optionally, a Pydantic props class, a
> FastAPI router, and its own static assets — no server state, no WebSocket.

---

## What this folder is for

A stateless component is the answer to "how do I reuse this chunk of HTML without copy-pasting
or breaking encapsulation?" In hotframe, the answer is: create a directory at
`apps/<app>/components/<name>/` or `modules/<id>/components/<name>/`, put a `template.html`
inside, and you can invoke it from any template with
`{{ render_component('name', prop=val) }}` or
`{% component 'name' prop=val %}...{% endcomponent %}`.

The `components/` folder manages the entire lifecycle of those definitions:

1. **Discovery** (`discovery.py`) — scans the filesystem and builds `ComponentEntry` objects.
2. **Registry** (`registry.py`) — an in-RAM catalog indexed by name, with per-module cleanup.
3. **Isolated rendering** (`rendering.py`) — validates props, builds an isolated context (no
   variable leakage from the parent), renders the template.
4. **Jinja2 extension** (`jinja_ext.py`) — the `{% component %}` tag with body support.
5. **Router and asset mounting** (`mounting.py`) — attaches each component's routers and
   `static/` directories to the `FastAPI` app.
6. **Base class** (`base.py`) — `Component`, the Pydantic model you declare in `component.py`.
7. **Entry** (`entry.py`) — `ComponentEntry`, the in-RAM descriptor of a registered component.

---

## File map

| File | Responsibility |
|---|---|
| [`__init__.py`](../src/hotframe/components/__init__.py) | Re-exports the subsystem's public API |
| [`base.py`](../src/hotframe/components/base.py) | `Component` class — Pydantic base for typed props |
| [`entry.py`](../src/hotframe/components/entry.py) | `ComponentEntry` dataclass — in-RAM descriptor |
| [`discovery.py`](../src/hotframe/components/discovery.py) | Filesystem scanning and entry construction |
| [`registry.py`](../src/hotframe/components/registry.py) | `ComponentRegistry` — in-memory catalog |
| [`rendering.py`](../src/hotframe/components/rendering.py) | `render_component` Jinja2 global + `_render_entry` |
| [`jinja_ext.py`](../src/hotframe/components/jinja_ext.py) | `ComponentExtension` — the `{% component %}` tag |
| [`mounting.py`](../src/hotframe/components/mounting.py) | Mount and unmount of routers and static assets |

---

## `base.py` — The `Component` class

```python
class Component(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    def context(self) -> dict:
        return {}
```

`Component` is a `pydantic.BaseModel` with a single extension: the `context()` method.

### How to use it

```python
# apps/shared/components/media_picker/component.py
from hotframe.components import Component

class MediaPicker(Component):
    path: str
    multiple: bool = False
    accept: str = "image/*"

    def context(self) -> dict:
        return {"accept_list": self.accept.split(",")}
```

When the framework renders this component it:

1. Instantiates `MediaPicker(**props_from_caller)` — Pydantic validates and coerces.
2. Calls `instance.model_dump()` to obtain the props as a dict.
3. Calls `instance.context()` and merges the result.
4. Renders `template.html` with that combined dict plus the framework slice.

### `context()` — for derived values

The `context()` method is synchronous by design (hotframe's Jinja2 environment is synchronous).
Its purpose is to expose values computed from props that you do not want to calculate inside the
template. If you need database data, load it in the endpoint or the view that invokes the
component and pass it as a prop.

### Difference from `LiveComponent`

`Component` and `LiveComponent` are independent hierarchies — `LiveComponent` does not inherit
from `Component`. This separation is intentional: LiveComponents have their own lifecycle
(Pydantic + state + lifecycle hooks + WebSocket) that is incompatible with the stateless model
of `Component`. The discovery layer handles both base classes.

---

## `entry.py` — `ComponentEntry`

```python
@dataclass(slots=True)
class ComponentEntry:
    name: str
    template: str
    has_endpoint: bool = False
    render_fn: Callable[..., dict[str, Any]] | None = None
    extra_router: APIRouter | None = None
    module_id: str | None = None
    static_dir: str | None = None
    props_cls: type | None = None
    is_live: bool = False
```

This dataclass is a component's "passport" inside the framework. Here is what each field carries:

| Field | Meaning |
|---|---|
| `name` | The unique identifier: `"badge"`, `"data_table"`, etc. |
| `template` | Jinja2 path relative to the loader: `"shared/components/badge/template.html"` |
| `has_endpoint` | `True` if `routes.py` declared a `router` |
| `render_fn` | Callable `(**props) -> dict` built by discovery; `None` if the component is template-only |
| `extra_router` | The `APIRouter` from `routes.py`, ready to mount |
| `module_id` | Owning module; `None` for static app components |
| `static_dir` | Absolute path to the component's `static/` directory, or `None` |
| `props_cls` | The `Component` or `LiveComponent` class declared in `component.py` |
| `is_live` | `True` when `props_cls` is a subclass of `LiveComponent` |

`is_live` lets the live runtime quickly distinguish between stateless and stateful components
without calling `issubclass` in the hot path of every WebSocket event.

---

## `discovery.py` — Component discovery

The discovery engine scans the filesystem, imports `component.py` and `routes.py` where they
exist, and builds a list of `ComponentEntry` objects. It is entirely synchronous and has only
two side effects: writing to `sys.modules` and calling `registry.register`.

### `_load_module_from_file(py_path, module_name)`

```python
def _load_module_from_file(py_path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, py_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
```

Imports any `.py` file by filesystem path, registers it in `sys.modules` under a synthetic
name, and returns it. This is necessary because components live outside Python's normal package
tree (they are under `apps/` or `modules/`, not inside an importable package).

The `module_name` uses prefixes such as
`_hotframe_components.{module_id}.{name}.component` to avoid `sys.modules` collisions between
components from different modules.

### `_find_component_class(module) -> type | None`

```python
def _find_component_class(module) -> type | None:
    bases = (Component, LiveComponent)
    for _attr_name, attr in inspect.getmembers(module, inspect.isclass):
        if attr in bases:
            continue
        if issubclass(attr, bases) and attr.__module__ == module.__name__:
            return attr
    # Fallback: accept even if the class was re-exported from another module
    ...
```

Finds the first subclass of `Component` or `LiveComponent` in the imported module. The
`attr.__module__ == module.__name__` condition prioritises classes defined in that file (rather
than imported ones). If none match that condition, the fallback accepts any subclass, supporting
the pattern of re-exporting the class from another module.

### `_build_render_fn(props_cls) -> Callable | None`

Builds the render function for the component based on the type of `props_cls`:

- `props_cls is None` (template-only) → returns `None`. The caller's kwargs are passed directly
  as context.
- `issubclass(props_cls, LiveComponent)` → returns `None`. LiveComponents have their own render
  path in `hotframe.live.diff`.
- `issubclass(props_cls, Component)` → returns the closure `render_fn(**props) -> dict`.

The resulting `render_fn`:

```python
def render_fn(**props) -> dict:
    instance = props_cls(**props)       # Pydantic validation
    context = instance.model_dump()     # validated props as dict
    extra = instance.context()          # derived values
    if extra:
        context.update(extra)
    return context
```

### `discover_components(root, *, module_id, template_search_prefix, import_prefix)`

The main function. Iterates `root` in alphabetical order, skips hidden directories and
directories without a `template.html`, and for each valid component:

1. Finds and loads `component.py` → `props_cls` + detects `is_live`.
2. Finds and loads `routes.py` → `extra_router`.
3. Detects whether a `static/` directory exists → `static_dir`.
4. Computes the template path: `{template_search_prefix}/{name}/template.html`.
5. Builds the `ComponentEntry` with all fields populated.

Returns the list of entries without touching the registry — it is pure data construction.

### Scoped discovery helpers

```python
discover_module_components(registry, module_dir, module_id) -> int
discover_app_components(registry, apps_dir, app_name) -> int
discover_apps_components(registry, apps_dir) -> int
```

These three helpers are the entry points used by the actual bootstrap:

- `discover_apps_components` scans all apps under `apps/` in a single pass at startup.
- `discover_module_components` scans a specific module when it is activated (called by
  `ModuleRuntime`).

The difference in `template_search_prefix` values matters:
- Apps: `"shared/components"` → template referenced as `shared/components/badge/template.html`
- Modules: `"loyalty/components"` → template referenced as `loyalty/components/widget/template.html`

Jinja2's `FileSystemLoader` has `apps/` and `modules_dir/` in its search path, so these
prefixes resolve correctly.

---

## `registry.py` — `ComponentRegistry`

```python
class ComponentRegistry:
    def __init__(self) -> None:
        self._components: dict[str, ComponentEntry] = {}
```

A flat dict indexed by component name, stored in `app.state.components`.

### Full API

| Method | Signature | Description |
|---|---|---|
| `register` | `(entry, *, module_id=None)` | Registers an entry; if `module_id` is provided it is written into `entry.module_id`. Name collision → warning + overwrite |
| `unregister` | `(name)` | Removes by name; no-op if not found |
| `unregister_module` | `(module_id)` | Removes all entries belonging to the module |
| `get` | `(name) -> ComponentEntry \| None` | Lookup by name |
| `has` | `(name) -> bool` | Existence check |
| `list_components` | `() -> list[ComponentEntry]` | List in insertion order |
| `clear` | `()` | Empties everything (tests) |
| `__len__` | — | Number of registered components |
| `__contains__` | — | `"badge" in registry` |

### Collision behaviour

When two entities register a component under the same name, the registry logs:

```
Component name collision: 'badge' is being overwritten
(previous module=shared, new module=premium_ui)
```

and overwrites with the new entry. This is intentional: in development, when a module reloads,
its updated definitions must replace the old ones without requiring a server restart.

### Cleanup on module unload

```python
def unregister_module(self, module_id: str) -> None:
    to_remove = [
        name for name, entry in self._components.items()
        if entry.module_id == module_id
    ]
    for name in to_remove:
        del self._components[name]
```

`ModuleRuntime` calls `unregister_module` when deactivating a module. From that point on, any
`render_component('module_widget')` will find `None` in the registry and return `Markup("")`
(with a warning in the log).

---

## `rendering.py` — Isolated rendering

This module implements the heart of stateless component rendering: the isolated context and the
`render_component` function exposed as a Jinja2 global.

### The "framework slice" — isolated context

```python
_FRAMEWORK_CONTEXT_KEYS = (
    "request",
    "csrf_token",
    "csp_nonce",
    "user",
    "current_path",
)
```

When a component is rendered, the new context receives only:
1. The component's validated props.
2. The 5 framework-slice keys extracted from the parent context.

**Nothing else.** Local variables from the calling template are not inherited. This isolation is
an explicit design decision: it prevents bugs where a general-purpose component works on one
page but silently depends on a variable that another page does not define.

```python
def _framework_slice(ctx: Context) -> dict:
    return {key: ctx.get(key) for key in _FRAMEWORK_CONTEXT_KEYS if key in ctx}
```

### `_render_entry(env, ctx, entry, props, body=None) -> Markup`

The central, non-public function. Sequence of operations:

1. If `entry.render_fn` is not `None`: calls `render_fn(**props)`, catching `ValidationError`
   (invalid props) and `TypeError` (unexpected kwargs). In both cases it returns an HTML comment
   containing the error instead of raising.
2. If `entry.render_fn` is `None` (template-only): uses the raw `props` as context.
3. Merges the framework slice on top of the context.
4. If `body` was passed (from the `{% component %}` tag), sets `context["body"] = Markup(body)`.
5. Loads the template with `env.get_template(entry.template)`.
6. Returns `Markup(template.render(**context))`.

All errors return `Markup("")` or an HTML comment — templates never surface exceptions to the
user because of a component failure.

### `render_component` — The Jinja2 global

```python
@pass_context
def render_component(ctx: Context, __component_name__: str, /, **props) -> Markup:
```

Decorated with Jinja2's `@pass_context` to receive the active rendering context.
The component name is a positional-only parameter (`/`) so that `name=...` can be a prop
without colliding with the dispatch argument.

Flow:
1. Retrieves the `ComponentRegistry` from `ctx.environment.globals["_hotframe_components"]`.
2. Looks up the entry by name.
3. Delegates to `_render_entry`.

### `register_component_globals(env)`

```python
def register_component_globals(env: Environment) -> None:
    env.globals["render_component"] = render_component
```

Registers the global on the environment. Called by `create_template_engine`.

---

## `jinja_ext.py` — The `{% component %}` tag

The Jinja2 extension that enables block syntax with a body:

```jinja
{% component 'modal' title='Confirm' %}
  <p>Are you sure?</p>
{% endcomponent %}
```

### The `CallBlock` and `ContextVar` problem

Jinja2 implements tags with bodies via `CallBlock`. The problem is that `CallBlock` does not
receive the rendering `Context` — the interpreter calls it in a separate context. So that the
component can still access the framework slice (request, csrf_token, …), the module patches the
environment's context class.

```python
_current_ctx: contextvars.ContextVar = contextvars.ContextVar(
    "hotframe_component_render_ctx", default=None
)
```

### `install_component_context_tracker(env)`

```python
def install_component_context_tracker(env: Environment) -> None:
    original_context_class = env.context_class
    if getattr(original_context_class, "_hotframe_patched", False):
        return

    class _TrackingContext(original_context_class):
        _hotframe_patched = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            _current_ctx.set(self)

    env.context_class = _TrackingContext
```

Every time Jinja2 creates a new `Context` (when it starts rendering a template), the
`_TrackingContext` constructor publishes that context to the `ContextVar`. The function
`_current_render_context()` reads it when the `CallBlock` needs the framework slice. The
`_hotframe_patched` guard makes the operation idempotent.

### `ComponentExtension`

```python
class ComponentExtension(Extension):
    tags = {"component"}

    def parse(self, parser: Parser):
        ...
        body = parser.parse_statements(("name:endcomponent",), drop_needle=True)
        call = self.call_method("_render_component", [name_expr], kwargs)
        return nodes.CallBlock(call, [], [], body).set_lineno(lineno)
```

The parser extracts the component name, collects keyword arguments up to the block end, parses
the body up to `{% endcomponent %}`, and emits a `CallBlock`. `CallBlock` is the Jinja2 AST
node that calls `caller()` to obtain the rendered body HTML.

```python
def _render_component(self, __component_name__, /, *, caller=None, **props) -> Markup:
    env = self.environment
    registry = env.globals.get("_hotframe_components")
    ...
    body = caller() if caller is not None else ""
    ctx = _current_render_context()
    return _render_entry(env, ctx, entry, props, body=str(body))
```

The body is available as `{{ body }}` inside the component's template (it is injected into the
context as `Markup(body)`).

#### Limitation: Python reserved words as props

Jinja2 parses tag kwargs as Python identifiers. `class` is a reserved word, so it cannot be
used directly as a prop name:

```jinja
{# BAD: syntax error #}
{% component 'button' class='btn-primary' %}

{# GOOD: use an attributes dict #}
{% component 'button' attrs={'class': 'btn-primary', 'id': 'submit'} %}
```

This limitation is documented in the module's docstring.

---

## `mounting.py` — Router and asset mounting

### The hot-reload and FastAPI problem

FastAPI/Starlette provide no API for removing routes at runtime. The component system solves
this with the same technique used by `ModuleLoader`: direct mutation of `app.router.routes`.

```python
routes[:] = [
    route for route in routes
    if not _matches_component_subtree(_route_path(route), prefix, prefix_slash)
]
```

The list is filtered in-place, removing the `Mount` entries that correspond to the component.
The OpenAPI schema is then invalidated (`app.openapi_schema = None`) so it is regenerated on
the next request.

### Reserved prefixes

All components use the `/_components/<name>/` namespace:

- Router: `/_components/{name}/`
- Static: `/_components/{name}/static/`

The leading underscore prevents collisions with module routes (`/m/`) and application routes.

### Mounting API

#### For the initial startup (all apps)

```python
mount_component_routers(app, registry) -> int
mount_component_static(app, registry) -> int
```

Iterates all registry entries and mounts those that have an `extra_router` or `static_dir`.

#### For individual modules (hot-mount)

```python
mount_component_routers_for_module(app, registry, module_id) -> int
mount_component_static_for_module(app, registry, module_id) -> int
```

Filters by `entry.module_id == module_id`. Called by `ModuleRuntime` when activating a module.

#### For unmounting

```python
unmount_component_router(app, name) -> bool
unmount_component_routers_for_module(app, module_id) -> int
unmount_component_static(app, name) -> bool
unmount_component_static_for_module(app, module_id) -> int
```

The `_for_module` variants read the `module_id` property of registry entries to identify which
paths to remove. This is why they **must** be called **before**
`registry.unregister_module()` — if the registry no longer holds the entries, it has no way of
knowing which paths to clean up.

### `_mount_single_static`

```python
def _mount_single_static(app: FastAPI, name: str, static_dir: str) -> bool:
```

Mounts a `StaticFiles` instance at `/_components/{name}/static`. Before mounting, it verifies:
1. That the directory exists on disk.
2. That no mount with the same path is already registered (prevents duplicates on hot-reload).

---

## How it fits into the rest of the framework

```
Startup (create_app)
  ├── discover_apps_components(registry, apps_dir)    ← discovery.py
  ├── mount_component_routers(app, registry)          ← mounting.py
  ├── mount_component_static(app, registry)           ← mounting.py
  └── create_template_engine()
        ├── ComponentExtension registered in env      ← jinja_ext.py
        └── register_component_globals(env)           ← rendering.py

ModuleRuntime.activate(module_id)
  ├── discover_module_components(registry, mod_dir, module_id)
  ├── mount_component_routers_for_module(app, registry, module_id)
  └── mount_component_static_for_module(app, registry, module_id)

ModuleRuntime.deactivate(module_id)
  ├── unmount_component_routers_for_module(app, module_id)  ← BEFORE unregister
  ├── unmount_component_static_for_module(app, module_id)   ← BEFORE unregister
  └── registry.unregister_module(module_id)

render_component / {% component %} (on each request)
  ├── ComponentRegistry.get(name)      ← registry.py
  └── _render_entry(env, ctx, entry)   ← rendering.py
```

- **`templating/engine.py`** installs `ComponentExtension` and calls
  `register_component_globals` — the template engine and the components share the same
  `Environment`.
- **`live/`** uses the same `ComponentRegistry` to look up `LiveComponent` entries when
  processing an `attach` over WebSocket. The `entry.is_live` field enables that fast
  distinction.
- **`engine/module_runtime.py`** is the sole consumer of `discover_module_components` and
  `mount_component_*_for_module` — it orchestrates the entire activation/deactivation cycle.

---

## Gotchas and design decisions

**1. Mandatory unmount order.**
`unmount_component_routers_for_module` reads component names from the registry to build the
prefixes it needs to remove. If you call `registry.unregister_module` first, that information
is gone and the routes become orphans in `app.router.routes`. `ModuleRuntime` respects this
order.

**2. `is_live` on the entry to avoid `issubclass` in the hot path.**
Every WebSocket event needs to know whether the component for a given `cid` is live or
stateless. Storing `is_live=True/False` on `ComponentEntry` at discovery time avoids calling
`issubclass(cls, LiveComponent)` on every message. This is a micro-optimisation that also
simplifies the live runtime code.

**3. The `ContextVar` for the framework slice in `{% component %}`.**
The alternative would have been passing the context as an argument to `CallBlock`, but the
Jinja2 API does not allow that directly. `ContextVar` is both thread-safe and coroutine-safe
(asyncio uses `contextvars` contexts per task). The one limitation: if a template is rendered
outside a request (e.g. in a CLI script), `_current_ctx.get()` returns `None` and `_EmptyCtx`
is used, leaving the framework slice empty.

**4. `LiveComponent` subclasses have no `render_fn`.**
`_build_render_fn` returns `None` for `LiveComponent` subclasses. If someone calls
`render_component('todo_list', user_id=1)` from a template (bypassing `{% live %}`), the render
falls through to the template-only path (raw kwargs as context) and logs a warning. It does not
crash, but the output will not include the initial state.

**5. Component names are globally scoped.**
`ComponentRegistry` has a single flat namespace. If module `billing` and module `crm` both
define a component called `invoice_badge`, whichever is activated last overwrites the other
(with a warning). The recommended convention is to use a module prefix:
`billing_invoice_badge` and `crm_invoice_badge`.

**6. `template_search_prefix` and the two-level loader.**
Discovery establishes that template paths are
`<app_name>/components/<name>/template.html`. Jinja2's loader has `apps/` in its search path,
so `apps/shared/components/badge/template.html` resolves as
`shared/components/badge/template.html`. This two-level arrangement (loader root + template
prefix) is what allows components from different origins to coexist without filename
collisions.

**7. `model_config = {"arbitrary_types_allowed": True}` on `Component`.**
This allows non-Pydantic types in props (e.g. SQLAlchemy objects, `Request`). Without it,
declaring `request: Request` as a prop would raise a validation error. Enabling this option is
reasonable for a model that is rarely serialised to JSON.
