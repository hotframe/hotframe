# 11. El runtime reactivo (live/)

> El módulo `live/` es el corazón de la reactividad de hotframe: implementa componentes con estado en servidor, ciclo de vida, un protocolo WebSocket JSON y el cliente JavaScript que cierra el bucle en el navegador. Leyendo esta sección entenderás qué ocurre desde que Jinja renderiza `{% live %}` hasta que morphdom actualiza un nodo del DOM.

---

## Para qué sirve esta carpeta

`hotframe.live` permite que el **estado de un componente de UI viva en el proceso Python** en lugar de en el navegador. El navegador actúa como terminal: muestra HTML, captura eventos del usuario y aplica parches. El servidor es quien decide qué HTML producir en cada momento.

El modelo es intencionadamente sencillo:

1. La página renderiza el componente en el HTML inicial (cold-load).
2. El cliente abre un WebSocket y re-attachea cada componente que encuentra en el DOM.
3. Las interacciones del usuario (clics, inputs, envíos de formulario) viajan como mensajes JSON por el WebSocket.
4. El servidor ejecuta el handler, re-renderiza el template y manda el HTML nuevo como parche.
5. `morphdom` lo aplica sin perder el foco ni el scroll.

No hay JavaScript que escribir en los módulos. No hay build. No hay estado compartido entre servidor y cliente que mantener sincronizado.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`__init__.py`](../src/hotframe/live/__init__.py) | Re-exporta las piezas públicas; punto de entrada del submódulo. |
| [`base.py`](../src/hotframe/live/base.py) | Clase `LiveComponent`: Pydantic model con identidad, ciclo de vida y tabla de eventos. |
| [`decorators.py`](../src/hotframe/live/decorators.py) | Decorador `@event(name)` y función `get_event_name`. |
| [`diff.py`](../src/hotframe/live/diff.py) | Renderizado del template + envoltura `data-hf-cid`. |
| [`jinja_ext.py`](../src/hotframe/live/jinja_ext.py) | Tag Jinja2 `{% live %}` para el cold-load. |
| [`protocol.py`](../src/hotframe/live/protocol.py) | TypedDicts del wire format (mensajes C→S y S→C) + helpers de construcción. |
| [`session.py`](../src/hotframe/live/session.py) | `LiveSession`: agrega instancias por WebSocket, despacha mensajes, gestiona locks. |
| [`runtime.py`](../src/hotframe/live/runtime.py) | `LiveRuntime`: singleton por app, dueño de todas las sesiones activas. |
| [`ws.py`](../src/hotframe/live/ws.py) | Endpoint FastAPI `/ws/_live` — el único punto de entrada WebSocket del runtime. |
| [`assets.py`](../src/hotframe/live/assets.py) | Global Jinja2 `live_assets()` que emite los `<script>` de `morphdom` y `live.js`. |
| [`static/live.js`](../src/hotframe/live/static/live.js) | Cliente JavaScript: WS, captura de eventos, bind con debounce, parches morphdom. |
| [`static/morphdom.min.js`](../src/hotframe/live/static/morphdom.min.js) | Librería de terceros para diff/patch eficiente del DOM. No se analiza. |
| [`tests/test_base.py`](../src/hotframe/live/tests/test_base.py) | Tests de `LiveComponent`: tabla de eventos, mutación de estado, contexto de render. |
| [`tests/test_decorators.py`](../src/hotframe/live/tests/test_decorators.py) | Tests del decorador `@event`. |
| [`tests/test_diff.py`](../src/hotframe/live/tests/test_diff.py) | Tests del render interno y del envelope HTML. |
| [`tests/test_protocol.py`](../src/hotframe/live/tests/test_protocol.py) | Tests de los helpers de construcción de mensajes. |
| [`tests/test_session.py`](../src/hotframe/live/tests/test_session.py) | Tests de integración de `LiveSession` con un WebSocket falso. |

---

## `__init__.py` — re-exportaciones públicas

El fichero declara la API pública del submódulo. Importar desde `hotframe.live` da acceso directo a:

```python
from hotframe.live import LiveComponent, event, LiveRuntime, LiveSession, live_router
```

La función `get_runtime(app)` también se exporta para que el endpoint WS y las extensiones Jinja puedan recuperar el singleton sin importar `runtime.py` directamente.

---

## `base.py` — `LiveComponent`

### Propósito

`LiveComponent` es la clase base de todos los componentes reactivos. Extiende `BaseModel` de Pydantic v2, añadiendo:

1. **Identidad** (`_cid`): un string opaco asignado por el runtime. Es la clave con la que el protocolo wire y morphdom identifican la instancia.
2. **Ciclo de vida** (`on_mount`, `on_unmount`): corrutinas que el runtime invoca en los momentos correctos.
3. **Tabla de eventos** (`_events`): dict de clase construido una vez por subclase en `__init_subclass__`, que mapea nombre de evento → función handler.

### Declaración de la clase

```python
class LiveComponent(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, validate_assignment=True)

    _events: ClassVar[dict[str, EventHandler]] = {}

    _cid: str = PrivateAttr(default="")
    _session: LiveSession | None = PrivateAttr(default=None)
    _component_name: str = PrivateAttr(default="")
    _last_html: str = PrivateAttr(default="")
```

