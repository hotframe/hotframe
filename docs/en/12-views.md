# 12. Views and responses (views/)

> The `views/` folder provides the three output layers that are not real-time reactivity: the `@view` decorator for rendering full HTML pages, response helpers for redirects and flash messages, and the `BroadcastHub` for pushing real-time updates via SSE and WebSocket to multiple clients simultaneously.

---

## What this folder is for

`hotframe.views` is the API for standard HTTP routes and for broadcasting. Its responsibility is split into two main blocks:

1. **`responses.py`**: the `@view` decorator (and its alias `htmx_view`), convention-based template resolution, authentication, permissions, and HTTP response helpers (`reactive_redirect`, `reactive_refresh`, `reactive_message`, etc.).

2. **`broadcast.py`**: `BroadcastHub` — an `asyncio.Queue`-based fan-out system for broadcasting messages to all SSE or WebSocket clients subscribed to a topic. Includes three FastAPI endpoints: per-topic SSE, multiplexed SSE, and a WebSocket alternative.

The folder does not implement component reactivity — that is the job of `live/`. When a route needs to push a UI update in response to a server-side event (for example, a new task created by another user), it uses `BroadcastHub.publish()`. When a component needs its own reactivity, it uses `LiveComponent`.

---

## File map

| File | Responsibility |
|---|---|
| [`__init__.py`](../src/hotframe/views/__init__.py) | Re-exports `BroadcastHub` and helpers; documents the public API. |
| [`responses.py`](../src/hotframe/views/responses.py) | `@view` decorator, HTTP response helpers, and generic SSE. |
| [`broadcast.py`](../src/hotframe/views/broadcast.py) | `BroadcastHub`, `get_broadcast_hub`, `broadcast_router` with SSE and WS endpoints. |
| [`tests/test_responses.py`](../src/hotframe/views/tests/test_responses.py) | Tests for the `@view` decorator, response helpers, and `BroadcastHub`. |

---

## `__init__.py` — re-exports

The module is intentionally minimal. It documents the recommended imports:

```python
from hotframe.views.broadcast import BroadcastHub, broadcast_router, get_broadcast_hub
```

`@view` is imported directly from `hotframe.views.responses` or, more commonly, from `hotframe` (the bootstrap re-exports the decorator in the root namespace).

---

## `responses.py` — the `@view` decorator and HTTP helpers

### `view` — the central decorator

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

The decorator wraps an async FastAPI view function and adds four responsibilities:

#### 1. Authentication

```python
if login_required:
    user_id = get_session_user_id(request)
    if user_id is None:
        return RedirectResponse(settings.AUTH_LOGIN_URL, status_code=302)
```

If there is no user in the session, redirects to `settings.AUTH_LOGIN_URL` with a 302. If `login_required=False`, the view is public.

#### 2. Permissions

```python
if permissions:
    user_perms = await _resolve_permissions(request, user_id)
    request.state.user_permissions = user_perms  # cached on the request
    if not all(has_permission(user_perms, p) for p in permissions):
        return RedirectResponse(settings.AUTH_UNAUTHORIZED_URL, status_code=302)
```

Permissions are resolved by calling the `PERMISSION_RESOLVER` configured in settings (a dotted path `"myapp.auth.get_permissions"`). The result is cached in `request.state.user_permissions` to avoid multiple calls per request.

The `permissions` argument accepts either a string or a list: `permissions="dashboard.view"` is normalized to `["dashboard.view"]` at the start of the decorator.

#### 3. Template resolution

If `module_id` and `view_id` are provided, the decorator searches for the template under multiple conventions, in this order:

For the partial:
```
{module}/partials/{view}_content.html
{module}/partials/{view}.html
{module}/partials/{view}_list.html
{module}/partials/{view}_form.html
```

For the full page:
```
{module}/pages/{view}.html
{module}/pages/{view}_list.html
{module}/pages/{view}_form.html
{module}/pages/list.html
{module}/pages/index.html
```

The search is implemented in `_resolve_template`, which is decorated with `@lru_cache(maxsize=512)` using an `env_id` as the cache key. This avoids repeated `env.get_template` calls on every request.

