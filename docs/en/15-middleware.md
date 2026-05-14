# 15. The middleware stack (`middleware/`)

> The interception layer that wraps every HTTP request before it reaches a
> view, and every response before it leaves to the client. Security,
> observability, resource control, and dynamic-module extensibility all live
> here — active by default, requiring zero lines in your application code.

---

## What this folder is for

In FastAPI/Starlette, middleware is a chain of ASGI wrappers built once at
startup and applied to every request. hotframe centralises three
responsibilities here:

1. **Security** — per-request CSP nonce, robust sessions, CSRF
   (managed in `auth/csrf.py` but orchestrated here), rate limiting,
   and body size limiting.
2. **Infrastructure** — global timeout, proxy fix, OpenTelemetry
   observability, i18n.
3. **Hot-mount** — adding and removing middleware contributed by dynamic
   modules at runtime without restarting the process.

The folder exposes two public entry points:

```python
from hotframe.middleware.stack import build_middleware_stack
from hotframe.middleware.stack_manager import MiddlewareStackManager
```

`build_middleware_stack` is called once inside `create_app`.
`MiddlewareStackManager` is used by the `ModuleRuntime` whenever a module is
activated or deactivated.

---

## File map

| File | Responsibility |
|---|---|
| [`__init__.py`](../src/hotframe/middleware/__init__.py) | Module docstring, conceptual exports |
| [`body_limit.py`](../src/hotframe/middleware/body_limit.py) | Rejects requests with an excessive `Content-Length` (413) |
| [`csp.py`](../src/hotframe/middleware/csp.py) | Generates a per-request nonce, adds CSP and HSTS headers |
| [`error_pages.py`](../src/hotframe/middleware/error_pages.py) | Catches unhandled exceptions, renders HTML or JSON error responses |
| [`i18n_support.py`](../src/hotframe/middleware/i18n_support.py) | Translation engine using gettext, per-module domains, `LazyString` |
| [`language.py`](../src/hotframe/middleware/language.py) | Detects and activates the language for each request (5-source cascade) |
| [`module_middleware.py`](../src/hotframe/middleware/module_middleware.py) | Delegates to middleware registered by active modules |
| [`observability.py`](../src/hotframe/middleware/observability.py) | Binds observability context, records a duration histogram |
| [`proxy_fix.py`](../src/hotframe/middleware/proxy_fix.py) | Rewrites the host/scheme in the ASGI scope when behind a reverse proxy |
| [`rate_limit.py`](../src/hotframe/middleware/rate_limit.py) | Per-IP rate limiting with an in-memory sliding window, 3 buckets |
| [`session_safe.py`](../src/hotframe/middleware/session_safe.py) | `SessionMiddleware` wrapper that absorbs corrupt cookies |
| [`stack.py`](../src/hotframe/middleware/stack.py) | Builds the stack at startup from `settings.MIDDLEWARE` |
| [`stack_manager.py`](../src/hotframe/middleware/stack_manager.py) | Atomically rebuilds the stack for hot-mount at runtime |
| [`timeout.py`](../src/hotframe/middleware/timeout.py) | Cancels requests that exceed the threshold (default 30 s) |

---

## `stack.py` — the startup constructor

[`middleware/stack.py`](../src/hotframe/middleware/stack.py) is the only file
called during `create_app`. All its work is done by a single public function.

### `build_middleware_stack(app, settings)`

```python
def build_middleware_stack(app: FastAPI, settings: HotframeSettings) -> None:
```

Reads `settings.MIDDLEWARE` — a list of dotted paths such as
`"hotframe.middleware.csp.CSPMiddleware"` — and adds each class to the
FastAPI app. The Starlette subtlety is that **the last class added is the
outermost one** (the first to see each incoming request). Because
`settings.MIDDLEWARE` is ordered from outermost to innermost, the loop
iterates in reverse:

```python
for dotted_path in reversed(settings.MIDDLEWARE):
    cls = _import_class(dotted_path)
    kwargs = _get_middleware_kwargs(cls, settings)
    app.add_middleware(cls, **kwargs)
```

This guarantees that the declarative order in `settings.py` matches the
actual execution order.

### `_import_class(dotted_path)`

```python
def _import_class(dotted_path: str) -> type:
```

Performs an `importlib.import_module` on the module and a `getattr` for the
class name. Raises an exception if the path does not exist, letting it
propagate so that `create_app` fails fast and visibly.

### `_get_middleware_kwargs(cls, settings)`

```python
def _get_middleware_kwargs(cls: type, settings: HotframeSettings) -> dict[str, Any]:
```

A dispatch function that maps known classes to their kwargs. This keeps each
middleware from having to read `settings` directly — the builder is the sole
place that knows which settings parameter belongs to which middleware:

| Class | Constructed parameters |
|---|---|
| `RobustSessionMiddleware` | `secret_key`, `max_age`, `session_cookie`, `same_site`, `https_only` |
| `CSPMiddleware` | `enforce` |
| `APIRateLimitMiddleware` | `api_rate`, `auth_rate` (10 000 in DEBUG), `window`, `auth_prefixes` |
| `BodyLimitMiddleware` | `max_bytes` |
| `TimeoutMiddleware` | `timeout=30` |
| `ModuleMiddlewareManager` | `registry=None` (filled in later by the ModuleRuntime) |

For any other class it returns `{}` — the middleware receives only `app`.

---

## `stack_manager.py` — atomic rebuild for hot-mount

[`middleware/stack_manager.py`](../src/hotframe/middleware/stack_manager.py)
solves a non-trivial problem: Starlette builds `app._middleware_stack`
**exactly once**, on the first request, and caches it. If you subsequently
add or remove middleware with `app.add_middleware`, the change has no effect
on in-flight requests or on new ones until the next rebuild.

`MiddlewareStackManager` exposes an async-safe API that:

1. Invalidates the cache.
2. Executes a `compose_stack` function inside the lock to mutate
   `app.user_middleware`.
3. Forces an immediate rebuild with `app.build_middleware_stack()`.

All of this runs under `asyncio.Lock` to serialise concurrent rebuilds.

### Constructor

```python
def __init__(self, app: FastAPI) -> None:
    self._app = app
    self._lock = asyncio.Lock()
```

The manager holds no extra state — `app.user_middleware` is the single
source of truth.

### `rebuild(compose_stack=None)`

```python
async def rebuild(
    self,
    compose_stack: Callable[[], Awaitable[None]] | None = None,
) -> None:
```

The central method. Algorithm:

1. Acquires `self._lock`.
2. Sets `self._app.middleware_stack = None` — this invalidates the cache
   and is necessary because `add_middleware` refuses mutations if the stack
   is already built.
3. Executes `await compose_stack()` if one was provided — here the caller
   can call `app.add_middleware(...)` or edit `app.user_middleware`.
4. Calls `self._app.middleware_stack = self._app.build_middleware_stack()`
   to rebuild the stack eagerly (so the cost does not fall on the next
   incoming request).
5. Releases the lock.

Practical atomicity relies on the fact that attribute assignment in CPython
is a single bytecode operation, and that `build_middleware_stack` is
synchronous — there is no yield point between the reset and the rebuild.

### `add_and_rebuild(middleware_class, **options)`

```python
async def add_and_rebuild(
    self,
    middleware_class: type,
    **options: Any,
) -> None:
```

Convenience wrapper. Calls `rebuild` with a `compose` callable that executes
`self._app.add_middleware(middleware_class, **options)`. The new middleware
ends up on the outermost edge of the stack (executed first on incoming
requests).

### `remove_and_rebuild(middleware_class)`

```python
async def remove_and_rebuild(self, middleware_class: type) -> None:
```

Removes all entries whose `mw.cls is middleware_class` from
`app.user_middleware` and rebuilds. Idempotent: if the class is not present,
the rebuild happens anyway.

