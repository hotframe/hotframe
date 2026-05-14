# 13. HTTP Client and Interceptors (`http/`)

> The `http/` subsystem gives hotframe **authenticated, observable outbound HTTP clients**, with an Angular-style interceptor chain that lets you add retries, circuit breakers, and credential refresh in a composable way — without touching business logic.

---

## What this folder is for

This folder **does not manage incoming routes** — that is FastAPI's job. Its domain is exclusively the HTTP traffic your application generates toward third parties: external APIs, webhooks, internal microservices, and so on.

The problem it solves is real and recurring: every module needs to call external APIs with different credentials, different retry strategies, and shared observability. Without a dedicated subsystem, each module reinvents the wheel: manual auth, buggy retries, no metrics.

hotframe's solution has three layers:

1. **Authentication strategies** (`auth.py`) — objects that know how to sign a request.
2. **Interceptors** (`interceptors.py`, `builtin_interceptors.py`) — middlewares that wrap the send operation and can retry, short-circuit, or refresh tokens.
3. **`AuthenticatedClient`** (`client.py`) — the client that orchestrates everything, built on top of `httpx.AsyncClient`.
4. **`HttpClientRegistry`** (`registry.py`) — the central catalog where modules and apps register their named clients.
5. **`discover_interceptors`** (`loader.py`) — automatic discovery of interceptors from the filesystem.

---

## File map

| File | Responsibility |
|---|---|
| [`__init__.py`](../src/hotframe/http/__init__.py) | Public surface of the subsystem; re-exports everything needed |
| [`auth.py`](../src/hotframe/http/auth.py) | Authentication strategies: `Auth`, `BearerAuth`, `ApiKeyAuth`, `QueryApiKeyAuth`, `BasicAuth`, `HmacAuth`, `CustomAuth`, `NoAuth` |
| [`interceptors.py`](../src/hotframe/http/interceptors.py) | `Interceptor` protocol, `InterceptorBase` base class, `build_chain` function, `CallNext` alias |
| [`builtin_interceptors.py`](../src/hotframe/http/builtin_interceptors.py) | Built-in interceptors: `RetryInterceptor`, `CircuitBreakerInterceptor`, `RefreshInterceptor`, helper `exponential_backoff` |
| [`client.py`](../src/hotframe/http/client.py) | `AuthenticatedClient` — httpx client with auth + interceptors + events |
| [`registry.py`](../src/hotframe/http/registry.py) | `HttpClientRegistry` — named client catalog with per-module ownership support |
| [`loader.py`](../src/hotframe/http/loader.py) | `discover_interceptors` — scans directories for instances that satisfy the protocol |
| [`events.py`](../src/hotframe/http/events.py) | Event name constants emitted to the `AsyncEventBus` |

---

## `auth.py` — Authentication strategies

The design is intentionally minimal: an abstract base class with a single async method, and several concrete implementations that mutate the `httpx.Request` in place. All strategies are **stateless** with respect to the session: credentials are re-read on every request.

### `CredentialSource`

```python
CredentialSource = str | Callable[[], str] | Callable[[], Awaitable[str]]
```

Type alias accepted by all strategies as a credential source. The key insight is that it can be a **sync or async callable**: when a token rotates (OAuth, AWS metadata, etc.), the callable always returns the current value without needing to restart the process or recreate the client.

### `_resolve_source(source: CredentialSource) -> str`

Internal function that resolves the `CredentialSource` to a plain string. It handles all three cases (string literal, sync callable, async callable) and raises `TypeError` if the callable returns something other than a string.

### `Auth` — base class

```python
class Auth:
    async def apply(self, request: httpx.Request) -> None: ...
```

This is not a formal Protocol: it is a classic abstract class that raises `NotImplementedError`. Subclassing `Auth` is the canonical way to create custom strategies.

### `BearerAuth`

```python
class BearerAuth(Auth):
    def __init__(self, source: CredentialSource) -> None: ...
    async def apply(self, request: httpx.Request) -> None: ...
```

