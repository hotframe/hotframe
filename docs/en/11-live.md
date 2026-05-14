# 11. The reactive runtime (live/)

> The `live/` module is the heart of hotframe's reactivity: it implements server-side stateful components, a component lifecycle, a JSON WebSocket protocol, and the JavaScript client that closes the loop in the browser. After reading this section you will understand exactly what happens from the moment Jinja renders `{% live %}` to the moment morphdom updates a DOM node.

---

## What this folder is for

`hotframe.live` allows the **state of a UI component to live in the Python process** rather than in the browser. The browser acts as a terminal: it renders HTML, captures user events, and applies patches. The server decides what HTML to produce at any given moment.

The model is intentionally simple:

1. The page renders the component in the initial HTML (cold-load).
2. The client opens a WebSocket and re-attaches every component it finds in the DOM.
3. User interactions (clicks, inputs, form submissions) travel as JSON messages over the WebSocket.
4. The server executes the handler, re-renders the template, and sends the new HTML as a patch.
5. `morphdom` applies it without losing focus or scroll position.

There is no JavaScript to write in your modules. No build step. No shared state between server and client to keep in sync.

---

## File map

| File | Responsibility |
|---|---|
| [`__init__.py`](../src/hotframe/live/__init__.py) | Re-exports public pieces; submodule entry point. |
| [`base.py`](../src/hotframe/live/base.py) | `LiveComponent` class: Pydantic model with identity, lifecycle, and event table. |
| [`decorators.py`](../src/hotframe/live/decorators.py) | `@event(name)` decorator and `get_event_name` function. |
| [`diff.py`](../src/hotframe/live/diff.py) | Template rendering + `data-hf-cid` envelope. |
| [`jinja_ext.py`](../src/hotframe/live/jinja_ext.py) | Jinja2 tag `{% live %}` for the cold-load. |
| [`protocol.py`](../src/hotframe/live/protocol.py) | Wire-format TypedDicts (C→S and S→C messages) + builder helpers. |
| [`session.py`](../src/hotframe/live/session.py) | `LiveSession`: aggregates instances per WebSocket, dispatches messages, manages locks. |
| [`runtime.py`](../src/hotframe/live/runtime.py) | `LiveRuntime`: per-app singleton, owns all active sessions. |
| [`ws.py`](../src/hotframe/live/ws.py) | FastAPI endpoint `/ws/_live` — the sole WebSocket entry point of the runtime. |
| [`assets.py`](../src/hotframe/live/assets.py) | Jinja2 global `live_assets()` that emits the `<script>` tags for `morphdom` and `live.js`. |
| [`static/live.js`](../src/hotframe/live/static/live.js) | JavaScript client: WS, event capture, debounced bind, morphdom patching. |
| [`static/morphdom.min.js`](../src/hotframe/live/static/morphdom.min.js) | Third-party library for efficient DOM diff/patch. Not analyzed here. |
| [`tests/test_base.py`](../src/hotframe/live/tests/test_base.py) | Tests for `LiveComponent`: event table, state mutation, render context. |
| [`tests/test_decorators.py`](../src/hotframe/live/tests/test_decorators.py) | Tests for the `@event` decorator. |
| [`tests/test_diff.py`](../src/hotframe/live/tests/test_diff.py) | Tests for internal rendering and the HTML envelope. |
| [`tests/test_protocol.py`](../src/hotframe/live/tests/test_protocol.py) | Tests for message builder helpers. |
| [`tests/test_session.py`](../src/hotframe/live/tests/test_session.py) | Integration tests for `LiveSession` with a fake WebSocket. |

---

## `__init__.py` — public re-exports

The file declares the public API of the submodule. Importing from `hotframe.live` gives direct access to:

```python
from hotframe.live import LiveComponent, event, LiveRuntime, LiveSession, live_router
```

The `get_runtime(app)` function is also exported so that the WS endpoint and Jinja extensions can retrieve the singleton without importing `runtime.py` directly.

---

## `base.py` — `LiveComponent`

