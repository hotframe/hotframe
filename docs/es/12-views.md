# 12. Vistas y respuestas (views/)

> La carpeta `views/` proporciona las tres capas de output que no son reactividad en tiempo real: el decorador `@view` para renderizar páginas HTML completas, los helpers de respuesta para redireccionamientos y mensajes flash, y el `BroadcastHub` para emitir actualizaciones en tiempo real vía SSE y WebSocket a múltiples clientes a la vez.

---

## Para qué sirve esta carpeta

`hotframe.views` es la API para rutas HTTP estándar y para broadcasting. Divide su responsabilidad en dos grandes bloques:

1. **`responses.py`**: decorador `@view` (y su alias `htmx_view`), resolución de templates por convención, autenticación, permisos, y helpers de respuesta HTTP (`reactive_redirect`, `reactive_refresh`, `reactive_message`, etc.).

2. **`broadcast.py`**: `BroadcastHub` — sistema de fan-out basado en `asyncio.Queue` para difundir mensajes a todos los clientes SSE o WebSocket suscritos a un topic. Incluye tres endpoints FastAPI: SSE por topic, SSE multiplexado, y WebSocket alternativo.

La carpeta no implementa reactividad de componentes — eso es trabajo de `live/`. Cuando una ruta necesita actualizar la UI en respuesta a un evento del servidor (por ejemplo, una nueva tarea creada por otro usuario), usa `BroadcastHub.publish()`. Cuando un componente necesita reactividad propia, usa `LiveComponent`.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`__init__.py`](../src/hotframe/views/__init__.py) | Re-exporta `BroadcastHub` y helpers; doc de la API pública. |
| [`responses.py`](../src/hotframe/views/responses.py) | Decorador `@view`, helpers de respuesta HTTP y SSE genérica. |
| [`broadcast.py`](../src/hotframe/views/broadcast.py) | `BroadcastHub`, `get_broadcast_hub`, `broadcast_router` con endpoints SSE y WS. |
| [`tests/test_responses.py`](../src/hotframe/views/tests/test_responses.py) | Tests del decorador `@view`, helpers de respuesta y `BroadcastHub`. |

---

## `__init__.py` — re-exportaciones

El módulo es intencionadamente mínimo. Documenta los imports recomendados:

```python
from hotframe.views.broadcast import BroadcastHub, broadcast_router, get_broadcast_hub
```

El `@view` se importa directamente desde `hotframe.views.responses` o, más habitualmente, desde `hotframe` (el bootstrap re-exporta el decorador en el namespace raíz).

---

## `responses.py` — el decorador `@view` y helpers HTTP

### `view` — el decorador central

```python
def view(
    full_template: str | None = None,
    partial_template: str | None = None,
    module_id: str | None = None,
    view_id: str | None = None,
    login_required: bool = True,
    permissions: list[str] | str | None = None,
) -> Callable:
```

El decorador envuelve una función async de vista FastAPI y añade cuatro responsabilidades:

#### 1. Autenticación

```python
if login_required:
    user_id = get_session_user_id(request)
    if user_id is None:
        return RedirectResponse(settings.AUTH_LOGIN_URL, status_code=302)
```

Si no hay usuario en sesión, redirige a `settings.AUTH_LOGIN_URL` con 302. Si `login_required=False`, la vista es pública.

#### 2. Permisos

```python
if permissions:
    user_perms = await _resolve_permissions(request, user_id)
    request.state.user_permissions = user_perms  # cache en el request
    if not all(has_permission(user_perms, p) for p in permissions):
        return RedirectResponse(settings.AUTH_UNAUTHORIZED_URL, status_code=302)
```

Los permisos se resuelven llamando al `PERMISSION_RESOLVER` configurado en settings (un dotted path `"myapp.auth.get_permissions"`). El resultado se cachea en `request.state.user_permissions` para no hacer múltiples llamadas por request.

El argumento `permissions` acepta tanto string como lista: `permissions="dashboard.view"` se normaliza a `["dashboard.view"]` al inicio del decorador.

#### 3. Resolución de templates

Si se pasan `module_id` y `view_id`, el decorador busca el template en múltiples convenciones, en este orden:

Para partial:
```
{module}/partials/{view}_content.html
{module}/partials/{view}.html
{module}/partials/{view}_list.html
{module}/partials/{view}_form.html
```