### Why this works for hot-mount

Requests that had already entered the old stack continue executing against
the closure they captured on entry. New requests that arrive after `rebuild`
returns use the new stack. There is no inconsistency window; in the worst
case two requests arrive simultaneously and both see the new stack (the
second observes the stack already built by the first).

---

## `body_limit.py` — `BodyLimitMiddleware`

[`middleware/body_limit.py`](../src/hotframe/middleware/body_limit.py)

**What it intercepts**: every HTTP request.

**What it does**: reads the `Content-Length` header. If it exceeds
`max_bytes` (default 10 MB), returns 413 without reading the body.

```python
class BodyLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, max_bytes: int = DEFAULT_MAX_BODY) -> None:
```

Key attributes:

- `_max_bytes`: limit in bytes, configurable via `settings.MAX_REQUEST_BODY`.
- `DEFAULT_MAX_BODY = 10 * 1024 * 1024` (10 MB).

The check is passive: if `Content-Length` is absent (e.g. chunked streaming),
the request passes through. Only the header is validated, not the actual body.
This is intentional — it guards against declared DoS, not adversarial
streaming (which would require reading the body, with its own cost).

---

## `csp.py` — `CSPMiddleware`

[`middleware/csp.py`](../src/hotframe/middleware/csp.py)

**What it intercepts**: every HTTP request and response.

**What it does**:

1. Generates `nonce = secrets.token_urlsafe(32)` — cryptographically secure,
   unique per request.
2. Stores it in `request.state.csp_nonce` so that templates can use it in
   `<script nonce="...">`.
3. Builds the CSP header by calling `build_csp_header(nonce, enforce)`
   (implemented in `hotframe.auth.csp`).
4. In `enforce=True` mode over HTTPS, also adds:
   - `Strict-Transport-Security: max-age=31536000; includeSubDomains`
   - `X-Robots-Tag: noindex, nofollow` (always, even over HTTP).

```python
class CSPMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, enforce: bool = False) -> None:
```

The `enforce` flag comes from `settings.CSP_ENFORCE`. When `False`, the
header is `Content-Security-Policy-Report-Only` (reports only, does not
block). When `True`, it is `Content-Security-Policy` (blocks).

---

## `error_pages.py` — `ErrorPageMiddleware`

[`middleware/error_pages.py`](../src/hotframe/middleware/error_pages.py)

**What it intercepts**: any unhandled exception that bubbles up through the
stack.

**What it does**:

1. Wraps `call_next(request)` in a `try/except Exception`.
2. On exception, decides between HTML and JSON based on the client's `Accept`
   header and whether the path starts with `/api/`.
3. When `settings.DEBUG=True`, includes the full traceback inside a
   collapsible `<details>` element.

### Relevant functions

```python
def _wants_json(request: Request) -> bool:
```

Returns `True` if `Accept` contains `application/json` or the path starts
with `/api/`.

```python
def _render_error_html(status_code: int, detail: str, tb: str | None = None) -> str:
```

Generates a self-contained error page with inline styles. Supports status
codes: 400, 403, 404, 405, 422, 429, 500, 502, 503.

```python
class ErrorPageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: ...) -> Response:
    def _handle_exception(self, request: Request, exc: Exception) -> Response:
```

`_handle_exception` reads `exc.status_code` and `exc.detail` (attributes of
`HTTPException`), falling back to `500` and `str(exc)`.

**Gotcha**: the middleware also inspects responses with `status >= 400`, but
currently does not overwrite them (the condition ends with `pass`). This is
scaffolding for a future expansion that would render error pages for empty
downstream responses.

---

## `i18n_support.py` — the translation engine

[`middleware/i18n_support.py`](../src/hotframe/middleware/i18n_support.py)

This file is not an ASGI middleware — it is **the internal i18n library**.
It exposes everything the rest of the framework needs to translate strings.

### Language `ContextVar`