Adds the `Authorization: Bearer <token>` header. The token is resolved on every call to `apply`, so it works directly with a function that returns the current token from an OAuth store.

### `ApiKeyAuth`

```python
class ApiKeyAuth(Auth):
    def __init__(self, source: CredentialSource, header: str = "X-Api-Key") -> None: ...
```

Key in a configurable header. The `header` parameter allows adapting to APIs that use non-standard names (`Stripe-Key`, `X-Token`, etc.).

### `QueryApiKeyAuth`

```python
class QueryApiKeyAuth(Auth):
    def __init__(self, source: CredentialSource, param: str = "api_key") -> None: ...
```

For APIs that require the key in the query string. Uses `httpx.URL.copy_merge_params` to add the parameter without destroying existing parameters or leaving stale keys after a rotation.

### `BasicAuth`

```python
class BasicAuth(Auth):
    def __init__(self, username: str, password: str) -> None: ...
```

HTTP Basic auth (RFC 7617). Recomputes the Base64 header on every request even though `username`/`password` do not change — for consistency with the rest of the design.

### `HmacAuth`

```python
class HmacAuth(Auth):
    def __init__(self, key_id: str, secret: str, algorithm: str = "sha256") -> None: ...
```

Signs the request body with HMAC. The resulting header has the form:

```
Authorization: HMAC-SHA256 KeyId=<key_id>, Signature=<hexdigest>
```

Validates in `__init__` that the algorithm is available in `hashlib`. For streaming bodies, returns a signature over `b""` — a documented limitation that the code comment acknowledges explicitly.

### `CustomAuth`

```python
class CustomAuth(Auth):
    def __init__(self, apply: Callable[[httpx.Request], Awaitable[None]]) -> None: ...
```

Escape hatch: delegates authentication to an arbitrary async callable. Rejects sync callables in the constructor (raises `TypeError`) because flows such as OAuth refresh or SigV4 with AWS metadata require `await`.

### `NoAuth`

```python
class NoAuth(Auth):
    async def apply(self, request: httpx.Request) -> None:
        return None
```

Explicit no-op. This is the default for `AuthenticatedClient` when no `auth=` is provided. It makes the intent clear: "this client intentionally does not authenticate."

---

## `interceptors.py` — Chain primitives

### `CallNext`

```python
CallNext = Callable[[httpx.Request], Awaitable[httpx.Response]]
```

Type alias for "the function that calls the next link in the chain." Each interceptor receives one as its second argument in `intercept`. This abstraction completely decouples interceptors from the concrete client: they can be used in tests without importing `AuthenticatedClient`.

### `Interceptor` — Protocol

```python
@runtime_checkable
class Interceptor(Protocol):
    name: str
    applies_to: str | list[str] | Callable[[str], bool]
    order: int

    async def intercept(self, request: httpx.Request, call_next: CallNext) -> httpx.Response: ...
```

The contract that every interceptor must satisfy. It is `@runtime_checkable`, which allows using it with `isinstance()` for the loader's automatic discovery.

Attributes:
- `name`: unique identifier; used for logging and deduplication.
- `applies_to`: client selector — wildcard `"*"`, exact name, list of names, or a callable predicate.
- `order`: position in the chain; **lower values sit on the outside** (execute first on the way down, last on the way up).

### `InterceptorBase`

```python
class InterceptorBase:
    name: str = ""
    applies_to: str | list[str] | Callable[[str], bool] = "*"
    order: int = 100

    def applies_to_client(self, client_name: str) -> bool: ...
    async def intercept(self, request: httpx.Request, call_next: CallNext) -> httpx.Response: ...
```

Optional base class that provides:
- Sensible defaults for all three attributes.
- `applies_to_client(client_name)`: resolves the matcher using the same logic for all cases (wildcard, exact string, list, callable) and **fails closed** (returns `False`) for unknown formats.
- `intercept` defaults to a pure pass-through: `return await call_next(request)`.

