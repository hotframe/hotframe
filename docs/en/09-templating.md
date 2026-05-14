# 9. The template engine (`templating/`)

> The `templating/` subsystem builds, configures, and maintains the Jinja2 environment that the
> entire application uses to render HTML — with automatic template directory discovery, framework
> globals, formatting filters, built-in i18n, and the slot system for injecting UI between modules.

---

## What this folder is for

`hotframe.templating` is the layer that separates "having Jinja2 installed" from "having Jinja2
production-ready in hotframe". Its concrete responsibilities are:

1. **Building the Jinja2 `Environment`** with all template directories correctly ordered —
   globals, apps, modules, and component roots.
2. **Registering tag extensions**: `{% component %}` and `{% live %}`, plus the Jinja2 standard
   library extensions (`i18n`, `do`, `loopcontrols`).
3. **Exposing globals and filters** that templates take for granted: `static`, `url_for`, `icon`,
   `render_slot`, `currency`, `dateformat`, etc.
4. **Injecting security context** automatically into every `TemplateResponse` (`csrf_token`,
   `csp_nonce`, `csrf_input`).
5. **Managing the slot system** — the `SlotRegistry` that lets third-party modules contribute UI
   fragments to extension points declared by other modules.
6. **Supporting hot-reload**: when a module is activated or deactivated, `refresh_template_dirs`
   rescans the directories without restarting the process.

---

## File map

| File | Responsibility |
|---|---|
| [`__init__.py`](../src/hotframe/templating/__init__.py) | Module docstring; re-exports public entry points |
| [`engine.py`](../src/hotframe/templating/engine.py) | `create_template_engine`, `_HotframeTemplates`, `refresh_template_dirs` |
| [`extensions.py`](../src/hotframe/templating/extensions.py) | `register_extensions` — globals (`static`, `url_for`, `icon`, `stat_card`, `render_slot`) and filters (`currency`, `dateformat`, `timesince`, `slugify`, …) |
| [`globals.py`](../src/hotframe/templating/globals.py) | `get_global_context` — per-request context injected by `@view` (user, csrf, csp, menu module items) |
| [`slots.py`](../src/hotframe/templating/slots.py) | `SlotEntry`, `SlotRegistry` — registration and resolution of slot content across modules |

---

## `engine.py` — The Jinja2 environment builder

### `_collect_template_dirs(modules_dir)`

Private function that builds the ordered list of directories the Jinja2 `FileSystemLoader` will
search. Order matters: the first matching template wins.

```
Search order:
1.  CWD/templates/                     ← project-wide globals
2.  apps/*/templates/                  ← each static app, sorted alphabetically
3.  modules/*/templates/               ← each active module, sorted alphabetically
4.  hotframe/components/               ← built-in framework component root
5.  apps/                              ← root for resolving <app>/components/<name>/template.html
6.  modules_dir/                       ← root for resolving <module_id>/components/<name>/template.html
```

Directories 4–6 are the "component roots". Component discovery registers paths such as
`shared/components/badge/template.html`; for Jinja2 to resolve them, it needs `apps/` in its
search path.

### `create_template_engine(modules_dir=None) -> Jinja2Templates`

The only public constructor. Creates the Jinja2 `Environment` with:

```python
env = Environment(
    loader=FileSystemLoader(template_dirs),
    autoescape=select_autoescape(["html", "xml"]),
    extensions=[
        "jinja2.ext.i18n",       # {% trans %}, _(), ngettext()
        "jinja2.ext.do",         # {% do expression %}
        "jinja2.ext.loopcontrols",  # {% break %}, {% continue %}
        ComponentExtension,      # {% component 'name' key=val %}...{% endcomponent %}
        LiveExtension,           # {% live 'name' prop=val %}
    ],
)
```

After building the `Environment`:

1. Calls `install_component_context_tracker(env)` to patch `env.context_class` and publish the
   active `Context` in a `ContextVar` (required so that `{% component %}` can access the framework
   slice inside a `CallBlock`).
2. Calls `register_extensions(env)` to install globals and filters.
3. Calls `register_component_globals(env)` to install the `render_component` function.
4. Installs `live_assets` as a global (the function that emits the `live.js` `<script>` tag).
5. Installs gettext translations via `env.install_gettext_translations(get_translations())`.

Returns an instance of `_HotframeTemplates` (not the raw `Environment`), ready to be used as
`app.state.templates` in FastAPI.

