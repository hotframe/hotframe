# Guía de estudio paso a paso — hotframe 1.0

> Esta guía explica **qué hace y cómo funciona** cada pieza de hotframe.
> No es un *cheatsheet*: leyéndola entera deberías entender el modelo
> mental completo del framework y poder construir una app desde cero.
>
> **Audiencia**: dev Python intermedio. Asumo Python 3.12+, async/await,
> FastAPI básico y SQLAlchemy/ORM en general.

## Índice

1. [Qué es hotframe](#1-qué-es-hotframe)
2. [Instalación y primer proyecto](#2-instalación-y-primer-proyecto)
3. [Anatomía de un proyecto](#3-anatomía-de-un-proyecto)
4. [Bootstrap: cómo arranca la app](#4-bootstrap-cómo-arranca-la-app)
5. [Apps vs módulos: la pieza clave](#5-apps-vs-módulos-la-pieza-clave)
6. [Vistas HTML: el decorador `@view`](#6-vistas-html-el-decorador-view)
7. [Componentes stateless](#7-componentes-stateless)
8. [LiveComponent: el corazón reactivo](#8-livecomponent-el-corazón-reactivo)
9. [El runtime live por dentro](#9-el-runtime-live-por-dentro)
10. [Slots: inyección entre módulos](#10-slots-inyección-entre-módulos)
11. [Eventos, hooks y bus de señales](#11-eventos-hooks-y-bus-de-señales)
12. [Persistencia: modelos, repositorios, protocolos](#12-persistencia-modelos-repositorios-protocolos)
13. [Migraciones y CLI](#13-migraciones-y-cli)
14. [Settings: el único archivo de configuración](#14-settings-el-único-archivo-de-configuración)
15. [Seguridad: CSRF, CSP, sesiones, rate limit](#15-seguridad-csrf-csp-sesiones-rate-limit)
16. [Cómo escribir un módulo desde cero](#16-cómo-escribir-un-módulo-desde-cero)

---

## 1. Qué es hotframe

hotframe es un framework web Python que combina:

- **FastAPI** como capa HTTP y WebSocket.
- **SQLAlchemy 2.0** asíncrono como ORM.
- **Jinja2** como motor de plantillas.
- Una **API tipo Django**: comandos `hf` para todo, descubrimiento automático
  de apps, un único `settings.py` como fuente de configuración.

Encima añade dos piezas que lo distinguen:

1. **Hot-mount module engine** — instalar, activar, desactivar y desinstalar
   "módulos" (paquetes Python autocontenidos) **en runtime, sin reiniciar**
   el proceso. Cada módulo aporta sus propias rutas, modelos, migraciones,
   eventos y plantillas.
2. **`LiveComponent`** — componentes reactivos donde el **estado vive en el
   servidor**. El cliente sólo manda eventos por WebSocket; el server
   recalcula el HTML y manda el parche. No escribes JavaScript en los
   módulos. El cliente (`live.js` + `morphdom`) viene incluido y se sirve
   automáticamente.

El resultado: ergonomía Django + reactividad estilo Phoenix LiveView, sin
HTMX, sin Alpine, sin paso de build.

---

## 2. Instalación y primer proyecto

```bash
pip install hotframe
hf startproject myapp
cd myapp
hf runserver
```

Eso te da un servidor en `http://127.0.0.1:8000`. El comando `startproject`
genera un esqueleto mínimo con un `apps/shared` ya listo.

`hf` es el CLI principal (también disponible como `hotframe`). Con
`hf --help` ves todos los comandos.

> **Decisión deliberada**: hotframe genera **menos** código en
> `startproject` que Django. Solo lo imprescindible para arrancar; sin
> archivos vacíos "por si acaso".

---

## 3. Anatomía de un proyecto

```
myapp/
├── asgi.py             ← entrypoint para uvicorn / gunicorn
├── main.py             ← idem (alternativo)
├── manage.py           ← entrypoint del CLI hf dentro del proyecto
├── settings.py         ← ÚNICO archivo de configuración
├── pyproject.toml      ← deps del proyecto
├── apps/               ← apps estáticas (compiladas en el deploy)
│   └── shared/
│       ├── app.py      ← AppConfig
│       ├── routes.py   ← rutas HTML (router FastAPI)
│       ├── api.py      ← rutas REST (api_router FastAPI)
│       ├── models.py   ← modelos SQLAlchemy
│       ├── templates/  ← plantillas Jinja2
│       ├── components/ ← componentes (stateless o LiveComponent)
│       └── migrations/ ← Alembic
├── modules/            ← módulos dinámicos (instalan/desinstalan en runtime)
└── tests/
```

Las dos carpetas relevantes son `apps/` y `modules/`. La diferencia
conceptual es importante.

---

## 4. Bootstrap: cómo arranca la app

`asgi.py` es trivial:

```python
from hotframe import create_app
from settings import settings

app = create_app(settings)
```

`create_app(settings)` hace, en orden:

1. Configura logging y telemetría OpenTelemetry.
2. Construye la pila de **middleware** (CSRF, CSP, sesiones, rate limit,
   compresión, error boundary, i18n, módulos).
3. Monta CORS si `CORS_ORIGINS` está definido.
4. Crea las registries del framework: `ComponentRegistry`,
   `SlotRegistry`, `AsyncEventBus`, `HookRegistry`, `BroadcastHub`.
5. Inicializa el **engine SQLAlchemy** y conecta los listeners
   ORM→EventBus.
6. Crea el motor Jinja2 y registra las extensiones `{% component %}`,
   `{% live %}`, los globals (`render_component`, `live_assets`,
   `icon`, etc.).
7. **Auto-descubre apps** en `apps/*/` y monta sus `routes.py` (HTML)
   y `api.py` (REST).
8. Crea el **`ModuleRuntime`** y, en el lifespan startup, monta cada
   módulo activo en la base de datos.
9. Crea el **`LiveRuntime`** y monta el endpoint WebSocket
   `/ws/_live`.
10. Sirve estáticos: `/static/...` (de tu app) y `/static/hotframe/...`
    (donde viven `live.js` y `morphdom`).

El usuario nunca toca este flujo. Sólo edita `settings.py` y `apps/`.

---

## 5. Apps vs módulos: la pieza clave

Esta distinción es la columna vertebral de hotframe. Si la entiendes,
todo lo demás cae en su sitio.

### Apps (estáticas)

- Viven en `apps/<nombre>/`.
- Se descubren al arrancar y se montan en el FastAPI app.
- **No se pueden activar/desactivar** sin reiniciar el proceso.
- Pensadas para código que es parte permanente del producto: auth,
  shared layout, página pública.

### Módulos (dinámicos)

- Viven en `modules/<id>/`.
- Tienen un `module.py` con un `ModuleConfig` que declara nombre,
  versión, dependencias, si usa vistas o sólo API, etc.
- Su **estado** (instalado / activo / inactivo) vive en una tabla
  `module` de la base de datos.
- El **`ModuleRuntime`** orquesta su ciclo de vida en runtime:
  - `install(module_id)` — copia archivos, registra en DB.
  - `activate(module_id)` — importa el paquete, monta rutas en
    `/m/<id>/`, registra eventos/hooks/slots, ejecuta migraciones.
  - `deactivate(module_id)` — desmonta rutas, limpia `sys.modules`,
    libera memoria.
  - `uninstall(module_id)` — elimina archivos.
  - `update(module_id, source=...)` — instala una nueva versión con
    backup y rollback automático en caso de fallo.

Esto es lo que permite tener un marketplace de plugins instalables sin
parar el proceso. La librería que esto inspira (Odoo, WordPress) lo hace
en frameworks bastante más viejos y pesados; hotframe lo hace en Python
3.12 + FastAPI con apenas 1500 líneas de orquestación.

### Cuándo cada uno

| Caso | App o módulo |
|---|---|
| Plantilla base, login, layout compartido | App |
| Funcionalidad opcional vendible/instalable | Módulo |
| Código que cambia rápido en desarrollo | App (más rápido) |
| Plugin de un cliente concreto | Módulo |

---

## 6. Vistas HTML: el decorador `@view`

Una ruta HTML típica:

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

`@view` resuelve cuatro cosas en un decorador:

1. **Auth**: si `login_required=True` (default), redirige a `/login` si
   no hay sesión.
2. **Permisos**: comprueba la lista contra el resolver configurado en
   `settings.PERMISSION_RESOLVER`.
3. **Auto-discovery del template**: busca por convención
   `{module_id}/pages/{view_id}.html`. Tu vista solo devuelve un dict
   con el contexto.
4. **Render**: pasa el dict + un contexto global (request, csrf_token,
   csp_nonce, user) al template y devuelve un `TemplateResponse`.

El alias `htmx_view` existe por simetría histórica de naming, pero hace
exactamente lo mismo: una sola página HTML completa. La reactividad va
por LiveComponent, no por bifurcaciones del request.

---

## 7. Componentes stateless

Antes de explicar `LiveComponent`, conviene ver el caso simple. Un
componente stateless es un trozo de UI reutilizable, **sin estado
servidor**.

### Estructura en disco

```
apps/shared/components/badge/
├── template.html
├── component.py    ← (opcional) define props con Pydantic
└── routes.py       ← (opcional) endpoints del componente
```

### `component.py` (opcional)

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

### Uso desde otra plantilla

```jinja
{# Forma compacta, sin body #}
{{ render_component('badge', text='New', variant='primary') }}

{# Forma con body, accesible como {{ body }} dentro del componente #}
{% component 'alert' type='warning' %}
  Stock is low
{% endcomponent %}
```

El `ComponentRegistry` los descubre al arrancar (apps) o al activar el
módulo (módulos). El render aísla el contexto: el componente solo ve
sus props + un *framework slice* (`request`, `csrf_token`, `csp_nonce`,
`user`, `current_path`). No hereda variables del padre por accidente.

---

## 8. LiveComponent: el corazón reactivo

Aquí está la innovación. Olvida HTMX, Alpine, Datastar, hooks de React.
El modelo es:

> **El estado vive en el servidor.** El navegador es un terminal que
> manda eventos y aplica parches HTML.

### Ejemplo completo: lista de TODOs

```python
# modules/todo/components/todo_list/component.py
from hotframe.live import LiveComponent, event
from modules.todo.models import Todo

class TodoList(LiveComponent):
    user_id: int             # prop (inmutable)
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

### Cold-load desde una página

```jinja
{% extends "shared/base.html" %}
{% block head %}
  {{ live_assets() }}    {# carga live.js + morphdom #}
{% endblock %}
{% block body %}
  {% live "todo_list" user_id=user.id %}
{% endblock %}
```

### Las cuatro convenciones que tienes que recordar

| Convención | Qué hace |
|---|---|
| `prop: int` | Pydantic field sin default, pasa por la prop del template |
| `state: list = []` | Pydantic field con default, mutable en handlers |
| `@event("name")` | Marca un método `async def` como handler del evento `"name"` |
| `data-on:click="name:payload"` | El cliente serializa este atributo y manda el evento |

El `data-bind="campo"` es atadura bidireccional: el cliente manda con
debounce el valor del input al server, que actualiza el campo del state
sin re-renderizar todavía. El siguiente evento sí dispara el render.

---

## 9. El runtime live por dentro

Vale la pena entenderlo porque es lo que hace que el modelo funcione.

### Componentes del runtime

```
hotframe/live/
├── base.py          ← clase LiveComponent (Pydantic + state + lifecycle)
├── decorators.py    ← @event(name)
├── protocol.py      ← TypedDicts del wire format
├── diff.py          ← render del template + envoltura con data-hf-cid
├── session.py       ← LiveSession (un dict {cid: instance} por WebSocket)
├── runtime.py       ← LiveRuntime (singleton de la app, dueña de las sesiones)
├── ws.py            ← endpoint /ws/_live
├── jinja_ext.py     ← {% live %} tag
├── assets.py        ← {{ live_assets() }} global
└── static/
    ├── live.js      ← cliente: WS + capturar eventos + aplicar parches
    └── morphdom.min.js
```

### Flujo de una interacción

1. **Cold load**. Usuario navega a una página. Jinja procesa
   `{% live "todo_list" user_id=42 %}`:
   - Genera un `cid` UUID corto: `c-7a3f4d2b91`.
   - Instancia `TodoList(user_id=42)`.
   - Ejecuta `on_mount()` síncronamente (loop nuevo si hace falta).
   - Renderiza el template con el state ya poblado.
   - Envuelve el HTML en `<div data-hf-cid="c-7a3f4d2b91"
     data-hf-component="todo_list" data-hf-props='{"user_id":42}'>...</div>`.

2. **Carga del cliente**. El navegador descarga `live.js` y
   `morphdom.min.js` desde `/static/hotframe/`. `live.js` arranca al
   `DOMContentLoaded`, abre un WebSocket a `/ws/_live`, y manda un
   mensaje `attach` por cada `[data-hf-cid]` que encuentra:

   ```json
   {"t":"attach","cid":"c-7a3f4d2b91","name":"todo_list","props":{"user_id":42}}
   ```

3. **Server attach**. El `LiveSession` recibe el mensaje:
   - Busca `todo_list` en el `ComponentRegistry`.
   - Instancia `TodoList(user_id=42)` de nuevo (el server no conserva
     la instancia del cold-load — es deliberado, así una reconexión
     funciona sin estado fantasma).
   - Le asigna el mismo `cid`.
   - Ejecuta `on_mount()` (sí, dos veces — es barato y simplifica el
     modelo).
   - Renderiza otra vez y manda un `patch`:

   ```json
   {"t":"patch","cid":"c-7a3f4d2b91","html":"<ul>...</ul>"}
   ```

4. **Patch en el cliente**. `live.js` busca `[data-hf-cid="c-..."]` en
   el DOM y le aplica `morphdom` con el HTML nuevo. Morphdom preserva
   foco, selección y scroll en los elementos que no cambian. Como el
   HTML es idéntico al cold-load, en la práctica no se ve cambio
   visual.

5. **Evento de usuario**. Click en un checkbox con
   `data-on:click="toggle:5"`. `live.js` lo captura:

   ```json
   {"t":"event","cid":"c-7a3f4d2b91","n":"toggle","p":"5"}
   ```

6. **Server event**. `LiveSession` busca el handler:
   - `instance.__class__._events["toggle"]` → la función decorada con
     `@event("toggle")`.
   - La invoca: `await handler(instance, "5")`.
   - Tras retornar, vuelve a renderizar y manda otro `patch`.

7. **`data-bind`**. Cuando el usuario teclea en `<input
   data-bind="new_text">`, el cliente espera 250 ms (debounce) y manda:

   ```json
   {"t":"bind","cid":"c-7a3f4d2b91","f":"new_text","v":"Buy mil"}
   ```

   El server actualiza `instance.new_text` (con la validación Pydantic)
   pero **no re-renderiza**. Así no se pisa lo que el usuario está
   escribiendo. El próximo evento (típicamente `submit`) sí ve el state
   actualizado y re-renderiza.

### Wire protocol completo

| Tipo | Dirección | Significado |
|---|---|---|
| `attach` | C→S | Registra una instancia bajo `cid` con sus props |
| `event` | C→S | Invoca un `@event` con un payload opcional |
| `bind` | C→S | Actualiza un campo del state, sin render |
| `detach` | C→S | Descarta una instancia (cleanup) |
| `patch` | S→C | HTML nuevo del componente; morphdom lo aplica |
| `nav` | S→C | `window.location.href = url` |
| `err` | S→C | Error en handler; cliente lo logea |
| `toast` | S→C | Notificación; el cliente decide cómo mostrarla |

Está todo en JSON plano con un único discriminador `t`. Sin
versionado en v1.0; si hace falta evolucionar, se añade `v: 2`.

### Garantías y caveats

- **Una instancia por (sesión WS, cid)**. Dos pestañas abiertas =
  dos sesiones WS distintas = dos instancias independientes.
- **Sticky sessions implícitas**: el state vive en RAM del proceso. Si
  vas a multi-instancia, hay que migrar `LiveSession.components` a
  Redis. Hoy no hay backend Redis incluido.
- **Reconexión**: si el WS cae, el cliente reabre con backoff
  exponencial. Cada `[data-hf-cid]` visible en el DOM se re-attachea.
  Como `on_mount` corre otra vez, **el state debe ser reconstruible
  desde `props` + DB**. No guardes asyncio tasks vivos en `self`.
- **Handler atómico por instancia**: cada handler corre bajo un
  `asyncio.Lock` por `cid`, así nunca hay race entre dos eventos en la
  misma instancia. Componentes distintos sí corren en paralelo.

---

## 10. Slots: inyección entre módulos

Distinto de los componentes. Los slots son **puntos de extensión** donde
módulos terceros pueden inyectar UI.

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
        priority=5,           # más alto = más arriba
        condition_fn=...,     # función opcional que decide si mostrar
    )
```

Cuando el módulo `loyalty` se desactiva, el `SlotRegistry` desregistra
sus contribuciones automáticamente. La página del dashboard simplemente
deja de mostrar el widget. **Cero acoplamiento**.

---

## 11. Eventos, hooks y bus de señales

Tres mecanismos complementarios:

### `AsyncEventBus` — pub/sub asíncrono

```python
# Suscribirse
@bus.on("invoice.paid")
async def send_email(event):
    await mailer.send(event.invoice_id)

# Emitir
await bus.emit("invoice.paid", invoice_id=42)
```

Usado para integraciones desacopladas. Soporta wildcards (`invoice.*`),
prioridades, y un `FakeEventBus` para tests.

### `HookRegistry` — filtros y acciones (estilo WordPress)

```python
# Filtrar un valor mientras viaja
hooks.add_filter("invoice.subtotal", lambda total, ctx: total * 1.1)

# Disparar una acción
await hooks.do_action("invoice.created", invoice=inv)
```

Conceptualmente parecido al EventBus pero con dos diferencias: las
filters **transforman** un valor; las actions son fire-and-forget.

### Eventos tipados

```python
from hotframe import BaseEvent, register_event
from pydantic import BaseModel

@register_event
class InvoicePaid(BaseEvent):
    invoice_id: int
    amount: float

await bus.emit_typed(InvoicePaid(invoice_id=42, amount=99.0))
```

Te da autocompletado y validación. Recomendado para eventos del dominio
estable.

### ORM events

`setup_orm_events()` (que `create_app` llama por ti) registra listeners
sobre SQLAlchemy. Cualquier `INSERT`/`UPDATE`/`DELETE` emite
automáticamente eventos como `models.User.created` o
`models.Invoice.updated`. No tienes que escribir nada — sólo suscribirte
si te interesan.

---

## 12. Persistencia: modelos, repositorios, protocolos

### Modelos

```python
# apps/shared/models.py
from hotframe import Base, Model, TimeStampedModel
from sqlalchemy.orm import Mapped, mapped_column

class User(TimeStampedModel):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True)
```

Mixins disponibles: `TimestampMixin` (`created_at`, `updated_at`),
`SoftDeleteMixin` (`deleted_at`), `AuditMixin` (`created_by_id`,
`updated_by_id`), `HubMixin` (multi-tenant `hub_id` con filtrado
automático).

### `BaseRepository[T]` — CRUD genérico tipado

```python
from hotframe import BaseRepository

class UserRepo(BaseRepository[User]):
    model = User

# Uso
repo = UserRepo(db)
user = await repo.get(42)
users = await repo.list(filters={"is_active": True})
await repo.create(email="x@y.com")
await repo.update(user, email="new@y.com")
await repo.delete(user)
```

### Protocolos (`ISession`, `IQueryBuilder`, `IRepository`)

Para no acoplar tu código a SQLAlchemy directamente, los protocolos
definen las interfaces:

```python
from hotframe import DbSession  # = Annotated[ISession, Depends(get_db)]

@router.get("/items")
async def items(db: DbSession):
    # db es ISession, no AsyncSession
    ...
```

Esto te permite escribir tests con un fake de `ISession` sin levantar
SQLAlchemy. La implementación real es siempre `AsyncSession`.

---

## 13. Migraciones y CLI

### Comandos `hf`

```bash
hf startproject myapp           # crea proyecto
hf startapp blog                # crea app
hf startmodule shop             # crea módulo
hf startmodule shop --api-only  # módulo solo API
hf startmodule shop --system    # módulo del sistema (no desinstalable)

hf runserver                    # uvicorn con reload
hf shell                        # REPL interactivo (IPython si está)
hf migrate                      # alembic upgrade head
hf makemigrations <app>         # alembic revision --autogenerate

hf modules list                 # lista módulos + estado
hf modules install <source>     # instala (nombre, .zip, URL, marketplace)
hf modules update <source>      # actualiza con backup + rollback
hf modules activate <name>
hf modules deactivate <name>
hf modules uninstall <name>

hf version
```

### Migraciones

Cada app y cada módulo tiene su propio `migrations/` con `env.py` y
`versions/`. `hf migrate` corre todas las pendientes en orden.

`hf makemigrations <app>` autogenera la próxima revisión con los
cambios detectados en los modelos. Los conflictos los resuelves a mano
(es Alembic estándar — el framework no añade magia que se rompa luego).

---

## 14. Settings: el único archivo de configuración

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

`HotframeSettings` es un Pydantic Settings que toma valores de:
1. Argumentos en el constructor.
2. Variables de entorno.
3. Archivo `.env` en el cwd.
4. Defaults definidos en `HotframeSettings`.

Los grupos de settings que más usarás:

- **Core**: `DATABASE_URL`, `SECRET_KEY`, `DEBUG`, `APP_TITLE`,
  `LOG_LEVEL`.
- **Auth**: `AUTH_USER_MODEL`, `AUTH_LOGIN_URL`, `PERMISSION_RESOLVER`.
- **Sesión**: `SESSION_COOKIE_NAME`, `SESSION_MAX_AGE`.
- **Seguridad**: `CSP_ENFORCE`, `CSP_TRUSTED_TYPES`, `CSP_ALLOWED_SOURCES`.
- **Rate limit**: `RATE_LIMIT_API`, `RATE_LIMIT_AUTH`,
  `RATE_LIMIT_AUTH_PREFIXES`.
- **DB pool**: `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_RECYCLE`,
  `DB_ECHO`.
- **Estáticos / media**: `STATIC_ROOT`, `STATIC_URL`, `MEDIA_ROOT`,
  `MEDIA_URL`, `MEDIA_STORAGE`.
- **Middleware**: `MIDDLEWARE` (lista de dotted paths). Casi nunca lo
  tocas; el default ya cubre todo.
- **Módulos**: `MODULES_DIR`, `MODULE_SOURCE`,
  `MODULE_MARKETPLACE_URL`.
- **Locale**: `LANGUAGE`, `CURRENCY`.
- **CORS**: `CORS_ORIGINS`, `CORS_METHODS`, `CORS_HEADERS`.

> **No hay `INSTALLED_APPS`** como en Django. Las apps se descubren al
> escanear `apps/`. Si quieres restringir, usa `INSTALLED_APPS` en
> settings (es opcional).

---

## 15. Seguridad: CSRF, CSP, sesiones, rate limit

Todo viene activo por defecto. No hay que escribir middleware a mano.

- **CSRF**: cada `TemplateResponse` recibe `csrf_token` y `csrf_input()`
  inyectados automáticamente. POST/PUT/PATCH/DELETE requieren el token
  o el header `X-CSRF-Token`. Las rutas en `CSRF_EXEMPT_PREFIXES`
  (`/api/`, `/health`, `/static/`) están exentas.
- **CSP**: cada respuesta lleva un nonce dinámico. Las plantillas
  reciben `csp_nonce` para inyectarlo en `<script nonce="...">`. Ajusta
  `CSP_ALLOWED_SOURCES` si añades CDNs externos.
- **Sesiones**: cookie firmada con `itsdangerous`. La sesión vive donde
  configures (memoria/Redis/DB — la implementación por defecto es en la
  cookie firmada).
- **Rate limit**: tres buckets — `api`, `view` (rutas `/m/`), `auth`
  (rutas en `RATE_LIMIT_AUTH_PREFIXES`). Por IP, ventana de 60s.

---

## 16. Cómo escribir un módulo desde cero

Pongamos que quieres un módulo `notes` con CRUD de notas.

### 1. Scaffolding

```bash
hf startmodule notes
```

Crea:

```
modules/notes/
├── module.py          ← ModuleConfig
├── models.py
├── routes.py          ← rutas HTML
├── api.py             ← rutas REST
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

### 3. Modelo

```python
# modules/notes/models.py
from hotframe import Base, TimeStampedModel
from sqlalchemy.orm import Mapped, mapped_column

class Note(TimeStampedModel):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str]
```

### 4. Migración

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

### 6. Vista que monta el componente

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

### 7. Activar

```bash
hf modules install notes
hf modules activate notes
```

Eso es todo. Sin reiniciar el server, las rutas `/m/notes/` están vivas
y el LiveComponent funciona con WS.

---

## Próximos pasos

- Lee el código de un módulo de referencia (cuando esté disponible
  el repo de demos).
- Lee `src/hotframe/live/` para ver cómo está implementado el runtime.
- Lee `src/hotframe/engine/module_runtime.py` para entender el
  ciclo de vida de los módulos.
- Suscríbete a issues del repo: https://github.com/hotframe/hotframe.

Si te quedas atascado, abre un issue con el caso reproducible mínimo.
La filosofía del proyecto es que **el framework no debería sorprenderte
mal nunca**.