Using `InterceptorBase` is not required — any object that satisfies the `Protocol` works.

### `build_chain(interceptors, terminal) -> CallNext`

```python
def build_chain(
    interceptors: list[Interceptor],
    terminal: CallNext,
) -> CallNext: ...
```

The central function of the system. Takes a list of interceptors and a `terminal` function (the one that actually sends the request) and returns the callable that represents the head of the full chain.

Algorithm:
1. Sorts interceptors by `order` ascending.
2. Builds the chain **from the inside out**: first wraps `terminal` with the interceptor with the highest `order`, then wraps that result with the next, continuing until the one with the lowest `order`.
3. Returns the outermost callable.

The result is that `order=100` (outer) runs its body before `order=200` (inner) on the way down, and after it on the way back up — exactly like ASGI middleware.

```python
# Conceptual example: CircuitBreaker(100) → Refresh(150) → Retry(200) → terminal
chain = build_chain([retry, circuit_breaker, refresh], terminal)
response = await chain(request)
```

---

## `builtin_interceptors.py` — The three built-in interceptors

### `exponential_backoff`

```python
def exponential_backoff(
    base: float = 0.5,
    cap: float = 8.0,
    jitter: bool = True,
) -> Callable[[int], float]: ...
```

Helper that returns a `compute(attempt) -> seconds` function. The formula is `min(cap, base * 2**attempt)`. With `jitter=True` (the default) the result is multiplied by a random factor in `[0.5, 1.0]` to avoid the thundering herd problem when many instances retry at the same time.

### `RetryInterceptor`

```python
class RetryInterceptor(InterceptorBase):
    def __init__(
        self,
        on_status: list[int],
        max_attempts: int = 3,
        backoff: Callable[[int], float] | None = None,
        applies_to: str | list[str] | Callable[[str], bool] = "*",
        order: int = 200,
        name: str = "retry",
    ) -> None: ...
```

Retries the same request when the response has a status code in `on_status`. `max_attempts` is the total number of attempts (including the first). If `backoff` is `None`, no sleep occurs between attempts. The default `order` is `200` — deliberately higher than `CircuitBreakerInterceptor` (100) so that retries happen **inside** the circuit breaker: if the breaker is open, retry budget is not wasted.

```python
retry = RetryInterceptor(
    on_status=[502, 503, 504],
    max_attempts=3,
    backoff=exponential_backoff(base=0.5, cap=8.0),
)
```

### `CircuitBreakerInterceptor`

```python
class CircuitBreakerInterceptor(InterceptorBase):
    def __init__(
        self,
        threshold: int = 5,
        recovery_seconds: float = 30.0,
        failure_statuses: list[int] | None = None,
        applies_to: str | list[str] | Callable[[str], bool] = "*",
        order: int = 100,
        name: str = "circuit_breaker",
    ) -> None: ...

    @property
    def state(self) -> str: ...  # "closed" | "open" | "half_open"
```

Implements the circuit breaker pattern with three states:

| State | Behavior |
|---|---|
| `closed` | Requests pass through; failures increment a counter |
| `open` | Requests are immediately rejected with `httpx.ConnectError` |
| `half_open` | One request passes as a probe; success → closes, failure → reopens |

A "failure" is any downstream exception OR a response with a status code in `failure_statuses` (defaults to all `5xx` codes).

The `open → half_open` transition occurs after `recovery_seconds`. Critical logic is protected by `asyncio.Lock` to prevent race conditions in environments with multiple concurrent coroutines. The `state` property is read-only and is useful for dashboards and health checks.

### `RefreshInterceptor`

```python
class RefreshInterceptor(InterceptorBase):
    def __init__(
        self,
        refresh: Callable[[], Awaitable[None]],
        on_status: int = 401,
        max_retries: int = 1,
        applies_to: str | list[str] | Callable[[str], bool] = "*",
        order: int = 150,
        name: str = "refresh",
    ) -> None: ...
```