Para full page:
```
{module}/pages/{view}.html
{module}/pages/{view}_list.html
{module}/pages/{view}_form.html
{module}/pages/list.html
{module}/pages/index.html
```

La búsqueda se implementa en `_resolve_template`, que está decorada con `@lru_cache(maxsize=512)` usando un `env_id` como clave. Esto evita llamadas repetidas a `env.get_template` en cada request.

Caso especial: si `view_id == "dashboard"`, también prueba `{module}/pages/index.html` como primer candidato.

Si el resultado de la vista incluye una clave `"template"` en el dict, sobreescribe el template resuelto.

#### 4. Render

```python
return _render_full(templates, request, merged, _full, _partial)
```

`_render_full` construye el contexto fusionado (`global_ctx` + lo que devuelve la vista), añade `content_template` al contexto, y llama `templates.TemplateResponse(request, tpl_name, context)`. Si el render falla, devuelve un `HTMLResponse` con status 500 con el mensaje de error embebido.

#### Ejemplo de uso

```python
from fastapi import APIRouter, Request
from hotframe import view

router = APIRouter()

@router.get("/dashboard")
@view(module_id="shared", view_id="dashboard", permissions="dashboard.view")
async def dashboard(request: Request):
    return {"items": await load_items()}
```

La vista devuelve un dict. `@view` se encarga del resto.

Si la vista devuelve un objeto `Response` directamente, el decorador lo pasa sin modificar:

```python
@router.post("/action")
@view(module_id="shared", view_id="action", login_required=True)
async def action(request: Request):
    return reactive_redirect("/success")  # Response pasada tal cual
```

### `htmx_view` — el alias

```python
htmx_view = view
```

Son exactamente lo mismo. `htmx_view` existe por razones históricas de naming. Todo código nuevo puede usar `view` indistintamente.

### `is_reactive_request` e `is_htmx_request`

```python
def is_reactive_request(request: Request) -> bool:
    return False

def is_htmx_request(request: Request) -> bool:
    return is_reactive_request(request)
```

Ambas retornan siempre `False`. La reactividad en hotframe va por WebSocket, no por headers HTTP. Estas funciones existen para mantener compatibilidad con código que bifurca comportamiento según ellas, pero no tienen efecto real.

### Helpers de respuesta HTTP

#### `reactive_redirect(url: str) -> Response`

```python
def reactive_redirect(url: str) -> Response:
    return RedirectResponse(url, status_code=303)
```

303 See Other es el código correcto después de un POST exitoso (evita el reenvío del formulario al refrescar). Alias: `htmx_redirect`.

#### `reactive_refresh() -> Response`

```python
def reactive_refresh() -> Response:
    return HTMLResponse('<meta http-equiv="refresh" content="0">', status_code=200)
```

Recarga la página actual mediante un meta-refresh HTML. Alias: `htmx_refresh`.

#### `reactive_trigger(name: str, **detail: Any) -> Response`

```python
def reactive_trigger(name: str, **detail: Any) -> Response:
    payload = json.dumps(detail, ensure_ascii=False, default=str)
    script = (
        f"<script>window.dispatchEvent(new CustomEvent("
        f"{json.dumps(name)}, {{detail: {payload}}}))</script>"
    )
    return HTMLResponse(script, status_code=200)
```

Devuelve un fragmento HTML con un `<script>` que dispara un `CustomEvent` en `window`. Útil para rutas no-live que necesitan notificar a listeners del DOM.

```python
# En la vista:
return reactive_trigger("cartUpdated", count=5)

# En el template (o en otro JS):
window.addEventListener("cartUpdated", (e) => { console.log(e.detail.count); });
```

#### `reactive_message(level: str, text: str) -> Response`

```python
def reactive_message(level: str, text: str) -> Response:
    safe_level = (level or "info").replace('"', "")
    safe_text = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return HTMLResponse(
        f'<div class="toast toast-{safe_level}" role="status">{safe_text}</div>',
        status_code=200,
    )
```

Devuelve un fragmento HTML con un toast. El texto se escapa manualmente (no usa `markupsafe` porque es un helper simple). El nivel y el texto vienen de código del servidor, no de input de usuario.

#### `add_message(request, level, text)`