`model_config` tiene dos flags importantes:
- `arbitrary_types_allowed=True`: permite que los campos de estado contengan objetos ORM, dataclasses, etc., sin que Pydantic los rechace.
- `validate_assignment=True`: cada `self.x = valor` ejecuta los validadores del campo. Esto hace que las mutaciones de estado en handlers sean siempre seguras: un campo `count: int` nunca podrá quedar con un string.

Los `PrivateAttr` son atributos de instancia que Pydantic no serializa en `model_dump()` ni expone en el wire format. Se escriben directamente con `instance._cid = "c-abc"`.

### `__init_subclass__` — construcción de la tabla de eventos

```python
def __init_subclass__(cls, **kwargs: Any) -> None:
    super().__init_subclass__(**kwargs)
    events: dict[str, EventHandler] = {}
    for klass in reversed(cls.__mro__):
        if klass is object:
            continue
        for attr_name, attr in vars(klass).items():
            wire_name = get_event_name(attr)
            if wire_name is None:
                continue
            if not inspect.iscoroutinefunction(attr):
                raise TypeError(...)
            events[wire_name] = attr
    cls._events = events
```

Se llama **una sola vez al definir la subclase**, no en cada instanciación. El MRO se recorre al revés (de objeto a subclase) para que las subclases sobreescriban handlers del padre. Cada handler debe ser `async def`; si no, se lanza `TypeError` en el momento de definir la clase, no en runtime. El lookup en despacho es O(1): `instance.__class__._events["toggle"]`.

### Ciclo de vida

| Método | Firma | Cuándo se llama |
|---|---|---|
| `on_mount` | `async def on_mount(self) -> None` | Tras instantiar y antes del primer render. En cold-load (Jinja) y en cada re-attach (WS abierto / reconexión). |
| `on_unmount` | `async def on_unmount(self) -> None` | En `detach` del cliente, cierre del WS o shutdown del runtime. |

Ambos son no-ops por defecto. El contrato importante: **el estado debe ser reconstruible desde `props` + DB cada vez que se llama `on_mount`**. Si el WebSocket cae y reconecta, `on_mount` corre de nuevo.

### Helpers para handlers

Los handlers pueden llamar a dos corrutinas de conveniencia:

```python
async def navigate(self, url: str) -> None:
    # Envía {"t": "nav", "url": url} al cliente → window.location.href = url
    await self._session.send_nav(url)

async def toast(self, msg: str, level: str = "info") -> None:
    # Envía {"t": "toast", ...} → dispara el evento hf:toast en el DOM
    await self._session.send_toast(msg, level=level)
```

Ambas lanzan `RuntimeError` si se llaman fuera de una sesión activa (por ejemplo, desde `on_mount` durante cold-load).

### Contexto de render

```python
def render_context(self) -> dict[str, Any]:
    ctx = self.model_dump()
    extra = self.extra_context()
    if extra:
        ctx.update(extra)
    return ctx

def extra_context(self) -> dict[str, Any]:
    return {}
```

`render_context()` produce el dict que se pasa al template Jinja. Por defecto contiene todos los campos Pydantic (props + state). Si necesitas exponer valores derivados sin añadirlos como campos, sobreescribe `extra_context`:

```python
def extra_context(self) -> dict:
    return {"open_count": sum(1 for t in self.items if not t.done)}
```

---

## `decorators.py` — `@event`

### El marcador

```python
_EVENT_NAME_ATTR = "__hf_live_event__"

def event(name: str) -> Callable[[F], F]:
    if not isinstance(name, str) or not name:
        raise ValueError("@event(name) requires a non-empty string")

    def decorate(fn: F) -> F:
        setattr(fn, _EVENT_NAME_ATTR, name)
        return fn

    return decorate
```

El decorador **no envuelve** la función: la estampa con un atributo privado y la devuelve tal cual. Cero overhead en invocación. El nombre en el wire protocol es totalmente independiente del nombre Python del método.

### `get_event_name`

```python
def get_event_name(fn: Callable[..., Any]) -> str | None:
    return getattr(fn, _EVENT_NAME_ATTR, None)
```

Función pública que `__init_subclass__` llama al escanear los atributos de la clase. Devuelve `None` para métodos no decorados.

### Ejemplo de uso

```python
class TodoList(LiveComponent):
    @event("toggle")         # wire name = "toggle"
    async def toggle(self, todo_id: str) -> None:
        ...

    @event("add")            # wire name diferente del método ("add")
    async def add_item(self) -> None:
        ...
```

---

## `diff.py` — renderizado e envoltura HTML

### Estrategia

El módulo es deliberadamente simple: **sin diff de plantillas**. Siempre se emite el HTML completo del componente y morphdom en el cliente aplica el diff a nivel de DOM. El comentario del código lo explica claramente:

> "The MVP strategy is 'no diff' — we always emit the full component HTML and let morphdom on the client patch only what changed. This is simpler and far more debuggable than an AST tag-and-track scheme."

### `render_component_inner`

```python
def render_component_inner(
    env: Environment,
    entry: ComponentEntry,
    instance: LiveComponent,
) -> str:
```

Obtiene el template del registro, llama `instance.render_context()` para construir el contexto, y llama `template.render(**context)`. Devuelve el HTML interno — **sin** el wrapper `data-hf-cid`. Los errores de template se logean y devuelven string vacío (nunca propagan).