### Purpose

`LiveComponent` is the base class for all reactive components. It extends Pydantic v2's `BaseModel` and adds:

1. **Identity** (`_cid`): an opaque string assigned by the runtime. It is the key by which the wire protocol and morphdom identify an instance.
2. **Lifecycle** (`on_mount`, `on_unmount`): coroutines that the runtime invokes at the right moments.
3. **Event table** (`_events`): a class-level dict built once per subclass in `__init_subclass__`, mapping event name → handler function.

### Class declaration

```python
class LiveComponent(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, validate_assignment=True)

    _events: ClassVar[dict[str, EventHandler]] = {}

    _cid: str = PrivateAttr(default="")
    _session: LiveSession | None = PrivateAttr(default=None)
    _component_name: str = PrivateAttr(default="")
    _last_html: str = PrivateAttr(default="")
```

`model_config` carries two important flags:
- `arbitrary_types_allowed=True`: allows state fields to hold ORM objects, dataclasses, etc., without Pydantic rejecting them.
- `validate_assignment=True`: every `self.x = value` runs the field's validators. This makes state mutations in handlers always safe — a field declared as `count: int` can never end up holding a string.

The `PrivateAttr` entries are instance attributes that Pydantic does not serialize in `model_dump()` or expose in the wire format. They are written directly as `instance._cid = "c-abc"`.

### `__init_subclass__` — building the event table

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

This is called **exactly once when the subclass is defined**, not on every instantiation. The MRO is traversed in reverse (from object down to the subclass) so that subclasses can override parent handlers. Every handler must be `async def`; if it is not, a `TypeError` is raised at class-definition time, not at runtime. Dispatch lookup is O(1): `instance.__class__._events["toggle"]`.

### Lifecycle

| Method | Signature | When it is called |
|---|---|---|
| `on_mount` | `async def on_mount(self) -> None` | After instantiation and before the first render. On cold-load (Jinja) and on every re-attach (WS opened / reconnect). |
| `on_unmount` | `async def on_unmount(self) -> None` | On client `detach`, WS close, or runtime shutdown. |

Both are no-ops by default. The important contract: **state must be reconstructable from `props` + DB each time `on_mount` is called**. If the WebSocket drops and reconnects, `on_mount` runs again.

### Handler helpers

Handlers can call two convenience coroutines:

```python
async def navigate(self, url: str) -> None:
    # Sends {"t": "nav", "url": url} to the client → window.location.href = url
    await self._session.send_nav(url)

async def toast(self, msg: str, level: str = "info") -> None:
    # Sends {"t": "toast", ...} → fires the hf:toast event on the DOM
    await self._session.send_toast(msg, level=level)
```

Both raise `RuntimeError` if called outside an active session (for example, from `on_mount` during cold-load).

### Render context

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

`render_context()` produces the dict passed to the Jinja template. By default it contains all Pydantic fields (props + state). If you need to expose derived values without adding them as fields, override `extra_context`:

```python
def extra_context(self) -> dict:
    return {"open_count": sum(1 for t in self.items if not t.done)}
```

---

## `decorators.py` — `@event`

### The marker

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

The decorator **does not wrap** the function: it stamps it with a private attribute and returns it unchanged. Zero call overhead. The wire-protocol name is completely independent of the Python method name.

### `get_event_name`

```python
def get_event_name(fn: Callable[..., Any]) -> str | None:
    return getattr(fn, _EVENT_NAME_ATTR, None)
```

Public function called by `__init_subclass__` when scanning the class's attributes. Returns `None` for undecorated methods.

### Usage example

```python
class TodoList(LiveComponent):
    @event("toggle")         # wire name = "toggle"
    async def toggle(self, todo_id: str) -> None:
        ...

    @event("add")            # wire name differs from the method name ("add")
    async def add_item(self) -> None:
        ...
```

---

## `diff.py` — rendering and HTML envelope

### Strategy

The module is deliberately simple: **no template diffing**. The component's full HTML is always emitted, and morphdom on the client applies the diff at the DOM level. The code comment says it plainly:

> "The MVP strategy is 'no diff' — we always emit the full component HTML and let morphdom on the client patch only what changed. This is simpler and far more debuggable than an AST tag-and-track scheme."

### `render_component_inner`

```python
def render_component_inner(
    env: Environment,
    entry: ComponentEntry,
    instance: LiveComponent,
) -> str:
```

Retrieves the template from the registry, calls `instance.render_context()` to build the context, and calls `template.render(**context)`. Returns the inner HTML — **without** the `data-hf-cid` wrapper. Template errors are logged and an empty string is returned (they never propagate).

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

Generates the `<div>` that wraps the component with the three attributes the client needs:

- `data-hf-cid`: instance ID, used to locate the DOM node when applying patches.
- `data-hf-component`: component name, used to re-instantiate on reconnect.
- `data-hf-props`: JSON of the original props (HTML-escaped), used to reconstruct the exact instance on re-attach.

Props are serialized with `json.dumps(raw, default=str, separators=(",", ":"))` and escaped with `html.escape(..., quote=True)` to be safe inside an HTML attribute.

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

Combines `render_component_inner` + `wrap_with_envelope` and returns a `Markup` object (Jinja2 will not double-escape the result). This is the function called by the `{% live %}` tag during the cold-load.

---

## `jinja_ext.py` — the `{% live %}` tag

### `LiveExtension`

```python
class LiveExtension(Extension):
    tags = {"live"}
```

Registered on the Jinja2 `Environment` during bootstrap. When the parser encounters `{% live "name" k=v ... %}` it builds a call to `_render_live`.

### `_render_live`

```python
def _render_live(self, __component_name__: str, /, **props) -> Markup:
```

Steps it executes:

1. Retrieves the `ComponentRegistry` from the Jinja environment (`env.globals["_hotframe_components"]`).
2. Looks up the component entry; verifies it is a `LiveComponent` (`entry.is_live == True`).
3. Instantiates the class with the props: `instance = cls(**props)`.
4. Generates the `cid`: `instance._cid = f"c-{uuid.uuid4().hex[:12]}"`.
5. Calls `_run_async(instance.on_mount())` to execute the mount hook in a synchronous context.
6. Calls `render_initial_html(env, entry, instance, prop_names=...)` and returns the `Markup`.

### `_run_async` — the sync/async bridge

Jinja2 renders templates synchronously, but `on_mount` may be a coroutine with awaits. `_run_async` handles both scenarios:

```python
def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        return asyncio.run(coro)

    # A loop is already running (tests): use a separate thread
    import threading
    result_box: dict[str, object] = {}
    def runner() -> None:
        result_box["v"] = asyncio.run(coro)
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    return result_box.get("v")
```

In production (FastAPI/Starlette), the template is rendered outside the event loop, so `asyncio.run()` works directly. In tests where a loop is already running, a daemon thread is spawned to avoid a deadlock.

---

## `protocol.py` — the wire protocol

### Design philosophy

Messages are `TypedDict` (not Pydantic models) to avoid validation overhead in the WebSocket hot path. The discriminator `t` is a single-character string. Field names are short for the same reason.

### Client → Server messages

#### `AttachMessage`
```python
class AttachMessage(TypedDict):
    t: Literal["attach"]
    cid: str    # ID generated during cold-load
    name: str   # component name
    props: dict[str, Any]  # original props
```
Sent when the WS opens (and on every reconnect) for each `[data-hf-cid]` in the DOM.

#### `EventMessage`
```python
class EventMessage(TypedDict, total=False):
    t: Literal["event"]
    cid: str
    n: str      # event wire name
    p: Any      # payload (optional)
```
`total=False` makes `p` optional. For simple clicks, `p` is a string or `null`. For forms, `p` is a dict (the serialized `FormData`).

#### `BindMessage`
```python
class BindMessage(TypedDict):
    t: Literal["bind"]
    cid: str
    f: str      # field name
    v: Any      # new value
```
Sent by the client with a 250 ms debounce when typing into `<input data-bind="field">`. The server updates the field but **does not re-render**.