Special case: if `view_id == "dashboard"`, it also tries `{module}/pages/index.html` as the first candidate.

If the view's return dict includes a `"template"` key, that value overrides the resolved template.

#### 4. Rendering

```python
return _render_full(templates, request, merged, _full, _partial)
```

`_render_full` builds the merged context (`global_ctx` + whatever the view returned), adds `content_template` to the context, and calls `templates.TemplateResponse(request, tpl_name, context)`. If rendering fails, it returns an `HTMLResponse` with status 500 containing the embedded error message.

#### Usage example

```python
from fastapi import APIRouter, Request
from hotframe import view

router = APIRouter()

@router.get("/dashboard")
@view(module_id="shared", view_id="dashboard", permissions="dashboard.view")
async def dashboard(request: Request):
    return {"items": await load_items()}
```

The view returns a dict. `@view` handles everything else.

If the view returns a `Response` object directly, the decorator passes it through unchanged:

```python
@router.post("/action")
@view(module_id="shared", view_id="action", login_required=True)
async def action(request: Request):
    return reactive_redirect("/success")  # Response passed straight through
```

### `htmx_view` — the alias

```python
htmx_view = view
```

They are exactly the same. `htmx_view` exists for historical naming reasons. All new code can use `view` interchangeably.

### `is_reactive_request` and `is_htmx_request`

```python
def is_reactive_request(request: Request) -> bool:
    return False

def is_htmx_request(request: Request) -> bool:
    return is_reactive_request(request)
```

Both always return `False`. Reactivity in hotframe goes over WebSocket, not HTTP headers. These functions exist to maintain compatibility with code that branches on them, but they have no real effect.

### HTTP response helpers

#### `reactive_redirect(url: str) -> Response`

```python
def reactive_redirect(url: str) -> Response:
    return RedirectResponse(url, status_code=303)
```

303 See Other is the correct status code after a successful POST (prevents form re-submission on page refresh). Alias: `htmx_redirect`.

#### `reactive_refresh() -> Response`

```python
def reactive_refresh() -> Response:
    return HTMLResponse('<meta http-equiv="refresh" content="0">', status_code=200)
```

Reloads the current page via an HTML meta-refresh. Alias: `htmx_refresh`.

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

Returns an HTML fragment containing a `<script>` that dispatches a `CustomEvent` on `window`. Useful for non-live routes that need to notify DOM listeners.

```python
# In the view:
return reactive_trigger("cartUpdated", count=5)

# In the template (or in other JS):
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

Returns an HTML fragment containing a toast. The text is escaped manually (does not use `markupsafe` because this is a simple helper). The level and text come from server-side code, not from user input.

#### `add_message(request, level, text)`

```python
def add_message(request: Request, level: str, text: str) -> None:
    if not hasattr(request.state, "_messages"):
        request.state._messages = []
    request.state._messages.append({"level": level, "text": text})
```

Adds a flash message to the request state. The flash middleware reads it on the next full-page response. `LiveComponent` instances should not use this: they have `self.toast(...)`, which is more efficient.

#### `htmx_trigger(event, data=None) -> dict`

```python
def htmx_trigger(event: str, data: dict | None = None) -> dict:
    if data:
        return {event: data}
    return {event: True}