```python
_current_language: ContextVar[str] = ContextVar("current_language", default=DEFAULT_LANGUAGE)
```

Each asyncio task has its own value, so two concurrent requests in different
languages do not interfere with each other.

### `activate(language)` / `deactivate()`

```python
def activate(language: str) -> None:
def deactivate() -> None:
```

`activate` validates that the code is in `SUPPORTED_LANGUAGES` (`en`, `es`)
and writes it to the `ContextVar`. `deactivate` resets it to
`DEFAULT_LANGUAGE`. `LanguageMiddleware` always calls `deactivate()` at the
end of each request to clean up the contextvar.

### `_(text, module_id=None)`

```python
def _(text: str, module_id: str | None = None) -> str:
```

The primary translation function. Fallback chain:

1. If the active language is `en`, returns `text` without any lookup.
2. If `module_id` is registered in `_module_locales`, looks in the module's
   domain.
3. Falls back to the `"messages"` domain of core (`hotframe/locales/`).
4. If no translation is found, returns the original `text`.

### `ngettext(singular, plural, n, module_id=None)`

```python
def ngettext(singular: str, plural: str, n: int, module_id: str | None = None) -> str:
```

Pluralised version with the same fallback chain.

### LRU translation cache

```python
@lru_cache(maxsize=128)
def _get_translation(domain: str, locales_dir: str, language: str) -> ...:
```

Caches `GNUTranslations` objects keyed by `(domain, locales_dir, language)`.
The cache is invalidated with `_clear_cache()` each time a module registers
or unregisters its locales — `_get_translation.cache_clear()`.

### Module locale registration

```python
def register_module_locales(module_id: str, locales_dir: Path) -> None:
def unregister_module_locales(module_id: str) -> None:
```

The `ModuleLoader` calls `register_module_locales` when activating a module
that has a `locales/` folder, and `unregister_module_locales` when
deactivating it.

### `LazyString`

```python
class LazyString:
    def __init__(self, text: str, module_id: str | None = None) -> None:
```

An object that behaves like a `str` but defers translation until `str()` or
`f"{lazy_str}"` evaluates it. Useful for module-level constants defined at
import time:

```python
# modules/inventory/module.py
MODULE_NAME = LazyString("Inventory", module_id="inventory")
```

When evaluated inside a request where the active language is `es`,
`str(MODULE_NAME)` → `"Inventario"`. Implements `__str__`, `__repr__`,
`__eq__`, `__hash__`, `__contains__`, `__add__`, `__radd__`, `__len__`,
`__bool__`, and `__format__` to behave as a string in most contexts.

The `.source` property returns the original untranslated text.

### `_RequestTranslations` and `get_translations()`

```python
class _RequestTranslations:
    def gettext(self, message: str) -> str: ...
    def ngettext(self, singular: str, plural: str, n: int) -> str: ...
```

Adapter for `jinja2.Environment.install_gettext_translations()`. Delegates
to the module's `_()` and `ngettext()`, so Jinja2 respects the language of
the current request.

---

## `language.py` — `LanguageMiddleware`

[`middleware/language.py`](../src/hotframe/middleware/language.py)

**What it intercepts**: every HTTP request (except `/static/`).

**What it does**: determines the user's language and activates it for the
duration of the request.

```python
class LanguageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: ...) -> Response:
```

### Detection cascade (in priority order)

| Source | Condition |
|---|---|
| 1. Session | `request.state.session["language"]` exists and is valid |
| 2. Cookie | `_lang` cookie present and valid |
| 3. `Accept-Language` header | Parsed with quality-factor negotiation (`q=`) |
| 4. User preference | `request.state.user.language` if there is an authenticated user |
| 5. Settings default | `settings.LANGUAGE` |

If the detected language is not in `_SUPPORTED_CODES`, a warning is logged
and it falls back to the settings default (or `"en"` if the settings value
is also invalid).

### Persistence cookie