### `_HotframeTemplates` — Subclass of `Jinja2Templates`

```python
class _HotframeTemplates(Jinja2Templates):
    def TemplateResponse(self, request, name, context=None, **kwargs):
        ...
```

Overrides `TemplateResponse` to automatically inject the following into `context`:

| Variable | Source |
|---|---|
| `request` | The current request |
| `csrf_token` | `request.state.csrf_token` |
| `csrf_input` | Lambda that returns the `<input hidden>` containing the token |
| `csp_nonce` | `request.state.csp_nonce` |

This guarantees that any template rendered via `templates.TemplateResponse(...)` always has these
variables available, without the view code having to remember to include them.

### `refresh_template_dirs(templates, modules_dir)`

```python
def refresh_template_dirs(templates: Jinja2Templates, modules_dir: Path) -> None:
    template_dirs = _collect_template_dirs(modules_dir)
    templates.env.loader = FileSystemLoader(template_dirs)
```

Replaces the loader on the existing environment. The `Environment` itself is not recreated — all
previously registered extensions and globals are preserved. `ModuleRuntime` calls this function
after activating or deactivating a module so that the module's templates appear (or disappear)
without restarting the process.

---

## `extensions.py` — Globals and filters

### `register_extensions(env)`

Single entry point. Registers all global functions and filters in the `Environment` that hotframe
templates take as given.

#### Registered globals

| Template name | Python function | What it does |
|---|---|---|
| `static` | `static_url(path)` | Returns `/static/{path}` |
| `url_for` | `url_for_helper(name, **kwargs)` | Generates URLs to modules: `url_for('notes:index')` → `/m/notes/index/` |
| `icon` | `render_icon(name, size, css_class, **attrs)` | Generates Iconify markup (`<span class="iconify" data-icon="...">`) |
| `render_slot` | `render_slot_helper(slot_name, **context)` | Placeholder; the real implementation is provided from bootstrap |
| `currency` | `currency_filter(value, currency_code, language)` | Formats currency via Babel if available |
| `ngettext` | From the i18n middleware | Plural gettext |
| `get_current_language` | From the i18n middleware | Active language for the request |
| `csrf_input` | Empty lambda | Overwritten by `_HotframeTemplates` with the real token |
| `stat_card` | `stat_card_helper(value, label, icon, color)` | Generates the HTML for a dashboard tile |

#### Registered filters

| Filter | Signature | Behavior |
|---|---|---|
| `currency` | `value \| currency` | Same as the `currency` global |
| `dateformat` | `value \| dateformat('d/m/Y H:i')` | PHP-style date/time formatting |
| `timeformat` | `value \| timeformat('H:i')` | Time only, PHP-style tokens: `H`, `G`, `h`, `g`, `i`, `s`, `a` |
| `timesince` | `value \| timesince` | "2 hours", "3 days", etc. elapsed since `value` |
| `truncatewords` | `value \| truncatewords(10)` | Truncates to N words, appending `…` |
| `slugify` | `value \| slugify` | Normalizes unicode, lowercases, replaces spaces with `-` |

#### `render_icon` in detail

```python
def render_icon(name: str, size: int | None = None, css_class: str = "", **attrs: str) -> Markup:
```

Supports icon namespaces:

```jinja
{{ icon('ion:heart') }}
{{ icon('material:account', size=24, css_class='header-icon') }}
{{ icon('hero:check', aria_label='done') }}
```

The `_NAMESPACE_MAP` dict translates short prefixes to Iconify prefixes:

```python
_NAMESPACE_MAP = {
    "ion": "ion", "material": "mdi", "hero": "heroicons",
    "tabler": "tabler", "lucide": "lucide", "fa": "fa-solid",
}
```

Additional `kwargs` are converted to HTML attributes with hyphens (`aria_label` → `aria-label`).

#### `url_for_helper` in detail

```python
def url_for_helper(name: str, **kwargs: str) -> str:
```

The name can use `:` or `.` as a separator. `url_for('notes:detail', pk=42)` generates
`/m/notes/detail/42/`. Without a separator it returns `/{name}`. This convention follows FastAPI's
but is adapted to the `/m/<module_id>/` prefix used by dynamic modules.

---

## `globals.py` — Per-request global context

### `get_global_context(request) -> dict`

Async coroutine that builds the base context injected before every view render (`@view`). It does
not act directly on the `Environment` — the `@view` decorator calls this function and merges its
result with the context returned by the view function.