### `wrap_with_envelope`

```python
def wrap_with_envelope(
    inner_html: str,
    *,
    cid: str,
    component_name: str,
    instance: LiveComponent,
    prop_names: list[str],
) -> str:
```

Genera el `<div>` que envuelve al componente con los tres atributos que el cliente necesita:

- `data-hf-cid`: id de instancia, para localizar el nodo en el DOM al aplicar parches.
- `data-hf-component`: nombre del componente, para reinstanciar en reconexiones.
- `data-hf-props`: JSON de las props originales (HTML-escaped), para reconstruir la instancia exacta en re-attach.

Los props se serializan con `json.dumps(raw, default=str, separators=(",", ":"))` y se escapan con `html.escape(..., quote=True)` para que sean seguros en un atributo HTML.

### `render_initial_html`

```python
def render_initial_html(
    env: Environment,
    entry: ComponentEntry,
    instance: LiveComponent,
    *,
    prop_names: list[str],
) -> Markup:
```

Combina `render_component_inner` + `wrap_with_envelope` y devuelve `Markup` (Jinja2 no re-escapa el resultado). Es la función llamada por el tag `{% live %}` durante el cold-load.

---

## `jinja_ext.py` — el tag `{% live %}`

### `LiveExtension`

```python
class LiveExtension(Extension):
    tags = {"live"}
```

Registrada en el `Environment` de Jinja2 durante el bootstrap. Cuando el parser encuentra `{% live "nombre" k=v ... %}` construye una llamada a `_render_live`.

### `_render_live`

```python
def _render_live(self, __component_name__: str, /, **props) -> Markup:
```

Pasos que ejecuta:

1. Obtiene el `ComponentRegistry` del entorno Jinja (`env.globals["_hotframe_components"]`).
2. Busca la entrada del componente; verifica que sea un `LiveComponent` (`entry.is_live == True`).
3. Instancia la clase con los props: `instance = cls(**props)`.
4. Genera el `cid`: `instance._cid = f"c-{uuid.uuid4().hex[:12]}"`.
5. Llama `_run_async(instance.on_mount())` para ejecutar el hook de mount en contexto síncrono.
6. Llama `render_initial_html(env, entry, instance, prop_names=...)` y devuelve el `Markup`.

### `_run_async` — el puente sync/async

Jinja2 renderiza templates de forma síncrona, pero `on_mount` puede ser una corrutina con awaits. `_run_async` gestiona los dos escenarios:

```python
def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        return asyncio.run(coro)

    # Ya hay un loop corriendo (tests): usar un thread separado
    import threading
    result_box: dict[str, object] = {}
    def runner() -> None:
        result_box["v"] = asyncio.run(coro)
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    return result_box.get("v")
```

En producción (FastAPI/Starlette), el template se renderiza fuera del bucle de eventos, así que `asyncio.run()` funciona directamente. En tests donde ya hay un loop activo, crea un thread daemon para evitar el deadlock.

---

## `protocol.py` — el wire protocol

### Filosofía de diseño

Los mensajes son `TypedDict` (no modelos Pydantic) para evitar overhead de validación en el hot path del WebSocket. El discriminador `t` es un string de una letra. Los nombres de campo son cortos por el mismo motivo.

### Mensajes Cliente → Servidor

#### `AttachMessage`
```python
class AttachMessage(TypedDict):
    t: Literal["attach"]
    cid: str    # id generado en cold-load
    name: str   # nombre del componente
    props: dict[str, Any]  # props originales
```
Enviado al abrir el WS (y en cada reconexión) por cada `[data-hf-cid]` en el DOM.

#### `EventMessage`
```python
class EventMessage(TypedDict, total=False):
    t: Literal["event"]
    cid: str
    n: str      # wire name del evento
    p: Any      # payload (opcional)
```
`total=False` hace que `p` sea opcional. Para clics simples, `p` es un string o `null`. Para formularios, `p` es un dict (el `FormData` serializado).

#### `BindMessage`
```python
class BindMessage(TypedDict):
    t: Literal["bind"]
    cid: str
    f: str      # nombre del campo
    v: Any      # nuevo valor
```
Enviado por el cliente con debounce de 250 ms al escribir en `<input data-bind="campo">`. El servidor actualiza el campo pero **no re-renderiza**.

#### `DetachMessage`
```python
class DetachMessage(TypedDict):
    t: Literal["detach"]
    cid: str
```
Cuando el componente sale del DOM. El servidor llama `on_unmount` y libera memoria.

### Mensajes Servidor → Cliente

#### `PatchMessage`
```python
class PatchMessage(TypedDict):
    t: Literal["patch"]
    cid: str
    html: str   # HTML interno del componente (sin el wrapper data-hf-cid)
```

#### `NavMessage`
```python
class NavMessage(TypedDict):
    t: Literal["nav"]
    url: str
```
El cliente ejecuta `window.location.href = url`.

#### `ErrMessage`
```python
class ErrMessage(TypedDict, total=False):
    t: Literal["err"]
    cid: str
    msg: str
    code: str   # "not_found", "props", "mount", "handler", "internal", ...
```