```python
def add_message(request: Request, level: str, text: str) -> None:
    if not hasattr(request.state, "_messages"):
        request.state._messages = []
    request.state._messages.append({"level": level, "text": text})
```

Añade un mensaje flash al estado del request. El middleware de flash lo lee en la siguiente respuesta de página completa. Los `LiveComponent` no deben usar esto: tienen `self.toast(...)` que es más eficiente.

#### `htmx_trigger(event, data=None) -> dict`

```python
def htmx_trigger(event: str, data: dict | None = None) -> dict:
    if data:
        return {event: data}
    return {event: True}
```

Este helper es diferente: devuelve un **dict**, no un `Response`. Es un helper de compatibilidad que construye el payload de un trigger header. En hotframe moderno se prefiere `reactive_trigger`.

### `sse_stream` — SSE genérico

```python
async def sse_stream(
    request: Request,
    generator: AsyncGenerator[dict[str, Any] | str, None],
    *,
    event_type: str = "message",
    ping_interval: int = 15,
) -> EventSourceResponse:
```

Envuelve cualquier generador async como una respuesta SSE. Útil para streams de progreso, streaming de logs, o cualquier push unidireccional que no requiera fan-out. A diferencia de `BroadcastHub`, no hay múltiples suscriptores — es un stream 1:1 por request.

Manejo de errores: si el generador lanza una excepción, emite un evento `"error"` con el mensaje y un `"done"` al finalizar. Si el cliente se desconecta (`request.is_disconnected()`), corta el stream limpiamente.

### Resolución de permisos

```python
async def _resolve_permissions(request: Request, user_id: Any) -> list[str]:
    from hotframe.config.settings import get_settings
    settings = get_settings()
    if not settings.PERMISSION_RESOLVER:
        return []
    module_path, func_name = settings.PERMISSION_RESOLVER.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    resolver = getattr(mod, func_name)
    return await resolver(request, user_id)
```

El resolver es una función async del proyecto, configurada en `settings.PERMISSION_RESOLVER` como dotted path. Recibe el `request` y el `user_id` y devuelve una lista de strings de permisos. Si no está configurado, todos los usuarios autenticados tienen acceso.

---

## `broadcast.py` — `BroadcastHub` y endpoints

### Arquitectura

El comentario del módulo lo ilustra perfectamente:

```
Module code                   Browser clients
    |                         |            |
broadcast("todos", html)  SSE /stream/   SSE /stream/
    |                     todos          todos
    v                         |            |
BroadcastHub.publish()        v            v
    |                    queue_A       queue_B
    +---> fan-out -----> event         event
```

Cada cliente SSE conectado tiene su propia `asyncio.Queue`. Cuando se publica en un topic, el mensaje se copia a todas las colas de ese topic.

### `BroadcastHub`

```python
class BroadcastHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
```

Un dict de `topic -> set de colas`. No hay locks explícitos porque asyncio es single-threaded.

#### `subscribe(topic: str) -> asyncio.Queue`

```python
async def subscribe(self, topic: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    self._subscribers[topic].add(queue)
    return queue
```

Crea una cola con capacidad 64. El tamaño máximo protege contra clientes lentos que no consumen. Devuelve la cola al llamador, que la usará en un bucle `await queue.get()`.

#### `unsubscribe(topic: str, queue: asyncio.Queue)`

```python
async def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
    self._subscribers[topic].discard(queue)
    if not self._subscribers[topic]:
        del self._subscribers[topic]
```

Elimina la cola del set. Si el set queda vacío, borra la entrada del topic (evita memory leak de topics inactivos).

#### `publish(topic: str, data: str) -> int`

```python
async def publish(self, topic: str, data: str) -> int:
    subscribers = self._subscribers.get(topic, set())
    delivered = 0
    stale: list[asyncio.Queue] = []
    for queue in subscribers:
        try:
            queue.put_nowait(data)
            delivered += 1
        except asyncio.QueueFull:
            logger.warning("SSE queue full for topic=%s, dropping message", topic)
            stale.append(queue)
    for q in stale:
        subscribers.discard(q)
    return delivered
```

Usa `put_nowait` — no bloquea el publicador aunque algún cliente esté saturado. Las colas llenas se marcan como `stale` y se eliminan automáticamente: un cliente que no consume se desregistra solo. Devuelve el número de clientes que recibieron el mensaje.