At the end of each request, if the detected language differs from what the
client sent in the `_lang` cookie, a
`Set-Cookie: _lang=<lang>; max-age=31536000` header is emitted. The cookie
has `httponly=False` (JavaScript can read it to update the UI) and
`samesite="lax"`.

`/static/` paths are skipped entirely — no detection, no cookie — to avoid
breaking CDN caches.

### ContextVar cleanup

When returning the response, `deactivate()` is always called to reset the
contextvar. This is critical in asyncio: without the reset, a task that
recycles a coroutine could inherit the language from a previous request.

---

## `module_middleware.py` — `ModuleMiddlewareManager`

[`middleware/module_middleware.py`](../src/hotframe/middleware/module_middleware.py)

**What it intercepts**: every request and response, but only when modules
have registered middleware.

**What it does**: acts as a meta-middleware that delegates to a dynamic list
of middleware contributed by active modules.

### `ModuleMiddlewareProtocol`

```python
@runtime_checkable
class ModuleMiddlewareProtocol(Protocol):
    async def process_request(self, request: Request) -> Response | None: ...
    async def process_response(self, request: Request, response: Response) -> Response: ...
```

The contract that module middleware must satisfy. `process_request` can
return a `Response` to short-circuit the pipeline (useful for module-level
auth, feature flags, etc.). `process_response` can mutate the response
before it is returned.

### `ModuleMiddlewareManager`

```python
class ModuleMiddlewareManager(BaseHTTPMiddleware):
    def __init__(self, app: Any, registry: Any | None = None) -> None:
        self.registry = registry
        self._cached_middleware: list[ModuleMiddlewareProtocol] | None = None
        self._cache_version: int = -1
```

Key attributes:

- `registry`: reference to the ModuleRuntime registry. Injected **after**
  startup, which is why the default is `None`. Until it is assigned, the
  manager is a transparent passthrough.
- `_cached_middleware`: cached list of active middleware instances.
- `_cache_version`: the registry version number at the last cache rebuild.

### `_get_middleware_list()`

```python
def _get_middleware_list(self) -> list[ModuleMiddlewareProtocol]:
```

Compares `registry.version` with `self._cache_version`. If they match,
returns the cached list without any I/O. If they differ (because a module
was activated or deactivated), calls `registry.get_all_middleware()` and
rebuilds the list, filtering out any objects that do not satisfy
`ModuleMiddlewareProtocol`.

### `dispatch` — execution order

```python
# Request phase (in order)
for mw in middleware_list:
    result = await mw.process_request(request)
    if result is not None:
        return result  # short-circuit

response = await call_next(request)

# Response phase (in reverse order)
for mw in reversed(middleware_list):
    response = await mw.process_response(request, response)
```

The request phase runs in registration order. The response phase runs in
reverse — the standard onion pattern.

### `invalidate_cache()`

```python
def invalidate_cache(self) -> None:
```

Forces the list to be rebuilt on the next request. Called by `ModuleRuntime`
when a module is activated or deactivated.

---

## `observability.py` — `RequestObservabilityMiddleware`

[`middleware/observability.py`](../src/hotframe/middleware/observability.py)

**What it intercepts**: every HTTP request.

**What it does**: binds the `request_id` (generated by
`asgi-correlation-id.CorrelationIdMiddleware`) together with `hub_id` and
`user_id` to the request's observability context, and records a duration
histogram with HTTP attributes.

```python
class RequestObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: ...) -> Response:
        request_id = correlation_id.get() or ""
        hub_id = str(getattr(request.state, "hub_id", "") or "")
        user_id = str(getattr(request.state, "user_id", "") or "")

        with bind_context(request_id=request_id, hub_id=hub_id, user_id=user_id):
            start = time.perf_counter()
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start) * 1000

            get_request_duration_histogram().record(
                duration_ms,
                attributes={
                    "http.method": request.method,
                    "http.route": route,
                    "http.status_code": response.status_code,
                },
            )
```