#### `ToastMessage`
```python
class ToastMessage(TypedDict):
    t: Literal["toast"]
    level: Literal["info", "success", "warning", "error"]
    msg: str
```
El cliente emite `CustomEvent("hf:toast", {detail: msg})`. La UI de toast la implementa el proyecto.

### Helpers de construcción

```python
make_patch(cid, html) -> PatchMessage
make_nav(url)         -> NavMessage
make_err(cid, msg, code=None) -> ErrMessage
make_toast(msg, level="info") -> ToastMessage
```

Centralizados para que ningún código fuera de `protocol.py` construya envelopes a mano.

---

## `session.py` — `LiveSession`

### Estructura

```python
class LiveSession:
    def __init__(self, session_id: str, ws: WebSocket, runtime: LiveRuntime) -> None:
        self.id = session_id
        self.ws = ws
        self.runtime = runtime
        self.components: dict[str, LiveComponent] = {}   # cid -> instancia
        self._locks: dict[str, asyncio.Lock] = {}        # cid -> Lock
        self._closed = False
```

Un `LiveSession` es la unidad viva por WebSocket. Contiene **todas** las instancias de componentes de esa pestaña.

### `handle_message`

El dispatcher principal:

```python
async def handle_message(self, msg: ClientMessage) -> None:
    t = msg.get("t")
    if t == "attach":  await self._attach(msg)
    elif t == "event": await self._event(msg)
    elif t == "bind":  await self._bind(msg)
    elif t == "detach": await self._detach(msg)
    else: logger.warning(...)
```

Tipos desconocidos se loguean y descartan. Errores de handler se capturan y se reportan como `err` envelopes sin cerrar el WS.

### `_attach`

1. Resuelve el `ComponentEntry` en el registry.
2. Instancia la clase con los props.
3. Estampa `_cid`, `_session`, `_component_name` en la instancia.
4. Registra en `self.components[cid]` y crea `self._locks[cid] = asyncio.Lock()`.
5. Bajo el lock: llama `await instance.on_mount()`.
6. Llama `await self._render_and_send(instance)`.

### `_event`

```python
async def _event(self, msg: EventMessage) -> None:
    handler = instance.__class__._events.get(wire_name)
    async with self._locks[cid]:
        await _invoke_handler(handler, instance, payload)
        await self._render_and_send(instance)
```

El lock garantiza que si dos eventos para el mismo `cid` llegan casi simultáneamente, el segundo espera a que termine el primero. Componentes distintos corren en paralelo (locks independientes).

### `_bind`

```python
async def _bind(self, msg: BindMessage) -> None:
    async with self._locks[cid]:
        setattr(instance, field, value)  # Pydantic valida aquí
    # Sin re-render
```

`validate_assignment=True` en `LiveComponent` hace que `setattr` pase por los validadores Pydantic. Si el valor es inválido, se logua una advertencia pero no se manda `err` al cliente (es una operación silenciosa).

### `_render_and_send`

```python
async def _render_and_send(self, instance: LiveComponent) -> None:
    html = render_component_inner(self.runtime.env, entry, instance)
    instance._last_html = html
    await self._send(make_patch(instance.cid, html))
```

Siempre re-renderiza completo. El `_last_html` se guarda en la instancia (disponible para debugging).

### `_send`

```python
async def _send(self, envelope: Any) -> None:
    if self._closed:
        return
    try:
        await self.ws.send_json(envelope)
    except Exception:
        logger.warning("LiveSession %s: send failed; closing", self.id)
        self._closed = True
```

Si el WS ya no acepta mensajes, marca la sesión como cerrada en lugar de propagar la excepción.

### `shutdown`

```python
async def shutdown(self) -> None:
    if self._closed:
        return
    self._closed = True
    for cid, instance in list(self.components.items()):
        await instance.on_unmount()
    self.components.clear()
    self._locks.clear()
```

Idempotente. Se llama en `WebSocketDisconnect`, en cierre limpio y en shutdown del runtime.

### `_invoke_handler` — adaptador de signatures

```python
async def _invoke_handler(handler, instance, payload) -> None:
```

Soporta tres formas de firma para maximizar la ergonomía:

1. Sin argumentos extra: `await handler(instance)` — ignora el payload.
2. Un argumento posicional: `await handler(instance, payload)` — útil para `toggle(self, todo_id: str)`.
3. Un dict desempaquetado en kwargs: `await handler(instance, **payload)` — para formularios donde el payload es `{"new_text": "..."}`.

La detección es por inspección de la firma (`inspect.signature`) en cada llamada.

---

## `runtime.py` — `LiveRuntime`

### Estructura

```python
class LiveRuntime:
    def __init__(self, registry: ComponentRegistry, env: Environment) -> None:
        self.registry = registry
        self.env = env
        self.sessions: dict[str, LiveSession] = {}
```

Singleton por app. El bootstrap lo crea durante el lifespan startup y lo guarda en `app.state.live`.

### Gestión de sesiones

```python
async def open_session(self, session_id: str, ws: WebSocket) -> LiveSession:
    existing = self.sessions.pop(session_id, None)
    if existing is not None:
        await existing.shutdown()  # cierra sesión huérfana
    session = LiveSession(session_id, ws, self)
    self.sessions[session_id] = session
    return session

async def close_session(self, session_id: str) -> None:
    session = self.sessions.pop(session_id, None)
    if session:
        await session.shutdown()

async def shutdown(self) -> None:
    for session_id in list(self.sessions.keys()):
        await self.close_session(session_id)
```