Variables it produces:

| Key | Type | Source |
|---|---|---|
| `request` | `Request` | The current HTTP request |
| `csp_nonce` | `str` | `request.state.csp_nonce` |
| `csp_trusted_types` | `bool` | `settings.CSP_TRUSTED_TYPES` |
| `csrf_token` | `str` | `request.state.csrf_token` |
| `csrf_input` | callable | Lambda that returns the `<input hidden>` |
| `debug` | `bool` | `app.state.debug` |
| `current_path` | `str` | `request.url.path` |
| `user` | model or `None` | Authenticated user; loaded from session if not already in `request.state` |
| `is_authenticated` | `bool` | `True` if there is an active user |
| `module_menu_items` | `list` | Menu items from active modules (via `module_registry.get_menu_items()`) |

#### Customizable context hook

If `settings.GLOBAL_CONTEXT_HOOK` is defined (dotted path to an async function), it is called
after the base context is populated. The function receives `request` and must return a `dict`. Its
result is merged into the context via `context.update(extra)`.

```python
# settings.py
GLOBAL_CONTEXT_HOOK = "apps.shared.context.add_branding_context"

# apps/shared/context.py
async def add_branding_context(request):
    return {"app_name": "Acme", "logo_url": "/static/logo.svg"}
```

#### `_load_user_from_session`

Private function that loads the user from the database if it was not already in `request.state`.
Uses `get_session_user_id(request)` to read the `user_id` from the session cookie, then executes a
`SELECT` with `is_active=True`. Writes the user back to `request.state.current_user` as a cache
for the rest of the request lifecycle.

---

## `slots.py` — The slot system

The slot system is hotframe's UI extension mechanism. It allows a module to inject content
(template fragments) into extension points defined by other modules, without any direct `import`
relationship between them.

### `SlotEntry`

```python
@dataclass(slots=True)
class SlotEntry:
    template: str
    priority: int = 10
    module_id: str | None = None
    context_fn: Callable | None = None
    condition_fn: Callable | None = None
```

Each entry represents a contribution to a specific slot. Key attributes:

- **`template`**: Jinja2 path to the fragment to render (e.g. `"loyalty/partials/widget.html"`).
- **`priority`**: Integer — entries with a lower number are rendered first (default 10).
- **`module_id`**: Enables automatic cleanup when the module is deactivated.
- **`condition_fn`**: Sync or async callable that returns `bool`. If it returns `False`, the entry
  is silently skipped. Receives `request` and the `**extra_context` from the call site.
- **`context_fn`**: Sync or async callable that returns a `dict`. Merged into the context before
  rendering the slot template. Useful for loading additional data (e.g. a customer's loyalty
  points balance from the database).

### `SlotRegistry`

App singleton, stored in `app.state.slots`. Internally maintains:

```python
self._slots: dict[str, list[SlotEntry]] = {}
```

#### `register(slot_name, template, *, priority, module_id, context_fn, condition_fn)`

```python
slots.register(
    "dashboard_widgets",
    template="loyalty/partials/widget.html",
    priority=5,
    module_id="loyalty",
    condition_fn=lambda request, **kw: request.user.has_loyalty_card,
)
```

Adds a `SlotEntry` to the slot's list. If the slot does not exist yet, it is created with
`setdefault`. There is no validation of slot names — any string is a valid slot name.

#### `get_entries(slot_name, request=None, **extra_context) -> list[tuple[SlotEntry, dict]]`

Async method. Internal process:

1. Retrieves the slot's entries and sorts them by `priority`.
2. For each entry, evaluates `condition_fn` (if present). If it returns `False` or raises an
   exception, the entry is skipped.
3. Evaluates `context_fn` (if present). The result is merged over `extra_context`.
4. Returns a list of `(entry, context_dict)` tuples.

Exceptions in `condition_fn` and `context_fn` are caught and logged (without crashing), keeping
the slot resilient against errors in third-party modules.

#### `unregister_module(module_id)`

```python
def unregister_module(self, module_id: str) -> None:
```

Removes all `SlotEntry` objects whose `module_id` matches. Then cleans up any slots that have
become empty. `ModuleRuntime` calls this function when deactivating a module — the UI stops
showing the module's fragments without any change to the host module's code.

#### Helper methods

| Method | Description |
|---|---|
| `has_content(slot_name)` | `True` if there is at least one entry for that slot |
| `list_slots()` | `dict[str, int]` — name → entry count; for diagnostics |
| `clear()` | Empties everything; used in tests |