`bind_context` is a context manager from `hotframe.utils.observability_context`
that configures structured logging and the OpenTelemetry span for the
duration of the request.

### `bind_user_context(user_id, hub_id="")`

```python
def bind_user_context(user_id: str, hub_id: str = "") -> None:
```

A helper function, not middleware. Called by the authentication layer
**after** the user has been identified, to enrich the observability context
with the real identity. The middleware can only read `request.state.user_id`
if auth has already set it there, but for public routes or async auth,
`bind_user_context` allows the context to be updated mid-request.

---

## `proxy_fix.py` — `ProxyFixMiddleware`

[`middleware/proxy_fix.py`](../src/hotframe/middleware/proxy_fix.py)

**What it intercepts**: HTTP and WebSocket requests.

**What it does**: corrects the `host` and `scheme` in the ASGI scope when
the server is behind a load balancer or reverse proxy.

```python
class ProxyFixMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        slug: str = "",
        domain_base: str = "",
        ecs_region: str = "",
    ) -> None:
```

Unlike the others, **it does not inherit from `BaseHTTPMiddleware`** — it is
a pure ASGI class that implements `__call__(scope, receive, send)`. This
gives it full control over headers without the buffering overhead of
`BaseHTTPMiddleware`.

### Correction logic

Two branches:

1. **ECS (AWS Elastic Container Service)**: if the `Host` header in the
   scope ends with `self._ecs_suffix` (e.g. `.ecs.eu-west-1.on.aws`),
   replaces `scope["server"]` and the `host` header with the public host
   built from `"{slug}.{domain_base}"`. Also forces `scheme="https"`.

2. **Standard `X-Forwarded-Host`**: if the `x-forwarded-host` header is
   present, it is used as the host. If `X-Forwarded-Proto` is present, the
   scheme is corrected accordingly.

The middleware buffers the complete response (`body_chunks`) so it can
recalculate and emit the correct `Content-Length` after the rewrite.

---

## `rate_limit.py` — `APIRateLimitMiddleware`

[`middleware/rate_limit.py`](../src/hotframe/middleware/rate_limit.py)

**What it intercepts**: requests to `/api/`, `/m/`, and the configured auth
prefixes.

**What it does**: controls the number of requests per IP using an in-memory
sliding window.

### `_SlidingWindow`

```python
class _SlidingWindow:
    __slots__ = ("_requests",)

    def is_allowed(self, key: str, limit: int, window: int) -> tuple[bool, int]:
    def cleanup(self, max_age: float = 300.0) -> None:
```

Stores request timestamps keyed by `"bucket:ip"`. For each request:

1. Filters out timestamps that fall outside the window.
2. If `>= limit` remain, rejects the request.
3. Otherwise, appends the current timestamp and returns `(True, remaining)`.

`cleanup()` removes inactive keys (no requests in the last 5 minutes). It
is called automatically every 60 seconds of monotonic clock time from
`dispatch`.

The `_window` instance and `_last_cleanup` are **module-level singletons**
shared across all requests in the process.

### The three buckets

```python
def _get_rate_config(self, path: str) -> tuple[str, int] | None:
    if path.startswith("/api/"):
        return "api", self._api_rate      # default: 120 req/60s
    if path.startswith("/m/"):
        return "view", self._view_rate    # default: 300 req/60s
    if any(path.startswith(p) for p in self._auth_prefixes):
        return "auth", self._auth_rate    # default: 60 req/60s
    return None                           # no rate limit
```

Paths that do not match any bucket pass through unrestricted.

### Response headers

When the request is allowed, the response carries:

```
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 87
```

When it is rejected (429):

```
Retry-After: 60
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 0
```

**Note on DEBUG**: in debug mode, `_get_middleware_kwargs` in `stack.py`
overrides `auth_rate` to 10 000, effectively disabling the auth rate limit
in development.

---

## `session_safe.py` — `RobustSessionMiddleware`

[`middleware/session_safe.py`](../src/hotframe/middleware/session_safe.py)