`open_session` cierra activamente sesiones con el mismo id que aún no habían detectado su WS muerto. Esto permite reconexiones limpias.

### `get_runtime`

```python
def get_runtime(app) -> LiveRuntime:
    runtime = getattr(app.state, "live", None)
    if runtime is None:
        raise RuntimeError(...)
    return runtime
```

Helper usado por el endpoint WS y la extensión Jinja. Falla ruidosamente si el bootstrap no inicializó el runtime.

---

## `ws.py` — el endpoint `/ws/_live`

### Resolución del session_id

```python
def _resolve_session_id(ws: WebSocket) -> str:
```

Orden de preferencia:
1. Clave conocida en `ws.session` (de `SessionMiddleware`): `user_id`, `session_id` o `sid`.
2. Cookie `session` cruda.
3. `host:port` del cliente.
4. `f"anon:{id(ws)}"` como último recurso.

La estabilidad entre reconexiones importa: si el id coincide con una sesión huérfana, `open_session` la cierra primero.

### El endpoint

```python
@live_router.websocket("/ws/_live")
async def live_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    runtime = get_runtime(ws.app)
    session_id = _resolve_session_id(ws)
    session = await runtime.open_session(session_id, ws)

    try:
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict):
                continue
            await session.handle_message(msg)
    except WebSocketDisconnect:
        pass
    finally:
        await runtime.close_session(session_id)
```

Toda la complejidad está en `session.handle_message`. El endpoint es un thin wrapper: acepta, abre sesión, bucle de recepción, cierra.

---

## `assets.py` — `live_assets()`

```python
LIVE_STATIC_BASE = "/static/hotframe"

@pass_context
def live_assets(ctx) -> Markup:
    nonce = ctx.get("csp_nonce") or ""
    nonce_attr = f' nonce="{nonce}"' if nonce else ""
    return Markup(
        f'<script{nonce_attr} src="{LIVE_STATIC_BASE}/morphdom.min.js"></script>\n'
        f'<script{nonce_attr} src="{LIVE_STATIC_BASE}/live.js"></script>'
    )
```

Global de Jinja2 que emite los dos `<script>` necesarios. Propaga el `csp_nonce` del contexto de la petición para que los scripts pasen una Content Security Policy estricta. Se llama una vez en el `<head>` de la base:

```jinja
{{ live_assets() }}
```

---

## El cliente `live.js`

`live.js` es un IIFE (Immediately Invoked Function Expression) que se inicializa en `DOMContentLoaded`. Expone `window.hotframeLive` para debugging y emite `CustomEvent("hf:ready")` en el documento.

### `LiveClient` — la clase central

```javascript
function LiveClient(url) {
    this.url = url;
    this.ws = null;
    this.queue = [];          // mensajes pendientes mientras no hay conexión
    this.bindTimers = {};     // "${cid}:${field}" -> timeout id (debounce)
    this.reconnectAttempt = 0;
    this.connect();
}
```

### Conexión y reconexión

```javascript
LiveClient.prototype.connect = function () {
    this.ws = new WebSocket(this.url);
    this.ws.onopen = function () {
        self.reconnectAttempt = 0;
        self.attachAll();          // re-attachea todo el DOM
        // drena la cola de mensajes pendientes
    };
    this.ws.onclose = function () {
        self._scheduleReconnect();
    };
};

LiveClient.prototype._scheduleReconnect = function () {
    var RECONNECT_BACKOFF = [250, 500, 1000, 2000, 5000, 10000];
    var delay = RECONNECT_BACKOFF[Math.min(this.reconnectAttempt, ...)];
    this.reconnectAttempt += 1;
    setTimeout(function () { self.connect(); }, delay);
};
```

El backoff exponencial tiene techo en 10 segundos. Si el WS se cierra por cualquier motivo, el cliente reintenta indefinidamente. Mientras no hay conexión, los mensajes se encolan en `this.queue` y se drenan en cuanto abre.

### `attachAll` y `attach`

```javascript
LiveClient.prototype.attachAll = function () {
    var roots = document.querySelectorAll("[data-hf-cid]");
    for (var i = 0; i < roots.length; i++) {
        this.attach(roots[i]);
    }
};

LiveClient.prototype.attach = function (el) {
    var cid = el.getAttribute("data-hf-cid");
    var name = el.getAttribute("data-hf-component");
    var props = JSON.parse(el.getAttribute("data-hf-props") || "{}");
    this.send({ t: "attach", cid: cid, name: name, props: props });
};
```

Lee los tres atributos del envelope HTML y envía el mensaje `attach`. En reconnect, el servidor recibe de nuevo todos los `attach` del DOM actual.

### `handle` — procesado de mensajes entrantes

```javascript
LiveClient.prototype.handle = function (msg) {
    switch (msg.t) {
        case "patch": this._applyPatch(msg); break;
        case "nav":   window.location.href = msg.url; break;
        case "err":
            console.error("[hotframe-live] server error", msg);
            document.dispatchEvent(new CustomEvent("hf:error", { detail: msg }));
            break;
        case "toast":
            document.dispatchEvent(new CustomEvent("hf:toast", { detail: msg }));
            break;
    }
};
```