#### Complete usage example

In the host app's template:

```jinja
{# apps/shared/templates/shared/dashboard.html #}
{% set widget_entries = slot_entries('dashboard_widgets') %}
{% for entry, ctx in widget_entries %}
  {% include entry.template with context %}
{% endfor %}
```

In the contributing module:

```python
# modules/loyalty/slots.py
def register_slots(slots, module_id):
    slots.register(
        slot="dashboard_widgets",
        template="loyalty/partials/points_widget.html",
        priority=5,
        module_id=module_id,
        context_fn=_load_points_summary,
        condition_fn=_user_has_card,
    )

async def _load_points_summary(request, **kw):
    return {"points": await PointsRepo.total_for(request.state.current_user.id)}

async def _user_has_card(request, **kw):
    return await LoyaltyRepo.has_card(request.state.current_user.id)
```

---

## How this fits with the rest of the framework

```
create_app()
  └─► create_template_engine()          ← templating/engine.py
        ├─ register_extensions(env)     ← templating/extensions.py
        ├─ register_component_globals() ← components/rendering.py
        ├─ install_component_context_tracker() ← components/jinja_ext.py
        └─ env.globals["live_assets"]   ← live/assets.py

@view decorator
  └─► get_global_context(request)       ← templating/globals.py
        └─ merges into TemplateResponse context

ModuleRuntime.activate(module_id)
  └─► refresh_template_dirs()           ← templating/engine.py
  └─► SlotRegistry.register(...)        ← templating/slots.py

ModuleRuntime.deactivate(module_id)
  └─► refresh_template_dirs()
  └─► SlotRegistry.unregister_module()
```

- **`live/`** uses the exact same `Environment` created here. The `LiveExtension` and the
  `live_assets` global are installed in `create_template_engine`.
- **`components/`** registers `render_component` in the same `env.globals` and uses the patched
  `context_class` to access the framework slice.
- **`engine/module_runtime.py`** calls `refresh_template_dirs` on every module activation and
  deactivation.
- **`middleware/csrf.py`** and **`middleware/csp.py`** write `csrf_token` and `csp_nonce` to
  `request.state`; `_HotframeTemplates.TemplateResponse` reads them and injects them into the
  context.

---

## Gotchas and design decisions

**1. One `Environment`, many directories.**
Rather than creating one `Environment` per module, hotframe uses a single one with all directories
in the `FileSystemLoader`. This maximizes reuse of Jinja2's compiled template cache. The downside
is that `refresh_template_dirs` must rebuild the entire loader, which partially invalidates the
cache. In practice this only happens when modules are activated or deactivated — a rare event in
production.

**2. Directory order as an override mechanism.**
The project app can override module templates by placing a file with the same name under
`CWD/templates/`. There is no explicit "app inheritance" system — the search order acts as an
implicit override. Make sure to document which templates in your module are intended to be
overridable.

**3. `render_slot_helper` is a placeholder.**
The function registered in `extensions.py` as `render_slot` returns an HTML comment. The real
implementation that renders fragments from the `SlotRegistry` is injected from the bootstrap. If
you see `<!-- slot:name -->` in the HTML output, there is a problem in the initialization.

**4. `context_fn` and `condition_fn` can be sync or async.**
`SlotRegistry.get_entries` uses `inspect.iscoroutinefunction` to decide whether to `await`.
This is convenient but has a limitation: lambdas and partial functions are not detected as
coroutines even if the underlying callable is one. Always use explicit `async def`.

**5. Name collisions in `url_for`.**
`url_for_helper` does not consult the FastAPI router — it generates URLs by convention
(`/m/<module_id>/<view_id>/`). If a module uses view names that do not follow this convention,
the generated URL will be incorrect. Modules mounted via `ModuleRuntime` do follow the convention.

**6. `csrf_input` is registered as an empty lambda in `register_extensions`.**
This is a safe default: if someone uses `{{ csrf_input() }}` in a template rendered outside the
normal cycle (e.g. in tests without a real request), it will not crash. `_HotframeTemplates`
overwrites this value with the real lambda in every `TemplateResponse`.

**7. Babel is optional.**
`currency_filter` attempts to import `babel.numbers.format_currency`. If Babel is not installed,
it falls back to `f"{value:.2f} {currency_code}"`. This is not a silently swallowed `ImportError`
— there is an explicit `try/except (ImportError, Exception)`.