**What it intercepts**: every HTTP and WebSocket request.

**What it does**: a drop-in replacement for
`starlette.middleware.sessions.SessionMiddleware` that does not blow up when
the session cookie is malformed.

### The problem

Starlette's `SessionMiddleware` decodes the cookie with `base64.b64decode`
and `json.loads` without catching `UnicodeDecodeError` or `binascii.Error`.
If the user has a cookie from an older server version (e.g. one using zlib
compression or a custom format), decoding fails with a 500.

### The solution

```python
class RobustSessionMiddleware(SessionMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
            return
        except (UnicodeDecodeError, ValueError, OSError) as exc:
            # Log and continue with an empty session
            ...

        scope = _scope_without_cookie(scope, cookie_name)

        async def send_with_clear(message: Message) -> None:
            # Adds Set-Cookie with Max-Age=0 to delete the bad cookie
            ...

        await super().__call__(scope, receive, send_with_clear)
```

When decoding fails:

1. Logs the error to the `hotframe.middleware.session` logger.
2. Rebuilds the scope without the bad cookie using `_scope_without_cookie`.
3. Wraps `send` in `send_with_clear`, which appends
   `Set-Cookie: <name>=; Max-Age=0` so the browser deletes the cookie
   immediately.
4. Re-invokes the parent middleware — now with a clean scope, the session
   will be empty and the request proceeds normally.

### `_scope_without_cookie(scope, cookie_name)`

```python
def _scope_without_cookie(scope: Scope, cookie_name: str) -> Scope:
```

Parses the `Cookie` header (format `k1=v1; k2=v2`) and filters out the
entry matching `cookie_name`. If the cookie string becomes empty, the
`Cookie` header is removed from the scope entirely.

---

## `timeout.py` — `TimeoutMiddleware`

[`middleware/timeout.py`](../src/hotframe/middleware/timeout.py)

**What it intercepts**: every HTTP request (except `/health` and `/health/`).

**What it does**: cancels the request if it exceeds the timeout using
`asyncio.timeout`.

```python
class TimeoutMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout  # default: 30 seconds

    async def dispatch(self, request: Request, call_next: ...) -> Response:
        if request.url.path in ("/health/", "/health"):
            return await call_next(request)
        try:
            async with asyncio.timeout(self.timeout):
                return await call_next(request)
        except TimeoutError:
            return JSONResponse({"detail": "Request timeout"}, status_code=504)
```

Returns 504 (Gateway Timeout). Health check paths are excluded because
monitoring tools (ALB, Kubernetes liveness probes) must always receive a
response.

This must be the **outermost middleware** in the stack — so the timeout
covers the total time including the processing of every other middleware.

---

## The middleware stack order

This is the **actual execution order** (outermost to innermost, left to
right in the flow of an incoming HTTP request):

```
Request →
  1. TimeoutMiddleware               — cancels if > 30 s
  2. ProxyFixMiddleware              — corrects host/scheme behind a proxy
  3. CorrelationIdMiddleware         — generates / propagates X-Request-ID
  4. ErrorPageMiddleware             — catches exceptions, renders error pages
  5. RobustSessionMiddleware         — decodes session cookie (or clears it if bad)
  6. CSPMiddleware                   — generates CSP nonce, stores in request.state.csp_nonce
  7. CSRFMiddleware                  — validates CSRF token on mutations (apps/auth/csrf.py)
  8. RequestObservabilityMiddleware  — binds observability context + histogram
  9. LanguageMiddleware              — detects and activates language, stores in request.state.language
 10. APIRateLimitMiddleware          — per-IP rate limiting
 11. BodyLimitMiddleware             — rejects oversized requests
 12. ModuleMiddlewareManager         — delegates to active module middleware
       → view / handler
```

The response traverses the chain in the opposite direction (12 → 1).

Order matters in particular for:

- `RobustSessionMiddleware` must come before `CSRFMiddleware` because CSRF
  reads the session to validate the token.