```

This helper is different: it returns a **dict**, not a `Response`. It is a compatibility helper that builds the payload for a trigger header. In modern hotframe, `reactive_trigger` is preferred.

### `sse_stream` — generic SSE

```python
async def sse_stream(
    request: Request,
    generator: AsyncGenerator[dict[str, Any] | str, None],
    *,
    event_type: str = "message",
    ping_interval: int = 15,
) -> EventSourceResponse:
```

Wraps any async generator as an SSE response. Useful for progress streams, log streaming, or any unidirectional push that does not require fan-out. Unlike `BroadcastHub`, there are no multiple subscribers — it is a 1:1 stream per request.

Error handling: if the generator raises an exception, it emits an `"error"` event with the message and a `"done"` event when it finishes. If the client disconnects (`request.is_disconnected()`), the stream is cleanly terminated.

### Permission resolution

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

The resolver is an async function in the project, configured in `settings.PERMISSION_RESOLVER` as a dotted path. It receives the `request` and the `user_id` and returns a list of permission strings. If not configured, all authenticated users have access.

---

## `broadcast.py` — `BroadcastHub` and endpoints

### Architecture

The module comment illustrates it perfectly:

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

Each connected SSE client has its own `asyncio.Queue`. When a message is published to a topic, it is copied into all queues for that topic.

### `BroadcastHub`

```python
class BroadcastHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
```

A dict of `topic -> set of queues`. No explicit locks are needed because asyncio is single-threaded.

#### `subscribe(topic: str) -> asyncio.Queue`

```python
async def subscribe(self, topic: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    self._subscribers[topic].add(queue)
    return queue
```

Creates a queue with a capacity of 64. The maximum size protects against slow clients that don't consume. Returns the queue to the caller, which uses it in an `await queue.get()` loop.

#### `unsubscribe(topic: str, queue: asyncio.Queue)`

```python
async def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
    self._subscribers[topic].discard(queue)
    if not self._subscribers[topic]:
        del self._subscribers[topic]
```

Removes the queue from the set. If the set becomes empty, deletes the topic entry (prevents memory leaks from inactive topics).

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

Uses `put_nowait` — the publisher is never blocked even if a client is saturated. Full queues are marked as `stale` and automatically removed: a client that stops consuming is deregistered on its own. Returns the number of clients that received the message.

#### Inspection methods

```python
def topic_count(self) -> int:
    return len(self._subscribers)

def subscriber_count(self, topic: str) -> int:
    return len(self._subscribers.get(topic, set()))
```

Useful for admin dashboards or metrics.

### `get_broadcast_hub(request)`

```python
def get_broadcast_hub(request: Request) -> BroadcastHub:
    return request.app.state.broadcast_hub
```

The `BroadcastHub` singleton lives on `app.state.broadcast_hub`, placed there by the bootstrap. Access it via this helper in views and handlers.

### `broadcast_router` — the three endpoints

The router is mounted by the bootstrap. Its routes are:

#### `GET /stream/{topic:path}` — per-topic SSE

```python
@broadcast_router.get("/stream/{topic:path}")
async def stream_topic(request, topic, user: CurrentUser) -> Response:
```

Requires authentication (via `CurrentUser`). Creates a queue with `hub.subscribe(topic)`, opens an `EventSourceResponse`, and sends messages with `yield {"event": "message", "data": data}`. Every 30 seconds it checks for disconnection using `asyncio.wait_for(..., timeout=30.0)`. On disconnect (or loop closure), calls `hub.unsubscribe`.

The ping is configured at 15 seconds (`EventSourceResponse(..., ping=15)`) to keep the connection alive through proxies.

#### `GET /stream/_mux?topics=a,b,c` — multiplexed SSE

```python
@broadcast_router.get("/stream/_mux")
async def stream_multiplexed(request, user: CurrentUser, topics: str = "") -> Response:
```

A single SSE connection for multiple topics. The client passes `topics=a,b,c` as a query parameter. Queues are created for each topic and `asyncio.wait(..., return_when=FIRST_COMPLETED)` is used to wait on all of them:

```python
done, pending = await asyncio.wait(
    [asyncio.create_task(_wait_queue(t, q)) for t, q in queues],
    timeout=30.0,
    return_when=asyncio.FIRST_COMPLETED,
)
```

Messages are emitted as `{"event": topic_name, "data": data}`, allowing the client to identify which topic each message came from via the SSE `event` field.

Validation: if `topic_list` is empty, responds with 400 immediately before opening any queue.

#### `POST /stream/{topic:path}` — publish from the browser

```python
@broadcast_router.post("/stream/{topic:path}")
async def publish_to_topic(request: Request, topic: str) -> Response:
    body = await request.body()
    data = body.decode("utf-8")
    count = await hub.publish(topic, data)
    return JSONResponse({"published": True, "subscribers": count})
```

Allows the browser to publish directly (raw body = HTML fragment or JSON string). Returns the number of subscribers that received the message.

#### `WebSocket /ws/stream/{topic:path}` — WS alternative

```python
@broadcast_router.websocket("/ws/stream/{topic:path}")
async def ws_broadcast_handler(websocket: WebSocket, topic: str) -> None:
```

For environments where SSE is unreliable (corporate proxies, mobile networks). Authentication is performed manually before `accept()`:

```python
session = get_session_data(websocket)
if not session.get(SESSION_USER_KEY):
    await websocket.close(code=4401)
    return
```

Code `4401` is an application-level code in the 4000–4999 range that maps to "unauthorized". The close happens before `accept()`, so the handshake fails with HTTP 403 on the client side.

Once authenticated, the loop is straightforward:

```python
while True:
    data = await asyncio.wait_for(queue.get(), timeout=30.0)
    await websocket.send_text(data)
```

If no messages arrive within 30 seconds, it sends a `{"type": "ping"}` keepalive to maintain the connection.

### Usage pattern from a module

```python
# In the view that creates an item:
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
// On the client:
const es = new EventSource("/stream/todos");
es.addEventListener("message", (e) => {
    document.getElementById("todo-list").insertAdjacentHTML("beforeend", e.data);
});
```

---

## The tests

### `test_responses.py`

Tests use a `_make_request` helper that builds a Starlette `Request` with a minimal scope:

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

#### Tests for the `@view` decorator

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

The test uses `monkeypatch` to simulate the absence of a session user. It verifies that the decorator redirects correctly. The pattern is clean: no real server required.

#### Tests for helpers

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

The `_body` function handles both responses with a direct `body` attribute and those with a `body_iterator` (streaming), using a `ThreadPoolExecutor` to run the coroutine when a loop is already active.

#### Test for `BroadcastHub`

```python
class TestBroadcast:
    def test_broadcast_hub_import(self):
        from hotframe.views.broadcast import BroadcastHub
        hub = BroadcastHub()
        assert hub is not None
```

Only verifies the import. Integration tests for the hub (subscribe/publish/unsubscribe) would run in an async context with `pytest.mark.asyncio`.

---

## How this fits into the rest of the framework

| Component | Relationship with `views/` |
|---|---|
| `hotframe.bootstrap.create_app` | Instantiates `BroadcastHub`, stores it in `app.state.broadcast_hub`, and mounts `broadcast_router`. |
| `hotframe.live.LiveComponent` | `LiveComponent` instances do NOT use `@view` or `BroadcastHub`. They have their own WebSocket channel (`/ws/_live`) and `self.navigate()` / `self.toast()` methods. |
| `hotframe.auth` | `@view` uses `get_session_user_id` for authentication. The `CurrentUser` dependency protects the broadcast endpoints. |
| `hotframe.config.settings` | `AUTH_LOGIN_URL`, `AUTH_UNAUTHORIZED_URL`, and `PERMISSION_RESOLVER` are read inside the decorator on every request. |
| `hotframe.templating.globals` | `get_global_context(request)` injects `request`, `csrf_token`, `csp_nonce`, `user`, and other globals into every render. |
| Dynamic modules | Their routes use `@view(module_id="name", ...)`. The request's `module_registry` contributes module navigation to the context. |
| Static apps | Their `routes.py` use the same `@view`. There is no API difference between apps and modules from the decorator's perspective. |

### Decision table: what to use when

| Need | Tool |
|---|---|
| Static HTML page with auth and permissions | `@view` |
| Server-side stateful reactive component | `LiveComponent` in `live/` |
| Push updates to multiple open tabs | `BroadcastHub.publish()` + `/stream/{topic}` |
| Redirect after a POST | `reactive_redirect("/url")` |
| Reload the current page | `reactive_refresh()` |
| Notify DOM JS without live | `reactive_trigger("event", data=...)` |
| Flash message on next page | `add_message(request, "success", "Saved")` |
| Log / progress stream (1:1) | `sse_stream(request, generator)` |
| Toast notification in a reactive component | `await self.toast("message", level="success")` |