`nav` es inmediato. `err` y `toast` se propagan como `CustomEvent` para que el proyecto los escuche con `document.addEventListener("hf:toast", ...)`.

### `_applyPatch` con morphdom

```javascript
LiveClient.prototype._applyPatch = function (msg) {
    var root = document.querySelector('[data-hf-cid="' + cssEscape(msg.cid) + '"]');
    if (!root) {
        this.detach(msg.cid);  // el nodo ya no existe — libera memoria en el server
        return;
    }
    var tmp = document.createElement(root.tagName);
    tmp.innerHTML = msg.html;
    window.morphdom(root, tmp, {
        childrenOnly: true,
        onBeforeElUpdated: function (fromEl, toEl) {
            // No actualizar el input que tiene el foco
            if (fromEl === document.activeElement && fromEl.tagName === "INPUT") {
                return false;
            }
            return true;
        },
    });
};
```

Puntos clave:
- Crea un elemento temporal con el mismo `tagName` que el root, inserta el HTML nuevo como `innerHTML`.
- Pasa `childrenOnly: true` a morphdom: parchea los hijos sin tocar el wrapper `data-hf-cid` (que debe mantenerse intacto).
- El hook `onBeforeElUpdated` preserva el elemento que tiene el foco — evita que morphdom borre el texto que el usuario está escribiendo.
- Si `root` no existe en el DOM, envía `detach` al servidor para liberar la instancia.

### Captura de eventos DOM → WS

`bindEvents` registra tres listeners en `document` con captura (`true` como tercer parámetro):

**Click** (`data-on:click="name:payload"`):
```javascript
document.addEventListener("click", function (e) {
    var target = e.target.closest("[data-on\\:click]");
    if (!target) return;
    var cid = findCid(target);
    var ev = parseEventValue(target.getAttribute("data-on:click"));
    self.send({ t: "event", cid: cid, n: ev.name, p: ev.payload });
}, true);
```

**Submit** (`form[data-on:submit="name"]`):
```javascript
document.addEventListener("submit", function (e) {
    var form = e.target.closest("form[data-on\\:submit]");
    e.preventDefault();
    var fd = new FormData(form);
    var data = {};
    fd.forEach(function (value, key) { data[key] = value; });
    self.send({ t: "event", cid: cid, n: ev.name, p: data });
}, true);
```
Serializa el `FormData` como dict plano. Campos múltiples colapsan al último valor.

**Input** (`data-bind="campo"`) con debounce 250 ms:
```javascript
document.addEventListener("input", function (e) {
    var field = el.getAttribute("data-bind");
    var key = cid + ":" + field;
    clearTimeout(self.bindTimers[key]);
    self.bindTimers[key] = setTimeout(function () {
        self.send({ t: "bind", cid: cid, f: field,
                    v: el.type === "checkbox" ? el.checked : el.value });
    }, 250);
}, true);
```

**Change** (selects y checkboxes) — inmediato, sin debounce.

### `findCid` — resolución de contexto

```javascript
function findCid(el) {
    var node = el;
    while (node && node !== document.body) {
        if (node.hasAttribute("data-hf-cid")) {
            return node.getAttribute("data-hf-cid");
        }
        node = node.parentNode;
    }
    return null;
}
```

Escala el DOM hacia arriba desde el elemento clickado hasta encontrar el `[data-hf-cid]` más cercano. Esto permite botones y inputs anidados arbitrariamente dentro del componente.

### `parseEventValue`

```javascript
function parseEventValue(raw) {
    var idx = raw.indexOf(":");
    if (idx === -1) return { name: raw, payload: null };
    return { name: raw.slice(0, idx), payload: raw.slice(idx + 1) };
}
```

`"toggle:42"` → `{name: "toggle", payload: "42"}`. `"add"` → `{name: "add", payload: null}`.

---

## El flujo de una interacción, por dentro

### 1. Cold-load (navegación a la página)

- La vista FastAPI retorna un `TemplateResponse`.
- Jinja2 renderiza la plantilla de forma síncrona.
- Encuentra `{% live "todo_list" user_id=42 %}`.
- `LiveExtension._render_live("todo_list", user_id=42)` se ejecuta:
  - Instancia `TodoList(user_id=42)` — **en memoria, sin cid todavía**.
  - Genera `cid = "c-7a3f4d2b91"` con `uuid.uuid4().hex[:12]`.
  - Llama `_run_async(instance.on_mount())` — en un loop nuevo si hace falta.
  - `on_mount` carga datos de DB y puebla `self.items`.
  - `render_initial_html(...)` llama `render_component_inner` + `wrap_with_envelope`.
  - El resultado es un `<div data-hf-cid="c-7a3f4d2b91" data-hf-component="todo_list" data-hf-props='{"user_id":42}'>...</div>`.
- El HTML completo (con el componente incrustado) viaja al navegador.

### 2. Attach (WS abierto)

- El navegador carga `live.js`, que en `DOMContentLoaded` llama `new LiveClient(LIVE_URL)`.
- `connect()` abre `WebSocket("/ws/_live")`.
- En `onopen`, `attachAll()` escanea `document.querySelectorAll("[data-hf-cid]")`.
- Por cada elemento, envía:
  ```json
  {"t":"attach","cid":"c-7a3f4d2b91","name":"todo_list","props":{"user_id":42}}
  ```