#### Métodos de inspección

```python
def topic_count(self) -> int:
    return len(self._subscribers)

def subscriber_count(self, topic: str) -> int:
    return len(self._subscribers.get(topic, set()))
```

Útiles para dashboards de administración o métricas.

### `get_broadcast_hub(request)`

```python
def get_broadcast_hub(request: Request) -> BroadcastHub:
    return request.app.state.broadcast_hub
```

El singleton del `BroadcastHub` vive en `app.state.broadcast_hub`, puesto ahí por el bootstrap. Se accede mediante este helper en vistas y handlers.

### `broadcast_router` — los tres endpoints

El router se monta en el bootstrap. Sus rutas son:

#### `GET /stream/{topic:path}` — SSE por topic

```python
@broadcast_router.get("/stream/{topic:path}")
async def stream_topic(request, topic, user: CurrentUser) -> Response:
```

Requiere autenticación (via `CurrentUser`). Crea una cola con `hub.subscribe(topic)`, abre un `EventSourceResponse` y manda mensajes con `yield {"event": "message", "data": data}`. Cada 30 segundos hace un check de desconexión con `asyncio.wait_for(..., timeout=30.0)`. Al desconectarse (o cerrarse el loop), llama `hub.unsubscribe`.

El ping está configurado en 15 segundos (`EventSourceResponse(..., ping=15)`) para mantener la conexión viva a través de proxies.

#### `GET /stream/_mux?topics=a,b,c` — SSE multiplexado

```python
@broadcast_router.get("/stream/_mux")
async def stream_multiplexed(request, user: CurrentUser, topics: str = "") -> Response:
```

Un solo SSE para múltiples topics. El cliente pasa `topics=a,b,c` como query parameter. Se crean colas para cada topic y se espera con `asyncio.wait(..., return_when=FIRST_COMPLETED)`:

```python
done, pending = await asyncio.wait(
    [asyncio.create_task(_wait_queue(t, q)) for t, q in queues],
    timeout=30.0,
    return_when=asyncio.FIRST_COMPLETED,
)
```

Los mensajes se emiten con `{"event": topic_name, "data": data}`, lo que permite al cliente distinguir de qué topic viene cada mensaje por el campo `event` del SSE.

Validación: si `topic_list` está vacío, responde 400 inmediatamente antes de abrir ninguna cola.

#### `POST /stream/{topic:path}` — publicar desde el navegador

```python
@broadcast_router.post("/stream/{topic:path}")
async def publish_to_topic(request: Request, topic: str) -> Response:
    body = await request.body()
    data = body.decode("utf-8")
    count = await hub.publish(topic, data)
    return JSONResponse({"published": True, "subscribers": count})
```

Permite que el navegador publique directamente (body crudo = HTML fragment o JSON string). Devuelve el número de suscriptores que recibieron el mensaje.

#### `WebSocket /ws/stream/{topic:path}` — alternativa WS

```python
@broadcast_router.websocket("/ws/stream/{topic:path}")
async def ws_broadcast_handler(websocket: WebSocket, topic: str) -> None:
```

Para entornos donde SSE no es fiable (proxies corporativos, redes móviles). La autenticación se hace manualmente antes de `accept()`:

```python
session = get_session_data(websocket)
if not session.get(SESSION_USER_KEY):
    await websocket.close(code=4401)
    return
```

Código `4401` es un código de aplicación en el rango 4000-4999 que mapea a "unauthorized". El cierre ocurre antes de `accept()`, por lo que el handshake falla con HTTP 403 en el cliente.

Una vez autenticado, el loop es sencillo:

```python
while True:
    data = await asyncio.wait_for(queue.get(), timeout=30.0)
    await websocket.send_text(data)
```

Si no hay mensajes en 30s, envía un ping `{"type": "ping"}` para mantener la conexión viva.

### Patrón de uso desde un módulo

```python
# En la vista que crea un item:
from hotframe.views.broadcast import get_broadcast_hub

@router.post("/todos")
@view(module_id="todos", view_id="create", login_required=True)
async def create_todo(request: Request, db: DbSession):
    todo = await Todo.create(text=request.form["text"])
    rendered = templates.get_template("todos/partials/todo_item.html").render(todo=todo)
    hub = get_broadcast_hub(request)
    await hub.publish("todos", rendered)
    return reactive_redirect("/todos")
```