#### `DetachMessage`
```python
class DetachMessage(TypedDict):
    t: Literal["detach"]
    cid: str
```
Sent when the component leaves the DOM. The server calls `on_unmount` and frees memory.

### Server → Client messages

#### `PatchMessage`
```python
class PatchMessage(TypedDict):
    t: Literal["patch"]
    cid: str
    html: str   # inner HTML of the component (without the data-hf-cid wrapper)
```

#### `NavMessage`
```python
class NavMessage(TypedDict):
    t: Literal["nav"]
    url: str
```
The client executes `window.location.href = url`.

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
The client dispatches `CustomEvent("hf:toast", {detail: msg})`. The toast UI is implemented by the project.

### Builder helpers

```python
make_patch(cid, html) -> PatchMessage
make_nav(url)         -> NavMessage
make_err(cid, msg, code=None) -> ErrMessage
make_toast(msg, level="info") -> ToastMessage
```

Centralized so that no code outside `protocol.py` constructs envelopes by hand.

---

## `session.py` — `LiveSession`

### Structure

```python
class LiveSession:
    def __init__(self, session_id: str, ws: WebSocket, runtime: LiveRuntime) -> None:
        self.id = session_id
        self.ws = ws
        self.runtime = runtime
        self.components: dict[str, LiveComponent] = {}   # cid -> instance
        self._locks: dict[str, asyncio.Lock] = {}        # cid -> Lock
        self._closed = False
```

A `LiveSession` is the live unit per WebSocket. It holds **all** component instances for that browser tab.

### `handle_message`

The main dispatcher:

```python
async def handle_message(self, msg: ClientMessage) -> None:
    t = msg.get("t")
    if t == "attach":  await self._attach(msg)
    elif t == "event": await self._event(msg)
    elif t == "bind":  await self._bind(msg)
    elif t == "detach": await self._detach(msg)
    else: logger.warning(...)
```

Unknown types are logged and discarded. Handler errors are caught and reported as `err` envelopes without closing the WS.

### `_attach`

1. Resolves the `ComponentEntry` in the registry.
2. Instantiates the class with the props.
3. Stamps `_cid`, `_session`, `_component_name` on the instance.
4. Registers it in `self.components[cid]` and creates `self._locks[cid] = asyncio.Lock()`.
5. Under the lock: calls `await instance.on_mount()`.
6. Calls `await self._render_and_send(instance)`.

### `_event`

```python
async def _event(self, msg: EventMessage) -> None:
    handler = instance.__class__._events.get(wire_name)
    async with self._locks[cid]:
        await _invoke_handler(handler, instance, payload)
        await self._render_and_send(instance)
```

The lock guarantees that if two events for the same `cid` arrive nearly simultaneously, the second waits for the first to finish. Different components run in parallel (independent locks).

### `_bind`

```python
async def _bind(self, msg: BindMessage) -> None:
    async with self._locks[cid]:
        setattr(instance, field, value)  # Pydantic validates here
    # No re-render
```

`validate_assignment=True` on `LiveComponent` means `setattr` runs through Pydantic validators. If the value is invalid, a warning is logged but no `err` is sent to the client (it is a silent operation).

### `_render_and_send`

```python
async def _render_and_send(self, instance: LiveComponent) -> None:
    html = render_component_inner(self.runtime.env, entry, instance)
    instance._last_html = html
    await self._send(make_patch(instance.cid, html))
```

Always performs a full re-render. `_last_html` is stored on the instance (available for debugging).

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

If the WS no longer accepts messages, the session is marked as closed rather than propagating the exception.

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

Idempotent. Called on `WebSocketDisconnect`, clean close, and runtime shutdown.

### `_invoke_handler` — signature adapter

```python
async def _invoke_handler(handler, instance, payload) -> None:
```

Supports three signature forms for maximum ergonomics:

1. No extra arguments: `await handler(instance)` — ignores the payload.
2. One positional argument: `await handler(instance, payload)` — useful for `toggle(self, todo_id: str)`.
3. A dict unpacked as kwargs: `await handler(instance, **payload)` — for forms where the payload is `{"new_text": "..."}`.

Detection is done by signature inspection (`inspect.signature`) on each call.

---

## `runtime.py` — `LiveRuntime`

### Structure

```python
class LiveRuntime:
    def __init__(self, registry: ComponentRegistry, env: Environment) -> None:
        self.registry = registry
        self.env = env
        self.sessions: dict[str, LiveSession] = {}
```

Per-app singleton. Bootstrap creates it during lifespan startup and stores it in `app.state.live`.

### Session management

```python
async def open_session(self, session_id: str, ws: WebSocket) -> LiveSession:
    existing = self.sessions.pop(session_id, None)
    if existing is not None:
        await existing.shutdown()  # close orphaned session
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

`open_session` actively closes any existing session with the same ID that had not yet detected its dead WS. This enables clean reconnects.

### `get_runtime`

```python
def get_runtime(app) -> LiveRuntime:
    runtime = getattr(app.state, "live", None)
    if runtime is None:
        raise RuntimeError(...)
    return runtime
```

Helper used by the WS endpoint and the Jinja extension. Fails loudly if bootstrap did not initialize the runtime.

---

## `ws.py` — the `/ws/_live` endpoint

### Resolving the session_id

```python
def _resolve_session_id(ws: WebSocket) -> str:
```

Lookup order:
1. Known key in `ws.session` (from `SessionMiddleware`): `user_id`, `session_id`, or `sid`.
2. Raw `session` cookie.
3. Client `host:port`.
4. `f"anon:{id(ws)}"` as a last resort.

Stability across reconnects matters: if the ID matches an orphaned session, `open_session` closes it first.

### The endpoint

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

All complexity lives in `session.handle_message`. The endpoint is a thin wrapper: accept, open session, receive loop, close.

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

Jinja2 global that emits the two required `<script>` tags. It propagates the `csp_nonce` from the request context so that the scripts pass a strict Content Security Policy. Called once in the base template's `<head>`:

```jinja
{{ live_assets() }}
```

---

## The `live.js` client

`live.js` is an IIFE (Immediately Invoked Function Expression) that initializes on `DOMContentLoaded`. It exposes `window.hotframeLive` for debugging and dispatches `CustomEvent("hf:ready")` on the document.

### `LiveClient` — the core class

```javascript
function LiveClient(url) {
    this.url = url;
    this.ws = null;
    this.queue = [];          // messages queued while disconnected
    this.bindTimers = {};     // "${cid}:${field}" -> timeout id (debounce)
    this.reconnectAttempt = 0;
    this.connect();
}
```

### Connection and reconnection

```javascript
LiveClient.prototype.connect = function () {
    this.ws = new WebSocket(this.url);
    this.ws.onopen = function () {
        self.reconnectAttempt = 0;
        self.attachAll();          // re-attach everything in the DOM
        // drain the pending message queue
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

The exponential backoff is capped at 10 seconds. If the WS closes for any reason, the client retries indefinitely. While disconnected, messages are enqueued in `this.queue` and drained as soon as the connection opens.

### `attachAll` and `attach`

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

Reads the three attributes from the HTML envelope and sends the `attach` message. On reconnect, the server receives all `attach` messages for the current DOM again.

### `handle` — processing incoming messages

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

`nav` is immediate. `err` and `toast` are propagated as `CustomEvent` so the project can listen with `document.addEventListener("hf:toast", ...)`.

### `_applyPatch` with morphdom

```javascript
LiveClient.prototype._applyPatch = function (msg) {
    var root = document.querySelector('[data-hf-cid="' + cssEscape(msg.cid) + '"]');
    if (!root) {
        this.detach(msg.cid);  // node no longer exists — free server-side memory
        return;
    }
    var tmp = document.createElement(root.tagName);
    tmp.innerHTML = msg.html;
    window.morphdom(root, tmp, {
        childrenOnly: true,
        onBeforeElUpdated: function (fromEl, toEl) {
            // Do not update the input that has focus
            if (fromEl === document.activeElement && fromEl.tagName === "INPUT") {
                return false;
            }
            return true;
        },
    });
};
```

Key points:
- Creates a temporary element with the same `tagName` as the root and sets the new HTML as `innerHTML`.
- Passes `childrenOnly: true` to morphdom: patches children without touching the `data-hf-cid` wrapper (which must remain intact).
- The `onBeforeElUpdated` hook preserves the element that has focus — it prevents morphdom from overwriting text the user is currently typing.
- If `root` no longer exists in the DOM, sends `detach` to the server to free the instance.

### Capturing DOM events → WS

`bindEvents` registers three listeners on `document` with capture (`true` as the third parameter):

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
Serializes `FormData` as a plain dict. Multiple fields with the same name collapse to the last value.

**Input** (`data-bind="field"`) with 250 ms debounce:
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

**Change** (selects and checkboxes) — immediate, no debounce.

### `findCid` — context resolution

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

Walks the DOM upward from the clicked element until it finds the nearest `[data-hf-cid]`. This supports buttons and inputs nested arbitrarily deep inside the component.

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

## A full interaction walk-through

### 1. Cold-load (navigating to the page)

- The FastAPI view returns a `TemplateResponse`.
- Jinja2 renders the template synchronously.
- It encounters `{% live "todo_list" user_id=42 %}`.
- `LiveExtension._render_live("todo_list", user_id=42)` executes:
  - Instantiates `TodoList(user_id=42)` — **in memory, no cid yet**.
  - Generates `cid = "c-7a3f4d2b91"` with `uuid.uuid4().hex[:12]`.
  - Calls `_run_async(instance.on_mount())` — in a new loop if necessary.
  - `on_mount` loads data from the DB and populates `self.items`.
  - `render_initial_html(...)` calls `render_component_inner` + `wrap_with_envelope`.
  - The result is `<div data-hf-cid="c-7a3f4d2b91" data-hf-component="todo_list" data-hf-props='{"user_id":42}'>...</div>`.
- The full HTML (with the embedded component) travels to the browser.

### 2. Attach (WS opened)

- The browser loads `live.js`, which calls `new LiveClient(LIVE_URL)` on `DOMContentLoaded`.
- `connect()` opens `WebSocket("/ws/_live")`.
- On `onopen`, `attachAll()` scans `document.querySelectorAll("[data-hf-cid]")`.
- For each element it sends:
  ```json
  {"t":"attach","cid":"c-7a3f4d2b91","name":"todo_list","props":{"user_id":42}}
  ```
- On the server, `live_endpoint` receives the message and calls `session.handle_message`.
- `LiveSession._attach` instantiates `TodoList(user_id=42)` again, runs `on_mount`, renders, and sends:
  ```json
  {"t":"patch","cid":"c-7a3f4d2b91","html":"<ul>...</ul>"}
  ```
- The client receives the patch. `_applyPatch` calls `morphdom(root, tmp, {childrenOnly: true})`.
- Since the HTML is identical to the cold-load, morphdom makes no visible changes.

### 3. Event (clicking a checkbox)

- The user clicks `<input data-on:click="toggle:5">`.
- `bindEvents` captures the event during the capture phase.
- `findCid` walks the DOM up to `<div data-hf-cid="c-7a3f4d2b91">`.
- `parseEventValue("toggle:5")` → `{name: "toggle", payload: "5"}`.
- Sends:
  ```json
  {"t":"event","cid":"c-7a3f4d2b91","n":"toggle","p":"5"}
  ```
- The server calls `LiveSession._event`:
  - Retrieves the instance from `self.components["c-7a3f4d2b91"]`.
  - Looks up `TodoList._events["toggle"]` → the decorated function.
  - `async with self._locks["c-7a3f4d2b91"]`:
    - `_invoke_handler(handler, instance, "5")` — one positional argument.
    - `await instance.toggle("5")` executes the business logic.
  - `_render_and_send(instance)` → new HTML → `patch`.
- morphdom applies the patch to the DOM.

### 4. Bind (typing into an input)

- The user types in `<input data-bind="new_text">`.
- The `input` listener waits 250 ms (debounce) and sends:
  ```json
  {"t":"bind","cid":"c-7a3f4d2b91","f":"new_text","v":"Buy milk"}
  ```
- `LiveSession._bind` calls `setattr(instance, "new_text", "Buy milk")`.
- Pydantic validates the type. **No re-render.**
- When the user submits the form (`data-on:submit="add"`), the `add` handler sees `self.new_text == "Buy milk"` and re-renders.

---

## The wire protocol

### Complete reference table

| Type | Dir | Key field | Description |
|---|---|---|---|
| `attach` | C→S | `cid`, `name`, `props` | Registers an instance. Runs `on_mount` and returns `patch`. |
| `event`  | C→S | `cid`, `n`, `p?` | Invokes a `@event` handler. Returns `patch`. |
| `bind`   | C→S | `cid`, `f`, `v` | Updates a state field. No render. |
| `detach` | C→S | `cid` | Discards the instance. Runs `on_unmount`. |
| `patch`  | S→C | `cid`, `html` | New component HTML. morphdom applies it. |
| `nav`    | S→C | `url` | `window.location.href = url`. |
| `err`    | S→C | `cid`, `msg`, `code?` | Handler error. The WS stays open. |
| `toast`  | S→C | `level`, `msg` | Flash notification. Dispatches `hf:toast` on the DOM. |

### Error codes

| Code | Origin |
|---|---|
| `not_found` | Unknown component or event. |
| `props` | Invalid props in `attach`. |
| `no_class` | Entry has no associated class. |
| `mount` | `on_mount` raised an exception. |
| `not_attached` | Event for an un-attached `cid`. |
| `protocol` | Malformed message (missing `n` in event). |
| `handler` | The event handler raised an exception. |
| `internal` | Unexpected error in the dispatcher. |

---

## `on_mount`, lifecycle, and the per-cid `asyncio.Lock`

### The double `on_mount`

`on_mount` executes twice on every page load:

1. **Cold-load**: in `LiveExtension._render_live` via `_run_async`. Ensures useful HTML is present in the initial response (SEO, first paint).
2. **WS attach**: in `LiveSession._attach`. The server does not retain the cold-load instance — it is discarded once the HTTP response finishes rendering.

This design is intentional. From the code:

> "Instantiate TodoList(user_id=42) again (the server does not keep the cold-load instance — this is deliberate, so a reconnect works without phantom state)."

### The per-cid lock

```python
self._locks: dict[str, asyncio.Lock] = {}
# ...
async with self._locks[cid]:
    await instance.on_mount()
    await self._render_and_send(instance)
```

Each `cid` has its own `asyncio.Lock`. This guarantees:

- If two events for the same component arrive in a burst (double-click, etc.), they are processed in FIFO order.
- Handlers can mutate `self` with complete safety — no race conditions.
- Different components on the same page process their events in parallel.

The lock also covers `_bind`, so a simultaneous `bind` and `event` cannot collide.

### `on_unmount` and cleanup

`on_unmount` runs in three scenarios:

1. `DetachMessage` from the client (the component leaves the DOM).
2. `LiveSession.shutdown()` (the WS closes).
3. `LiveRuntime.shutdown()` (the server shuts down).

This is the right place to cancel internal asyncio tasks, close event subscriptions, or clean up resources. It should not perform DB I/O by default — the WS is already closed.

---

## Gotchas

### Reconnection and phantom state

When the WS drops (unstable network, server restart, background tab), the client reconnects with backoff. On reconnect, it sends `attach` for every `[data-hf-cid]` visible in the DOM. The server instantiates the component from scratch and runs `on_mount` again.

**Consequence**: any state that cannot be reconstructed from `props + DB` is lost on reconnect. Do not store in `self`:
- asyncio tasks (`asyncio.Task`).
- File handles or streams.
- Unique IDs generated in RAM.
- Session counters that do not live in the DB.

### Multi-instance / horizontal scaling

Component state lives in `LiveSession.components`, which is an in-process dict. If you scale to multiple server instances behind a load balancer, two tabs from the same user may connect to different processes and their states will be out of sync.

The code documentation states this explicitly:

> "Implicit sticky sessions: state lives in process RAM. For multi-instance deployments, `LiveSession.components` must be migrated to Redis."

Hotframe v1.0 does not include a Redis backend. To scale out you need sticky sessions on the load balancer or an external `LiveSession` implementation backed by Redis pub/sub.

### The focused input and morphdom

By default, morphdom would replace an `<input>` even while the user is typing in it. The `onBeforeElUpdated` hook in `live.js` returns `false` for the `activeElement` if it is an input:

```javascript
onBeforeElUpdated: function (fromEl, toEl) {
    if (fromEl === document.activeElement && fromEl.tagName === "INPUT") {
        return false;
    }
    return true;
}
```

This means that if the server sends a patch while the active input holds a different value than the DOM, that input is not updated. The `data-bind` mechanism exists precisely to sync state to the server without depending on a re-render.

### Props only on attach

Props are serialized into the initial HTML and the client sends them back in the `attach` message. The server uses those props to reconstruct the instance. If you change a component's props server-side (uncommon), the client will not know until the next reconnect or navigation.

### The `{% live %}` tag has no body

Unlike `{% component %}`, the `{% live %}` tag does not accept slot-style content (`caller()`). If you need to inject content into a live component, pass it as a prop or use the framework's slot mechanism for stateless external components.

---

## The tests

### `test_base.py`

Verifies that the `_events` table is built when the subclass is defined, that it is isolated per class, that synchronous methods decorated with `@event` fail at definition time, and that `render_context` and `extra_context` work correctly. Also checks that `validate_assignment` rejects incorrect types:

```python
def test_validate_assignment_rejects_wrong_type():
    c = Counter(start=0)
    with pytest.raises(ValidationError):
        c.value = "not-an-int"
```

### `test_session.py`

Session tests use a `FakeWebSocket` that records all `send_json` calls in a list:

```python
class FakeWebSocket:
    async def send_json(self, payload) -> None:
        self.sent.append(payload)
```

They exercise the full cycle: attach → event → patch, props validation, session isolation, bind without re-render, and that `shutdown` calls `on_unmount` on all instances.

### `test_diff.py`

Verifies that the HTML envelope contains the correct attributes and that props are properly escaped to prevent XSS:

```python
def test_wrap_with_envelope_props_are_html_escaped_json():
    inst = Sample(name='evil"<x>', count=1)
    html = wrap_with_envelope(...)
    assert "&quot;" in html
    assert "&lt;x&gt;" in html
```

---

## How it fits into the rest of the framework

| Component | Relationship with `live/` |
|---|---|
| `hotframe.bootstrap.create_app` | Creates `LiveRuntime`, mounts `live_router`, serves `live.js` + `morphdom` statics. |
| `hotframe.components.ComponentRegistry` | The runtime looks up component entries by name here. |
| `hotframe.components.entry.ComponentEntry` | Carries the `is_live` flag and `props_cls` that the runtime needs to instantiate. |
| `hotframe.views.responses.view` | Renders the page that includes the component cold-load. Does not interact with the runtime directly. |
| Jinja2 `Environment.globals` | `_hotframe_components` and `live_assets` are injected here by bootstrap. |
| `hotframe.auth` | WS-level authentication is handled implicitly via the session cookie. Handlers can perform additional checks in `on_mount`. |
| Dynamic modules | When a module is activated, its `LiveComponent` classes are registered in the `ComponentRegistry`. The existing `LiveRuntime` will find them on the next `_attach` call. |