- En el servidor, `live_endpoint` recibe el mensaje y llama `session.handle_message`.
- `LiveSession._attach` instancia `TodoList(user_id=42)` de nuevo, corre `on_mount`, renderiza y envía:
  ```json
  {"t":"patch","cid":"c-7a3f4d2b91","html":"<ul>...</ul>"}
  ```
- El cliente recibe el patch. `_applyPatch` llama `morphdom(root, tmp, {childrenOnly: true})`.
- Como el HTML es idéntico al cold-load, morphdom no hace nada visible.

### 3. Evento (clic en checkbox)

- El usuario hace clic en `<input data-on:click="toggle:5">`.
- `bindEvents` captura el evento en la fase de captura.
- `findCid` sube el DOM hasta el `<div data-hf-cid="c-7a3f4d2b91">`.
- `parseEventValue("toggle:5")` → `{name: "toggle", payload: "5"}`.
- Envía:
  ```json
  {"t":"event","cid":"c-7a3f4d2b91","n":"toggle","p":"5"}
  ```
- El servidor llama `LiveSession._event`:
  - Obtiene la instancia de `self.components["c-7a3f4d2b91"]`.
  - Busca `TodoList._events["toggle"]` → la función decorada.
  - `async with self._locks["c-7a3f4d2b91"]`:
    - `_invoke_handler(handler, instance, "5")` — un argumento posicional.
    - `await instance.toggle("5")` ejecuta la lógica de negocio.
  - `_render_and_send(instance)` → nuevo HTML → `patch`.
- morphdom aplica el parche en el DOM.

### 4. Bind (escritura en input)

- El usuario escribe en `<input data-bind="new_text">`.
- El listener `input` espera 250 ms (debounce) y envía:
  ```json
  {"t":"bind","cid":"c-7a3f4d2b91","f":"new_text","v":"Buy milk"}
  ```
- `LiveSession._bind` hace `setattr(instance, "new_text", "Buy milk")`.
- Pydantic valida el tipo. **No hay re-render.**
- Cuando el usuario envía el formulario (`data-on:submit="add"`), el handler `add` ve `self.new_text == "Buy milk"` y re-renderiza.

---

## El wire protocol

### Tabla de referencia completa

| Tipo | Dir | Campo clave | Descripción |
|---|---|---|---|
| `attach` | C→S | `cid`, `name`, `props` | Registra una instancia. Corre `on_mount` y devuelve `patch`. |
| `event`  | C→S | `cid`, `n`, `p?` | Invoca un handler `@event`. Devuelve `patch`. |
| `bind`   | C→S | `cid`, `f`, `v` | Actualiza un campo de estado. Sin render. |
| `detach` | C→S | `cid` | Descarta la instancia. Corre `on_unmount`. |
| `patch`  | S→C | `cid`, `html` | HTML nuevo del componente. morphdom lo aplica. |
| `nav`    | S→C | `url` | `window.location.href = url`. |
| `err`    | S→C | `cid`, `msg`, `code?` | Error de handler. El WS sigue vivo. |
| `toast`  | S→C | `level`, `msg` | Notificación flash. Emite `hf:toast` en el DOM. |

### Códigos de error

| Código | Origen |
|---|---|
| `not_found` | Componente o evento desconocido. |
| `props` | Props inválidas en `attach`. |
| `no_class` | Entry sin clase asociada. |
| `mount` | `on_mount` lanzó excepción. |
| `not_attached` | Evento para un `cid` no attachado. |
| `protocol` | Mensaje malformado (sin `n` en event). |
| `handler` | El handler del evento lanzó excepción. |
| `internal` | Error no esperado en el dispatcher. |

---

## `on_mount`, ciclo de vida y el `asyncio.Lock` por cid

### El doble `on_mount`

`on_mount` se ejecuta dos veces en cada carga de página:

1. **Cold-load**: en `LiveExtension._render_live` mediante `_run_async`. Sirve para tener HTML útil en el HTML inicial (SEO, primer paint).
2. **Attach por WS**: en `LiveSession._attach`. El servidor no conserva la instancia del cold-load — la descarta al terminar de renderizar la respuesta HTTP.

Este diseño es deliberado. Citar del código:

> "Instantiate TodoList(user_id=42) de nuevo (el server no conserva la instancia del cold-load — es deliberado, así una reconexión funciona sin estado fantasma)."

### El Lock por cid

```python
self._locks: dict[str, asyncio.Lock] = {}
# ...
async with self._locks[cid]:
    await instance.on_mount()
    await self._render_and_send(instance)
```

Cada `cid` tiene su propio `asyncio.Lock`. Esto garantiza:

- Si dos eventos para el mismo componente llegan en ráfaga (doble clic, etc.), se procesan en orden FIFO.
- Los handlers pueden mutar `self` con seguridad total — no hay race conditions.
- Componentes distintos en la misma página procesan sus eventos en paralelo.

El lock también cubre `_bind`, de modo que un `bind` y un `event` simultáneos no colisionan.

### `on_unmount` y limpieza

`on_unmount` corre en tres escenarios:

1. `DetachMessage` del cliente (el componente sale del DOM).
2. `LiveSession.shutdown()` (el WS se cierra).
3. `LiveRuntime.shutdown()` (el servidor se apaga).

Es el lugar correcto para cancelar tareas asyncio internas, cerrar subscripciones a eventos, o hacer cleanup de recursos. No debe hacer IO de DB por defecto — el WS ya está cerrado.

---

## Gotchas

### Reconexión y estado fantasma

Cuando el WS cae (red inestable, reinicio del servidor, tab en background), el cliente reconecta con backoff. Al reconectar, envía `attach` para cada `[data-hf-cid]` visible en el DOM. El servidor instancia el componente de cero y corre `on_mount` otra vez.

**Consecuencia**: cualquier estado que no sea reconstruible desde `props + DB` se pierde en la reconexión. No guardes en `self`:
- Tareas asyncio (`asyncio.Task`).
- Handles de fichero o streams.
- IDs únicos generados en RAM.
- Contadores de sesión que no viven en DB.

### Multi-instancia / escalado horizontal

El estado de los componentes vive en `LiveSession.components`, que es un dict en RAM del proceso. Si escala a múltiples instancias de servidor con un load balancer, dos pestañas del mismo usuario pueden conectarse a procesos distintos y sus estados estarán desincronizados.

La documentación del código lo menciona explícitamente:

> "Sticky sessions implícitas: el state vive en RAM del proceso. Si vas a multi-instancia, hay que migrar `LiveSession.components` a Redis."

Hotframe v1.0 no incluye backend Redis. Para escalar, necesitas sticky sessions en el load balancer o una implementación externa de `LiveSession` con Redis pub/sub.

### El input con foco y morphdom

morphdom por defecto remplazaría el `<input>` aunque el usuario esté escribiendo en él. El hook `onBeforeElUpdated` de `live.js` devuelve `false` para el `activeElement` si es un input:

```javascript
onBeforeElUpdated: function (fromEl, toEl) {
    if (fromEl === document.activeElement && fromEl.tagName === "INPUT") {
        return false;
    }
    return true;
}
```

Esto significa que si el servidor envía un patch mientras el input activo tiene un valor diferente al del DOM, ese input no se actualiza. El mecanismo `data-bind` existe precisamente para sincronizar el estado al servidor sin depender del re-render.

### Props solo en el attach

Los props se serializan en el HTML inicial y el cliente los manda de vuelta en el `attach`. El servidor usa esos props para reconstruir la instancia. Si cambias los props de un componente en el servidor (algo inusual), el cliente no lo sabrá hasta la próxima reconexión o navegación.

### El tag `{% live %}` no tiene body

A diferencia de `{% component %}`, el tag `{% live %}` no acepta contenido tipo slot (`caller()`). Si necesitas inyectar contenido en un live component, pásalo como prop o usa el mecanismo de slots del framework para componentes stateless externos.

---

## Los tests

### `test_base.py`

Prueba que la tabla `_events` se construye al definir la subclase, que está aislada por clase, que métodos síncronos decorados con `@event` fallan en la definición, y que `render_context` y `extra_context` funcionan correctamente. También verifica que `validate_assignment` rechaza tipos incorrectos:

```python
def test_validate_assignment_rejects_wrong_type():
    c = Counter(start=0)
    with pytest.raises(ValidationError):
        c.value = "not-an-int"
```

### `test_session.py`

Los tests de sesión usan un `FakeWebSocket` que registra todos los `send_json` en una lista:

```python
class FakeWebSocket:
    async def send_json(self, payload) -> None:
        self.sent.append(payload)
```

Prueban el ciclo completo: attach → event → patch, validación de props, aislamiento entre sesiones, bind sin re-render, y que `shutdown` llama a `on_unmount` de todas las instancias.

### `test_diff.py`

Verifica que el envelope HTML contiene los atributos correctos y que los props se escapan adecuadamente para prevenir XSS:

```python
def test_wrap_with_envelope_props_are_html_escaped_json():
    inst = Sample(name='evil"<x>', count=1)
    html = wrap_with_envelope(...)
    assert "&quot;" in html
    assert "&lt;x&gt;" in html
```

---

## Cómo encaja con el resto del framework

| Componente | Relación con `live/` |
|---|---|
| `hotframe.bootstrap.create_app` | Crea `LiveRuntime`, monta `live_router`, sirve los estáticos `live.js` + `morphdom`. |
| `hotframe.components.ComponentRegistry` | El runtime consulta aquí las entradas de componentes por nombre. |
| `hotframe.components.entry.ComponentEntry` | Contiene el flag `is_live` y `props_cls` que el runtime necesita para instanciar. |
| `hotframe.views.responses.view` | Renderiza la página que incluye el cold-load del componente. No interactúa con el runtime directamente. |
| Jinja2 `Environment.globals` | `_hotframe_components` y `live_assets` son inyectados aquí por el bootstrap. |
| `hotframe.auth` | La autenticación a nivel WS se hace implícitamente mediante la cookie de sesión. Los handlers pueden hacer checks adicionales en `on_mount`. |
| Módulos dinámicos | Cuando un módulo se activa, sus componentes `LiveComponent` se registran en el `ComponentRegistry`. El `LiveRuntime` existente los encontrará en la siguiente llamada a `_attach`. |