```javascript
// En el cliente:
const es = new EventSource("/stream/todos");
es.addEventListener("message", (e) => {
    document.getElementById("todo-list").insertAdjacentHTML("beforeend", e.data);
});
```

---

## Los tests

### `test_responses.py`

Los tests usan un `_make_request` helper que construye un `Request` de Starlette con un scope mínimo:

```python
def _make_request(headers=None, query=""):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [...],
        "query_string": query.encode(),
    }
    return Request(scope)
```

#### Tests del decorador `@view`

```python
@pytest.mark.asyncio
async def test_login_required_redirects_when_no_user(monkeypatch):
    monkeypatch.setattr("hotframe.views.responses.get_session_user_id", lambda r: None)

    @view(login_required=True)
    async def handler(request):
        return {}

    resp = await handler(_make_request())
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
```

El test usa `monkeypatch` para simular que no hay usuario en sesión. Verifica que el decorador redirige correctamente. El patrón es limpio: no necesita un servidor real.

#### Tests de helpers

```python
class TestReactiveRedirect:
    def test_emits_303_redirect(self):
        response = reactive_redirect("/login")
        assert response.status_code == 303

class TestReactiveMessage:
    def test_escapes_html(self):
        response = reactive_message("info", "<script>alert(1)</script>")
        body = _body(response)
        assert "<script>alert" not in body
        assert "&lt;script&gt;" in body
```

La función `_body` maneja tanto responses con `body` directo como responses con `body_iterator` (streaming), usando un ThreadPoolExecutor para ejecutar el corutine si hay un loop activo.

#### Test de `BroadcastHub`

```python
class TestBroadcast:
    def test_broadcast_hub_import(self):
        from hotframe.views.broadcast import BroadcastHub
        hub = BroadcastHub()
        assert hub is not None
```

Solo verifica la importación. Los tests de integración del hub (subscribe/publish/unsubscribe) se harían en un contexto async con `pytest.mark.asyncio`.

---

## Cómo encaja con el resto del framework

| Componente | Relación con `views/` |
|---|---|
| `hotframe.bootstrap.create_app` | Instancia `BroadcastHub`, lo guarda en `app.state.broadcast_hub`, y monta `broadcast_router`. |
| `hotframe.live.LiveComponent` | Los `LiveComponent` NO usan `@view` ni `BroadcastHub`. Tienen su propio canal WebSocket (`/ws/_live`) y métodos `self.navigate()` / `self.toast()`. |
| `hotframe.auth` | `@view` usa `get_session_user_id` para autenticar. `CurrentUser` dependency protege los endpoints de broadcast. |
| `hotframe.config.settings` | `AUTH_LOGIN_URL`, `AUTH_UNAUTHORIZED_URL`, `PERMISSION_RESOLVER` son leídos dentro del decorador en cada request. |
| `hotframe.templating.globals` | `get_global_context(request)` inyecta `request`, `csrf_token`, `csp_nonce`, `user` y otros globals en cada render. |
| Módulos dinámicos | Sus rutas usan `@view(module_id="nombre", ...)`. El `module_registry` del request aporta la navegación del módulo al contexto. |
| Apps estáticas | Sus `routes.py` usan el mismo `@view`. No hay diferencia de API entre apps y módulos desde el punto de vista del decorador. |

### Tabla de decisión: qué usar cuándo

| Necesidad | Herramienta |
|---|---|
| Página HTML estática con auth y permisos | `@view` |
| Componente con estado reactivo en servidor | `LiveComponent` en `live/` |
| Push de actualizaciones a múltiples tabs abiertas | `BroadcastHub.publish()` + `/stream/{topic}` |
| Redirigir tras un POST | `reactive_redirect("/url")` |
| Recargar la página actual | `reactive_refresh()` |
| Notificar JS del DOM sin live | `reactive_trigger("evento", data=...)` |
| Mensaje flash en la próxima página | `add_message(request, "success", "Guardado")` |
| Stream de log / progreso (1:1) | `sse_stream(request, generator)` |
| Notificación de toast en un componente reactivo | `await self.toast("mensaje", level="success")` |