When the downstream returns `on_status` (default `401`), calls the `refresh()` callback and retries the request. `max_retries=1` is deliberate: if a `401` persists after the refresh, something is wrong and continuing makes no sense — the original response is returned.

The `order=150` places it between the circuit breaker (100) and the retry interceptor (200): if the retry exhausts its budget first and the result is `401`, the refresh has no opportunity to run. Conversely, if the refresh itself fails, the circuit breaker may open.

The framework is agnostic about the refresh mechanism: OAuth, API key rotation, SigV4 with AWS metadata — everything fits in the async callback.

---

## `client.py` — `AuthenticatedClient`

```python
class AuthenticatedClient:
    def __init__(
        self,
        base_url: str = "",
        auth: Auth | None = None,
        timeout: float | httpx.Timeout = 10.0,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        event_bus: AsyncEventBus | None = None,
        name: str | None = None,
        interceptors: list[Interceptor] | None = None,
    ) -> None: ...
```

Wraps `httpx.AsyncClient` and adds three layers:

1. **Auth**: the strategy is applied on every request.
2. **Interceptors**: the chain wraps the send operation.
3. **Observability events**: emitted to the `AsyncEventBus` if one is configured.

### Design decision: two auth paths

When there are **no interceptors**, auth is attached as an `httpx` event hook (a `request` hook). This is efficient and clean.

When there **are interceptors**, auth moves into the `terminal` of the chain:

```python
async def terminal(req: httpx.Request) -> httpx.Response:
    await self._auth.apply(req)
    return await self._client.send(req)
```

This change is critical for `RefreshInterceptor`: when the refresh callback updates the token (for example via the `CredentialSource` callable), the retry that happens inside the interceptor calls `terminal` again, which re-reads the fresh token. Without this design, the retry would send the stale token.

### Main methods

| Method | Simplified signature | Description |
|---|---|---|
| `request` | `async def request(method, url, **kwargs) -> httpx.Response` | Dispatches any method; emits lifecycle events |
| `get/post/put/patch/delete` | `async def <method>(url, **kwargs) -> httpx.Response` | Shortcuts that delegate to `request` |
| `stream` | `def stream(method, url, **kwargs)` | Streaming context manager; does **not** pass through interceptors or emit lifecycle events |
| `set_interceptors` | `def set_interceptors(interceptors)` | Atomically replaces the chain; reconfigures the auth path |
| `aclose` | `async def aclose()` | Closes the underlying `httpx.AsyncClient` |

### Observability events

Events are emitted **once per external call** to `request`, not once per internal retry:

| Event | When | Extra payload |
|---|---|---|
| `http.request.started` | Before dispatch | `method`, `url` |
| `http.request.completed` | Response received | `method`, `url`, `status`, `duration_ms` |
| `http.request.failed` | Exception before response | `method`, `url`, `error`, `duration_ms` |

All three carry `client_name` if the client has a name. Errors during event emission never propagate — logging continues but the HTTP request proceeds normally.

### Properties

| Property | Type | Description |
|---|---|---|
| `auth` | `Auth` | Active authentication strategy |
| `name` | `str | None` | Client name |
| `base_url` | `httpx.URL` | Base URL of the underlying httpx client |
| `headers` | `httpx.Headers` | Default headers |
| `is_closed` | `bool` | Whether the underlying httpx client is closed |
| `interceptors` | `list[Interceptor]` | Shallow copy of the interceptor chain |

---

## `registry.py` — `HttpClientRegistry`

```python
class HttpClientRegistry:
    def __init__(
        self,
        ambient_interceptors: list[Interceptor] | None = None,
    ) -> None: ...
```

Named catalog that lives at `app.state.http_clients`. Two responsibilities:

1. **Registration and lookup by name**: modules and apps register clients; any other code looks them up.
2. **Lifecycle management**: when a module is deactivated, all of its clients are closed and removed from the catalog.

### Ambient interceptors