- `TimeoutMiddleware` must be outermost to cover the entire chain.
- `RequestObservabilityMiddleware` comes after `RobustSessionMiddleware`
  so it can read `request.state.hub_id` if authentication has set it there.
- `LanguageMiddleware` must come after `RobustSessionMiddleware` because
  it reads the language from the session.

---

## How this fits into the rest of the framework

### Bootstrap (`create_app`)

`create_app` in `hotframe/__init__.py` calls
`build_middleware_stack(app, settings)` as step 2 of startup (see section 4
of GUIDE.md). This registers all middleware before the first request arrives.

### ModuleRuntime (dynamic modules)

When the `ModuleRuntime` activates a module that has its own middleware:

1. The module registers its instances in the registry.
2. `ModuleMiddlewareManager` detects the `registry.version` change on the
   next request and reloads the cached list.

If the module needs to add a full ASGI middleware (not just a
`ModuleMiddlewareProtocol`), the `ModuleRuntime` can use
`MiddlewareStackManager` for an atomic `add_and_rebuild`.

### Jinja2 templates

- `request.state.csp_nonce` is set by `CSPMiddleware` and consumed by the
  global Jinja2 context helpers registered in `create_app`.
- `request.state.language` is set by `LanguageMiddleware` and read by the
  `_RequestTranslations` adapter to translate strings in templates.

### Settings

All middleware parameters are controlled from `settings.py`:

```python
MIDDLEWARE: list[str]             # class list and order
CSP_ENFORCE: bool                 # CSPMiddleware
RATE_LIMIT_API: int               # APIRateLimitMiddleware
RATE_LIMIT_AUTH: int
RATE_LIMIT_AUTH_PREFIXES: list[str]
MAX_REQUEST_BODY: int             # BodyLimitMiddleware
SESSION_COOKIE_NAME: str          # RobustSessionMiddleware
SESSION_MAX_AGE: int
SECRET_KEY: str
DEPLOYMENT_MODE: str              # "local" vs production (https_only)
```

---

## Gotchas and design decisions

### 1. In-memory sliding window, not Redis

`_SlidingWindow` is a per-process singleton. In a multi-instance deployment
each instance has its own counter, which effectively multiplies the limit by
the number of instances. This is acceptable for most applications, but if
you need strict per-user limits in production with multiple workers, you
will need to implement a Redis backend.

### 2. `BaseHTTPMiddleware` vs pure ASGI middleware

Most middleware inherits from `BaseHTTPMiddleware` for simplicity.
`ProxyFixMiddleware` is the exception — it is pure ASGI because it needs to
buffer the complete response to recalculate `Content-Length` after the
rewrite, which `BaseHTTPMiddleware` does not facilitate. The
`BaseHTTPMiddleware` overhead (buffering the body in memory) is acceptable
for the others because they operate on headers only.

### 3. The manager invalidates, it does not replace

`MiddlewareStackManager` does not build an alternative stack and perform an
atomic swap. It invalidates Starlette's cache and lets
`build_middleware_stack()` rebuild it. The "atomicity" is best-effort under
CPython (no guarantee on other implementations).

### 4. `RobustSessionMiddleware` only absorbs decoding errors

The class does not catch a generic `Exception` — only `UnicodeDecodeError`,
`ValueError`, and `OSError` (which covers `binascii.Error`). Legitimate
session errors (e.g. a handler that corrupts state) still propagate.

### 5. `ModuleMiddlewareManager.registry` is injected post-construction

During bootstrap, the manager is built with `registry=None`. The
`ModuleRuntime`, created later, assigns its registry to the manager. Until
then, the manager is a transparent passthrough. This pattern avoids a
circular dependency between the middleware and the module runtime.

### 6. The `/static/` exclusion in `LanguageMiddleware` is intentional

Requests for static assets must not carry `Set-Cookie` because it would
break CDN caching. Static files are identical for all languages; language
only affects the dynamic UI.
