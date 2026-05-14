# Step-by-step study guide — hotframe 1.0

> This guide explains **what each piece of hotframe does and how it works**.
> It is not a *cheatsheet*: reading it in full should give you a complete
> mental model of the framework and the confidence to build an app from scratch.
>
> **Audience**: intermediate Python developer. Assumes Python 3.12+, async/await,
> basic FastAPI, and SQLAlchemy/ORM familiarity.

## Table of contents

1. [What is hotframe](#1-what-is-hotframe)
2. [Installation and first project](#2-installation-and-first-project)
3. [Project anatomy](#3-project-anatomy)
4. [Bootstrap: how the app starts up](#4-bootstrap-how-the-app-starts-up)
5. [Apps vs modules: the key concept](#5-apps-vs-modules-the-key-concept)
6. [HTML views: the `@view` decorator](#6-html-views-the-view-decorator)
7. [Stateless components](#7-stateless-components)
8. [LiveComponent: the reactive core](#8-livecomponent-the-reactive-core)
9. [The live runtime internals](#9-the-live-runtime-internals)
10. [Slots: cross-module UI injection](#10-slots-cross-module-ui-injection)
11. [Events, hooks, and the signal bus](#11-events-hooks-and-the-signal-bus)
12. [Persistence: models, repositories, protocols](#12-persistence-models-repositories-protocols)
13. [Migrations and the CLI](#13-migrations-and-the-cli)
14. [Settings: the single configuration file](#14-settings-the-single-configuration-file)
15. [Security: CSRF, CSP, sessions, rate limiting](#15-security-csrf-csp-sessions-rate-limiting)
16. [Writing a module from scratch](#16-writing-a-module-from-scratch)

---

## 1. What is hotframe

hotframe is a Python web framework that combines:

- **FastAPI** as the HTTP and WebSocket layer.
- **SQLAlchemy 2.0** async as the ORM.
- **Jinja2** as the template engine.
- A **Django-style API**: `hf` commands for everything, automatic app
  discovery, and a single `settings.py` as the source of truth for
  configuration.

On top of that foundation it adds two distinctive pieces:

1. **Hot-mount module engine** — install, activate, deactivate, and uninstall
   "modules" (self-contained Python packages) **at runtime, without restarting**
   the process. Each module contributes its own routes, models, migrations,
   events, and templates.
2. **`LiveComponent`** — reactive components where **state lives on the
   server**. The client only sends events over WebSocket; the server
   recomputes the HTML and sends back the patch. You write no JavaScript inside
   modules. The client-side runtime (`live.js` + `morphdom`) is bundled and
   served automatically.

The result: Django ergonomics + Phoenix LiveView-style reactivity, with no
HTMX, no Alpine, and no build step.

---

## 2. Installation and first project

```bash
pip install hotframe
hf startproject myapp
cd myapp
hf runserver
```

This gives you a server at `http://127.0.0.1:8000`. The `startproject` command
generates a minimal skeleton with an `apps/shared` app ready to go.

`hf` is the primary CLI (also available as `hotframe`). Run `hf --help` to
see all commands.

> **Deliberate design choice**: hotframe generates **less** boilerplate with
> `startproject` than Django does. Only the bare minimum needed to start;
> no empty "just in case" files.

---

## 3. Project anatomy

```
myapp/
├── asgi.py             ← entrypoint for uvicorn / gunicorn
├── main.py             ← same (alternative entrypoint)
├── manage.py           ← entrypoint for the hf CLI within the project
├── settings.py         ← SINGLE configuration file
├── pyproject.toml      ← project dependencies
├── apps/               ← static apps (compiled into the deploy)
│   └── shared/
│       ├── app.py      ← AppConfig
│       ├── routes.py   ← HTML routes (FastAPI router)
│       ├── api.py      ← REST routes (FastAPI api_router)
│       ├── models.py   ← SQLAlchemy models
│       ├── templates/  ← Jinja2 templates
│       ├── components/ ← components (stateless or LiveComponent)
│       └── migrations/ ← Alembic
├── modules/            ← dynamic modules (install/uninstall at runtime)
└── tests/
```

The two folders that matter most are `apps/` and `modules/`. The conceptual
difference between them is important.

---

## 4. Bootstrap: how the app starts up

`asgi.py` is trivial:

```python
from hotframe import create_app
from settings import settings

app = create_app(settings)
```

`create_app(settings)` runs, in order:

1. Configures logging and OpenTelemetry.
2. Builds the **middleware** stack (CSRF, CSP, sessions, rate limiting,
   compression, error boundary, i18n, modules).
3. Mounts CORS if `CORS_ORIGINS` is defined.
4. Creates the framework registries: `ComponentRegistry`,
   `SlotRegistry`, `AsyncEventBus`, `HookRegistry`, `BroadcastHub`.
5. Initializes the **SQLAlchemy engine** and connects the
   ORM→EventBus listeners.
6. Creates the Jinja2 engine and registers the `{% component %}`,
   `{% live %}` extensions and globals (`render_component`, `live_assets`,
   `icon`, etc.).
7. **Auto-discovers apps** under `apps/*/` and mounts their `routes.py`
   (HTML) and `api.py` (REST) routers.
8. Creates the **`ModuleRuntime`** and, during lifespan startup, mounts
   each active module from the database.
9. Creates the **`LiveRuntime`** and mounts the WebSocket endpoint
   `/ws/_live`.
10. Serves static files: `/static/...` (your app assets) and
    `/static/hotframe/...` (where `live.js` and `morphdom` live).

You never touch this flow. You only edit `settings.py` and `apps/`.

---

## 5. Apps vs modules: the key concept

This distinction is the backbone of hotframe. Once you understand it,
everything else falls into place.

### Apps (static)

- Live in `apps/<name>/`.
- Discovered at startup and mounted into the FastAPI application.
- **Cannot be activated or deactivated** without restarting the process.
- Intended for code that is a permanent part of the product: auth,
  shared layout, public pages.

### Modules (dynamic)

- Live in `modules/<id>/`.
- Have a `module.py` containing a `ModuleConfig` that declares name,
  version, dependencies, whether it has views or only an API, and so on.
- Their **state** (installed / active / inactive) lives in a `module`
  database table.
- The **`ModuleRuntime`** orchestrates the lifecycle at runtime:
  - `install(module_id)` — copies files, registers in the DB.
  - `activate(module_id)` — imports the package, mounts routes under
    `/m/<id>/`, registers events/hooks/slots, runs migrations.
  - `deactivate(module_id)` — unmounts routes, clears `sys.modules`,
    frees memory.
  - `uninstall(module_id)` — removes files.
  - `update(module_id, source=...)` — installs a new version with
    automatic backup and rollback on failure.

This is what enables a plugin marketplace where you install and uninstall
plugins without stopping the process. The libraries that inspired this
(Odoo, WordPress) do it in much older and heavier frameworks; hotframe
does it in Python 3.12 + FastAPI with roughly 1,500 lines of orchestration.

### When to use each

| Scenario | App or module |
|---|---|
| Base template, login, shared layout | App |
| Optional, sellable/installable feature | Module |
| Code that changes rapidly during development | App (faster iteration) |
| A plugin specific to one customer | Module |

---

## 6. HTML views: the `@view` decorator

A typical HTML route:

```python
# apps/shared/routes.py
from fastapi import APIRouter, Request
from hotframe import view

router = APIRouter()

@router.get("/dashboard")
@view(module_id="shared", view_id="dashboard", permissions="dashboard.view")
async def dashboard(request: Request):
    return {"items": await load_items()}
```

`@view` handles four concerns in a single decorator:

1. **Auth**: if `login_required=True` (the default), redirects to `/login`
   when there is no active session.
2. **Permissions**: checks the list against the resolver configured in
   `settings.PERMISSION_RESOLVER`.
3. **Template auto-discovery**: looks up the template by convention at
   `{module_id}/pages/{view_id}.html`. Your view only returns a dict
   containing the template context.
4. **Render**: merges the dict with a global context (request, csrf_token,
   csp_nonce, user) and returns a `TemplateResponse`.

The `htmx_view` alias exists for historical naming symmetry, but it does
exactly the same thing: a single full HTML page. Reactivity comes through
LiveComponent, not through request branching.

---

## 7. Stateless components

Before explaining `LiveComponent`, it is worth looking at the simple case.
A stateless component is a reusable UI fragment with **no server state**.

### Directory layout

```
apps/shared/components/badge/
├── template.html
├── component.py    ← (optional) defines props with Pydantic
└── routes.py       ← (optional) component-level endpoints
```

### `component.py` (optional)

```python
from hotframe.components import Component

class BadgeProps(Component):
    text: str
    variant: str = "default"
```

### `template.html`

```jinja
<span class="badge badge-{{ variant }}">{{ text }}</span>
```

### Using a component from another template

```jinja
{# Compact form, no body #}
{{ render_component('badge', text='New', variant='primary') }}

{# Block form, body accessible as {{ body }} inside the component #}
{% component 'alert' type='warning' %}
  Stock is low
{% endcomponent %}
```

The `ComponentRegistry` discovers components at startup (for apps) or when
the module is activated (for modules). Rendering isolates the context:
the component sees only its own props plus a *framework slice*
(`request`, `csrf_token`, `csp_nonce`, `user`, `current_path`).
Variables from the parent template are not accidentally inherited.

---

## 8. LiveComponent: the reactive core

This is the key innovation. Forget HTMX, Alpine, Datastar, React hooks.
The model is:

> **State lives on the server.** The browser is a terminal that
> sends events and applies HTML patches.

### Complete example: a TODO list

```python
# modules/todo/components/todo_list/component.py
from hotframe.live import LiveComponent, event
from modules.todo.models import Todo

class TodoList(LiveComponent):
    user_id: int             # prop (immutable)
    items: list = []         # state (mutable)
    new_text: str = ""

    async def on_mount(self) -> None:
        self.items = await Todo.where(user_id=self.user_id).all()

    @event("toggle")
    async def toggle(self, todo_id: str) -> None:
        t = next(t for t in self.items if str(t.id) == todo_id)
        t.done = not t.done
        await t.save()

    @event("add")
    async def add(self) -> None:
        if not self.new_text.strip():
            return
        await Todo.create(user_id=self.user_id, text=self.new_text)
        self.items = await Todo.where(user_id=self.user_id).all()
        self.new_text = ""
```

```jinja
{# modules/todo/components/todo_list/template.html #}
<ul>
{% for todo in items %}
  <li>
    <input type="checkbox" {% if todo.done %}checked{% endif %}
           data-on:click="toggle:{{ todo.id }}">
    {{ todo.text }}
  </li>
{% endfor %}
</ul>
<form data-on:submit="add">
  <input data-bind="new_text">
  <button type="submit">Add</button>
</form>
```

### Embedding in a page (cold load)

```jinja
{% extends "shared/base.html" %}
{% block head %}
  {{ live_assets() }}    {# loads live.js + morphdom #}
{% endblock %}
{% block body %}
  {% live "todo_list" user_id=user.id %}
{% endblock %}
```

### The four conventions to remember

| Convention | What it does |
|---|---|
| `prop: int` | Pydantic field with no default; passed via the template tag prop |
| `state: list = []` | Pydantic field with a default; mutable inside event handlers |
| `@event("name")` | Marks an `async def` method as the handler for event `"name"` |
| `data-on:click="name:payload"` | The client serializes this attribute and sends the event |

`data-bind="field"` is two-way binding: the client debounces the input value
and sends it to the server, which updates the state field without re-rendering.
The next event (typically `submit`) sees the updated state and triggers a render.

---

## 9. The live runtime internals

Understanding this is worthwhile because it explains why the model works.

### Runtime components

```
hotframe/live/
├── base.py          ← LiveComponent class (Pydantic + state + lifecycle)
├── decorators.py    ← @event(name)
├── protocol.py      ← TypedDicts for the wire format
├── diff.py          ← template rendering + wrapping with data-hf-cid
├── session.py       ← LiveSession (a {cid: instance} dict per WebSocket)
├── runtime.py       ← LiveRuntime (app singleton, owns the sessions)
├── ws.py            ← /ws/_live endpoint
├── jinja_ext.py     ← {% live %} tag
├── assets.py        ← {{ live_assets() }} global
└── static/
    ├── live.js      ← client: WS + event capture + patch application
    └── morphdom.min.js
```

### Lifecycle of a single interaction

1. **Cold load**. The user navigates to a page. Jinja processes
   `{% live "todo_list" user_id=42 %}`:
   - Generates a short UUID `cid`: `c-7a3f4d2b91`.
   - Instantiates `TodoList(user_id=42)`.
   - Runs `on_mount()` synchronously (in a new event loop if needed).
   - Renders the template with state already populated.
   - Wraps the HTML in `<div data-hf-cid="c-7a3f4d2b91"
     data-hf-component="todo_list" data-hf-props='{"user_id":42}'>...</div>`.

2. **Client load**. The browser downloads `live.js` and `morphdom.min.js`
   from `/static/hotframe/`. `live.js` fires on `DOMContentLoaded`, opens
   a WebSocket to `/ws/_live`, and sends an `attach` message for every
   `[data-hf-cid]` it finds in the DOM:

   ```json
   {"t":"attach","cid":"c-7a3f4d2b91","name":"todo_list","props":{"user_id":42}}
   ```

3. **Server attach**. The `LiveSession` handles the message:
   - Looks up `todo_list` in the `ComponentRegistry`.
   - Instantiates `TodoList(user_id=42)` again (the server does not keep
     the cold-load instance — this is intentional, so a reconnect works
     without stale state).
   - Assigns the same `cid`.
   - Runs `on_mount()` (yes, twice — it is cheap and simplifies the model).
   - Re-renders and sends a `patch`:

   ```json
   {"t":"patch","cid":"c-7a3f4d2b91","html":"<ul>...</ul>"}
   ```

4. **Client patch**. `live.js` finds `[data-hf-cid="c-..."]` in the DOM
   and applies `morphdom` with the new HTML. Morphdom preserves focus,
   selection, and scroll position for elements that did not change.
   Because the HTML is identical to the cold-load output, no visible
   change occurs in practice.

5. **User event**. Click on a checkbox with `data-on:click="toggle:5"`.
   `live.js` captures it and sends:

   ```json
   {"t":"event","cid":"c-7a3f4d2b91","n":"toggle","p":"5"}
   ```

6. **Server event**. `LiveSession` dispatches the handler:
   - `instance.__class__._events["toggle"]` → the method decorated with
     `@event("toggle")`.
   - Invokes it: `await handler(instance, "5")`.
   - After it returns, re-renders and sends another `patch`.

7. **`data-bind`**. When the user types in `<input data-bind="new_text">`,
   the client waits 250 ms (debounce) and sends:

   ```json
   {"t":"bind","cid":"c-7a3f4d2b91","f":"new_text","v":"Buy mil"}
   ```

   The server updates `instance.new_text` (with Pydantic validation)
   but **does not re-render**. This avoids clobbering what the user is
   actively typing. The next event (typically `submit`) sees the updated
   state and triggers a render.

### Complete wire protocol

| Type | Direction | Meaning |
|---|---|---|
| `attach` | C→S | Registers an instance under `cid` with its props |
| `event` | C→S | Invokes an `@event` handler with an optional payload |
| `bind` | C→S | Updates a state field without triggering a render |
| `detach` | C→S | Disposes an instance (cleanup) |
| `patch` | S→C | New HTML for the component; morphdom applies it |
| `nav` | S→C | `window.location.href = url` |
| `err` | S→C | Handler error; the client logs it |
| `toast` | S→C | Notification; the client decides how to display it |

Plain JSON with a single `t` discriminator. No versioning in v1.0;
if evolution is needed, add `v: 2`.

### Guarantees and caveats

- **One instance per (WS session, cid)**. Two open tabs = two distinct
  WS sessions = two independent instances.
- **Implicit sticky sessions**: state lives in process RAM. For
  multi-instance deployments, `LiveSession.components` must be migrated
  to Redis. No Redis backend is included today.
- **Reconnection**: if the WebSocket drops, the client reopens it with
  exponential backoff. Each `[data-hf-cid]` visible in the DOM
  re-attaches. Because `on_mount` runs again, **state must be
  reconstructible from `props` + the database**. Do not store live
  asyncio tasks in `self`.
- **Atomic handler per instance**: each handler runs under an
  `asyncio.Lock` per `cid`, so there is never a race between two events
  on the same instance. Different components do run in parallel.

---

## 10. Slots: cross-module UI injection

Distinct from components. Slots are **extension points** where third-party
modules can inject UI.

```python
# apps/shared/templates/shared/dashboard.html
{% for entry in slot_entries('dashboard_widgets') %}
  {% include entry.template with context %}
{% endfor %}
```

```python
# modules/loyalty/slots.py
def register_slots(slots, module_id):
    slots.register(
        slot="dashboard_widgets",
        template="loyalty/partials/widget.html",
        priority=5,           # higher = appears earlier
        condition_fn=...,     # optional function that decides whether to render
    )
```

When the `loyalty` module is deactivated, the `SlotRegistry` automatically
unregisters its contributions. The dashboard page simply stops showing the
widget. **Zero coupling**.

---

## 11. Events, hooks, and the signal bus

Three complementary mechanisms:

### `AsyncEventBus` — async pub/sub

```python
# Subscribe
@bus.on("invoice.paid")
async def send_email(event):
    await mailer.send(event.invoice_id)

# Emit
await bus.emit("invoice.paid", invoice_id=42)
```

Used for loosely-coupled integrations. Supports wildcards (`invoice.*`),
priorities, and a `FakeEventBus` for testing.

### `HookRegistry` — filters and actions (WordPress-style)

```python
# Filter a value as it passes through
hooks.add_filter("invoice.subtotal", lambda total, ctx: total * 1.1)

# Trigger an action
await hooks.do_action("invoice.created", invoice=inv)
```

Conceptually similar to the EventBus but with two differences: filters
**transform** a value; actions are fire-and-forget.

### Typed events

```python
from hotframe import BaseEvent, register_event
from pydantic import BaseModel

@register_event
class InvoicePaid(BaseEvent):
    invoice_id: int
    amount: float

await bus.emit_typed(InvoicePaid(invoice_id=42, amount=99.0))
```

Gives you autocomplete and validation. Recommended for stable domain events.

### ORM events

`setup_orm_events()` (called by `create_app` on your behalf) registers
listeners on SQLAlchemy. Every `INSERT`/`UPDATE`/`DELETE` automatically
emits events such as `models.User.created` or `models.Invoice.updated`.
You write nothing — just subscribe if you care about the event.

---

## 12. Persistence: models, repositories, protocols

### Models

```python
# apps/shared/models.py
from hotframe import Base, Model, TimeStampedModel
from sqlalchemy.orm import Mapped, mapped_column

class User(TimeStampedModel):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True)
```

Available mixins: `TimestampMixin` (`created_at`, `updated_at`),
`SoftDeleteMixin` (`deleted_at`), `AuditMixin` (`created_by_id`,
`updated_by_id`), `HubMixin` (multi-tenant `hub_id` with automatic filtering).

### `BaseRepository[T]` — typed generic CRUD

```python
from hotframe import BaseRepository

class UserRepo(BaseRepository[User]):
    model = User

# Usage
repo = UserRepo(db)
user = await repo.get(42)
users = await repo.list(filters={"is_active": True})
await repo.create(email="x@y.com")
await repo.update(user, email="new@y.com")
await repo.delete(user)
```

### Protocols (`ISession`, `IQueryBuilder`, `IRepository`)

To avoid coupling your code directly to SQLAlchemy, protocols define the
interfaces:

```python
from hotframe import DbSession  # = Annotated[ISession, Depends(get_db)]

@router.get("/items")
async def items(db: DbSession):
    # db is ISession, not AsyncSession
    ...
```

This lets you write tests with a fake `ISession` without spinning up
SQLAlchemy. The real implementation is always `AsyncSession`.

---

## 13. Migrations and the CLI

### `hf` commands

```bash
hf startproject myapp           # create a project
hf startapp blog                # create an app
hf startmodule shop             # create a module
hf startmodule shop --api-only  # API-only module
hf startmodule shop --system    # system module (cannot be uninstalled)

hf runserver                    # uvicorn with auto-reload
hf shell                        # interactive REPL (IPython if available)
hf migrate                      # alembic upgrade head
hf makemigrations <app>         # alembic revision --autogenerate

hf modules list                 # list modules + status
hf modules install <source>     # install (name, .zip, URL, marketplace)
hf modules update <source>      # update with backup + rollback
hf modules activate <name>
hf modules deactivate <name>
hf modules uninstall <name>

hf version
```

### Migrations

Each app and each module has its own `migrations/` directory with an
`env.py` and `versions/`. `hf migrate` runs all pending revisions in order.

`hf makemigrations <app>` auto-generates the next revision from detected
model changes. Conflicts are resolved manually (this is standard Alembic
— the framework adds no magic that will break later).

---

## 14. Settings: the single configuration file

```python
# settings.py
from hotframe import HotframeSettings

class Settings(HotframeSettings):
    APP_TITLE = "My App"
    DATABASE_URL = "postgresql+asyncpg://user:pass@localhost/myapp"
    AUTH_USER_MODEL = "apps.shared.models.User"
    AUTH_LOGIN_URL = "/login"

settings = Settings()
```

`HotframeSettings` is a Pydantic Settings class that reads values from:
1. Constructor arguments.
2. Environment variables.
3. A `.env` file in the current working directory.
4. Defaults defined in `HotframeSettings`.

The settings groups you will use most often:

- **Core**: `DATABASE_URL`, `SECRET_KEY`, `DEBUG`, `APP_TITLE`,
  `LOG_LEVEL`.
- **Auth**: `AUTH_USER_MODEL`, `AUTH_LOGIN_URL`, `PERMISSION_RESOLVER`.
- **Session**: `SESSION_COOKIE_NAME`, `SESSION_MAX_AGE`.
- **Security**: `CSP_ENFORCE`, `CSP_TRUSTED_TYPES`, `CSP_ALLOWED_SOURCES`.
- **Rate limiting**: `RATE_LIMIT_API`, `RATE_LIMIT_AUTH`,
  `RATE_LIMIT_AUTH_PREFIXES`.
- **DB pool**: `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_RECYCLE`,
  `DB_ECHO`.
- **Static / media**: `STATIC_ROOT`, `STATIC_URL`, `MEDIA_ROOT`,
  `MEDIA_URL`, `MEDIA_STORAGE`.
- **Middleware**: `MIDDLEWARE` (list of dotted paths). You almost never
  touch this; the default covers everything.
- **Modules**: `MODULES_DIR`, `MODULE_SOURCE`,
  `MODULE_MARKETPLACE_URL`.
- **Locale**: `LANGUAGE`, `CURRENCY`.
- **CORS**: `CORS_ORIGINS`, `CORS_METHODS`, `CORS_HEADERS`.

> **There is no `INSTALLED_APPS`** like in Django. Apps are discovered by
> scanning `apps/`. If you need to restrict what is mounted, use
> `INSTALLED_APPS` in settings (it is optional).

---

## 15. Security: CSRF, CSP, sessions, rate limiting

Everything is active by default. You do not write any middleware by hand.

- **CSRF**: every `TemplateResponse` receives `csrf_token` and
  `csrf_input()` injected automatically. POST/PUT/PATCH/DELETE require the
  token or the `X-CSRF-Token` header. Routes matching `CSRF_EXEMPT_PREFIXES`
  (`/api/`, `/health`, `/static/`) are exempt.
- **CSP**: every response carries a dynamic nonce. Templates receive
  `csp_nonce` for use in `<script nonce="...">`. Adjust
  `CSP_ALLOWED_SOURCES` if you add external CDNs.
- **Sessions**: cookie signed with `itsdangerous`. The session backend is
  configurable (memory / Redis / DB — the default implementation uses a
  signed cookie).
- **Rate limiting**: three buckets — `api`, `view` (routes under `/m/`),
  and `auth` (routes matching `RATE_LIMIT_AUTH_PREFIXES`). Per IP, 60-second
  window.

---

## 16. Writing a module from scratch

Say you want a `notes` module with full CRUD.

### 1. Scaffolding

```bash
hf startmodule notes
```

Creates:

```
modules/notes/
├── module.py          ← ModuleConfig
├── models.py
├── routes.py          ← HTML routes
├── api.py             ← REST routes
├── templates/notes/
├── components/
└── migrations/
```

### 2. `module.py`

```python
from hotframe import ModuleConfig

class NotesModule(ModuleConfig):
    name = "notes"
    verbose_name = "Notes"
    version = "1.0.0"
    is_system = False
    has_views = True
    has_api = True
    requires_restart = False
    dependencies = []

    async def ready(self):
        pass

    async def install(self, ctx):
        pass

    async def uninstall(self, ctx):
        pass
```

### 3. Model

```python
# modules/notes/models.py
from hotframe import Base, TimeStampedModel
from sqlalchemy.orm import Mapped, mapped_column

class Note(TimeStampedModel):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str]
```

### 4. Migration

```bash
hf makemigrations notes
hf migrate
```

### 5. LiveComponent

```python
# modules/notes/components/note_list/component.py
from hotframe.live import LiveComponent, event
from modules.notes.models import Note

class NoteList(LiveComponent):
    items: list = []
    new_text: str = ""

    async def on_mount(self):
        self.items = await Note.all()

    @event("add")
    async def add(self):
        if self.new_text:
            await Note.create(text=self.new_text)
            self.items = await Note.all()
            self.new_text = ""
```

```jinja
{# modules/notes/components/note_list/template.html #}
<ul>
{% for note in items %}<li>{{ note.text }}</li>{% endfor %}
</ul>
<form data-on:submit="add">
  <input data-bind="new_text">
  <button>Add</button>
</form>
```

### 6. View that mounts the component

```python
# modules/notes/routes.py
from fastapi import APIRouter, Request
from hotframe import view

router = APIRouter()

@router.get("/")
@view(module_id="notes", view_id="index")
async def index(request: Request):
    return {}
```

```jinja
{# modules/notes/templates/notes/pages/index.html #}
{% extends "shared/base.html" %}
{% block body %}
  {% live "note_list" %}
{% endblock %}
```

### 7. Activate

```bash
hf modules install notes
hf modules activate notes
```

That is all. Without restarting the server, the routes under `/m/notes/` are
live and the LiveComponent works over WebSocket.

---

## Next steps

- Read the code of a reference module (once the demo repository is available).
- Read `src/hotframe/live/` to see how the runtime is implemented.
- Read `src/hotframe/engine/module_runtime.py` to understand the
  module lifecycle.
- Watch the repository issues: https://github.com/hotframe/hotframe.

If you get stuck, open an issue with a minimal reproducible case.
The project philosophy is that **the framework should never surprise you
in a bad way**.