The registry maintains a **pool of ambient interceptors** (`ambient_interceptors`). When a client is registered without explicit `interceptors=`, the registry automatically applies the pool interceptors whose `applies_to` matches the client name. This allows global policies (retry, circuit breaker) to be configured once at bootstrap.

```python
# At startup:
registry.set_ambient_interceptors([
    RetryInterceptor(on_status=[502, 503, 504]),
    CircuitBreakerInterceptor(threshold=10),
])
```

### Registration methods

```python
def register(
    self,
    name: str,
    client: AuthenticatedClient,
    owner_module_id: str | None = None,
    interceptors: list[Interceptor] | None = None,
) -> None: ...
```

Registers a client. Raises `KeyError` if the name already exists — use `replace()` to overwrite. `owner_module_id` is the id of the module registering the client; when that module is deactivated, the registry cleans it up automatically.

```python
def replace(self, name, client, owner_module_id=None, interceptors=None) -> None: ...
```

Silently overwrites. The previous client is closed **in the background** (via `asyncio.create_task`) so the replacement is not blocked.

### Lookup

```python
def get(self, name: str) -> AuthenticatedClient | None: ...
def __getitem__(self, name: str) -> AuthenticatedClient: ...  # KeyError if not found
def __contains__(self, name: object) -> bool: ...
def list_registered(self) -> list[str]: ...
def owner_of(self, name: str) -> str | None: ...
```

### Lifecycle

```python
async def unregister(self, name: str) -> None: ...           # removes one client
async def unregister_module(self, module_id: str) -> None:   # removes all clients owned by a module
async def aclose_all(self) -> None:                          # shutdown: closes all clients
```

`unregister_module` is the key method for hot-unloading modules: `ModuleRuntime` calls it automatically when deactivating a module, releasing httpx connections without requiring the module to do manual cleanup.

---

## `loader.py` — `discover_interceptors`

```python
def discover_interceptors(
    search_paths: list[Path],
    logger: Logger | None = None,
    recursive: bool = False,
) -> list[Interceptor]: ...
```

Scans directories looking for **instances** (not classes) that satisfy the `Interceptor` protocol. This is the mechanism for "ambient interceptors" discovered from disk, equivalent to the auto-discovery of apps or modules.

Behavior:
- Ignores files starting with `_` (including `__init__.py`).
- A file that fails to import is logged as a `WARNING` and skipped — a broken interceptor does not block startup.
- Duplicate names are deduplicated (the first one discovered wins).
- The result is sorted by `order` ascending.
- Non-existent paths are logged as `DEBUG` and ignored — projects without interceptors start cleanly.

Detection uses `_looks_like_interceptor(obj)`:
1. It is not a class or a module.
2. It has attributes `name`, `applies_to`, `order`, `intercept`.
3. `intercept` is a coroutine (`inspect.iscoroutinefunction`).
4. `name` is a non-empty string.

---

## `events.py` — Event constants

Three string constants naming the events emitted to the `AsyncEventBus`:

```python
EVENT_REQUEST_STARTED   = "http.request.started"
EVENT_REQUEST_COMPLETED = "http.request.completed"
EVENT_REQUEST_FAILED    = "http.request.failed"
```

Using constants instead of string literals prevents typos and lets IDEs and grep find all subscribers.

---

## How this fits into the rest of the framework

- **Bootstrap (`create_app`)**: creates the `HttpClientRegistry` and stores it at `app.state.http_clients`. Calls `discover_interceptors` over configured paths to populate the ambient pool.
- **Modules**: when activated, a module can call `app.state.http_clients.register("stripe", client, owner_module_id="billing")`. When deactivated, `ModuleRuntime` calls `registry.unregister_module("billing")` automatically.
- **`AsyncEventBus`** (`signals/`): `AuthenticatedClient` optionally accepts the bus and emits the three lifecycle events. Other modules can subscribe to `"http.request.*"` for metrics, centralized logging, or alerts.
- **`ISession` / repos** (`db/`): there is no direct dependency between `http/` and `db/`. They are parallel subsystems.
- **Settings**: the interceptor discovery path can be configured in `settings.py` via `MODULE_SOURCE` or custom configuration.

---

## Gotchas and design decisions

**1. Interceptors are instances, not classes**
The loader looks for pre-configured instances at module level, not classes. This lets the interceptor file configure real parameters (`RetryInterceptor(on_status=[503])`) rather than deferring that responsibility to the framework.

**2. Streaming does not pass through interceptors**
`AuthenticatedClient.stream()` delegates directly to `httpx.AsyncClient.stream()`. Interceptors do not wrap streaming because `httpx` manages stream completion semantics. If you need to intercept streams, use a custom httpx `transport`.

**3. Lifecycle events are per external call, not per retry**
If `RetryInterceptor` makes 3 attempts, there is only one `EVENT_REQUEST_STARTED` and one `EVENT_REQUEST_COMPLETED` (or `FAILED`). This is intentional: retries are an implementation detail; the external observer sees a single logical operation.

**4. `replace()` closes the previous client in the background**
To avoid blocking the replacement, the old client is closed with `asyncio.create_task`. If no event loop is running (e.g., in tests), it falls back to `asyncio.run()` in a temporary loop. This avoids connection leaks without imposing an `await` on the caller.

**5. `CircuitBreakerInterceptor` is not distributed**
Circuit breaker state (`_state`, `_failures`) lives in process memory. In a multi-instance deployment (several server replicas), each process has its own independent breaker. If you need a shared breaker, implement your own interceptor backed by Redis.

**6. `CustomAuth` rejects sync callables**
Unlike `CredentialSource`, `CustomAuth` explicitly requires `async def`. The error message is instructive: "wrap synchronous logic in an async function if needed."

---

## Usage examples from the codebase

### Configure a client with Bearer auth and retry

```python
from hotframe.http import (
    AuthenticatedClient,
    BearerAuth,
    RetryInterceptor,
    CircuitBreakerInterceptor,
    exponential_backoff,
)

client = AuthenticatedClient(
    base_url="https://api.example.com",
    auth=BearerAuth(source=lambda: token_store.current_token()),
    interceptors=[
        CircuitBreakerInterceptor(threshold=5, recovery_seconds=30),
        RetryInterceptor(
            on_status=[502, 503, 504],
            max_attempts=3,
            backoff=exponential_backoff(base=0.5, cap=8.0),
        ),
    ],
    name="example_api",
)
```

### Register in the registry with module ownership

```python
# In the billing module, on activation:
registry = app.state.http_clients
registry.register(
    name="stripe",
    client=AuthenticatedClient(
        base_url="https://api.stripe.com/v1",
        auth=BearerAuth(source=settings.STRIPE_SECRET_KEY),
    ),
    owner_module_id="billing",
)

# On module deactivation, ModuleRuntime does:
await registry.unregister_module("billing")
# → The "stripe" client is closed automatically
```

### OAuth token refresh

```python
from hotframe.http import RefreshInterceptor, BearerAuth

token_store = {"access_token": "..."}

async def refresh_oauth():
    # Call the token endpoint and update token_store
    new_token = await oauth_client.refresh()
    token_store["access_token"] = new_token

client = AuthenticatedClient(
    auth=BearerAuth(source=lambda: token_store["access_token"]),
    interceptors=[
        RefreshInterceptor(refresh=refresh_oauth, on_status=401),
    ],
)
```

### Custom interceptor

```python
from hotframe.http import InterceptorBase, CallNext
import httpx

class LoggingInterceptor(InterceptorBase):
    name = "my_logger"
    order = 50  # outermost — sees all requests

    async def intercept(self, request: httpx.Request, call_next: CallNext) -> httpx.Response:
        print(f"→ {request.method} {request.url}")
        response = await call_next(request)
        print(f"← {response.status_code}")
        return response
```
