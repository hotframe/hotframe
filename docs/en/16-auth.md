# 16. Security and authentication (auth/)

> Purpose: implement hotframe's complete security stack — sessions, password and PIN hashing,
> symmetric encryption of secrets, CSRF protection, CSP headers, wildcard permission checks,
> rate limiting with escalating lockouts, and session decoding over WebSocket.

---

## What this folder is for

`auth/` covers everything that stands between an HTTP request and application code from a
security standpoint. Each file has a well-defined, bounded responsibility:

- User session management (who is logged in).
- Password and PIN hashing and verification.
- Symmetric encryption of secrets at rest.
- CSRF token generation and validation.
- Content-Security-Policy header construction with a per-request nonce.
- Dependency injection for the current user and permissions.
- Wildcard permission system.
- Rate limiting with escalating lockouts.
- Session cookie decoding outside the HTTP cycle (WebSocket).

---

## File map

| File | Responsibility |
|---|---|
| [`auth/__init__.py`](../src/hotframe/auth/__init__.py) | Package documentation and re-exports |
| [`auth/auth.py`](../src/hotframe/auth/auth.py) | Sessions: `create_session`, `destroy_session`, `hash_password`, `verify_password`, `hash_pin`, `verify_pin`, `get_session_user_id` |
| [`auth/crypto.py`](../src/hotframe/auth/crypto.py) | Symmetric Fernet encryption: `encrypt_secret`, `decrypt_secret`, `generate_key` |
| [`auth/csp.py`](../src/hotframe/auth/csp.py) | CSP header construction: `build_csp_header` |
| [`auth/csrf.py`](../src/hotframe/auth/csrf.py) | Double-submit cookie CSRF middleware: `CSRFMiddleware` |
| [`auth/current_user.py`](../src/hotframe/auth/current_user.py) | FastAPI dependencies: `get_current_user`, `get_db`, `DbSession`, `CurrentUser`, etc. |
| [`auth/permissions.py`](../src/hotframe/auth/permissions.py) | Permission engine: `has_permission`, `require_permission`, `RequireAdmin` |
| [`auth/rate_limit.py`](../src/hotframe/auth/rate_limit.py) | In-memory rate limiter with escalating lockouts: `PINRateLimiter` |
| [`auth/session_helpers.py`](../src/hotframe/auth/session_helpers.py) | Starlette session decoding for WebSocket: `get_session_data` |

---

## auth/auth.py — Sessions and hashing

This is the operational core of the login/logout flow. It handles two distinct concerns:
HTTP session state and credential hashing.

### `SESSION_USER_KEY` constant

```python
SESSION_USER_KEY = "user_id"
```

The key under which the user's UUID is stored in Starlette's session dict. Centralising this
constant prevents different parts of the codebase from accidentally using different keys.

### `hash_password(password: str) -> str`

```python
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
```

Generates a bcrypt hash of the password. `bcrypt.gensalt()` generates a fresh random salt on
every call, so two calls with the same password produce different hashes. The result is a
UTF-8 string that can be stored directly in the database.

### `verify_password(password: str, password_hash: str) -> bool`

```python
def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False
```

Verifies a password against its bcrypt hash. Catches `ValueError` and `TypeError` to return
`False` instead of propagating exceptions when the hash is malformed — important defensive
behaviour that avoids leaking information through error logs.

### `hash_pin(pin: str) -> str` and `verify_pin(pin: str, pin_hash: str) -> bool`

Identical in logic to their password equivalents, but semantically distinct: a PIN is typically
4–8 digits and has its own rate limiter (`PINRateLimiter` in `rate_limit.py`).

### `get_session_user_id(request: Request) -> UUID | None`

```python
def get_session_user_id(request: Request) -> UUID | None:
    raw = request.session.get(SESSION_USER_KEY)
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None
```

Reads the user's UUID from the session. The try/except guards against corrupt sessions where
the stored value is not a valid UUID.

### `create_session(request: Request, user_id: UUID) -> None`

```python
def create_session(request: Request, user_id: UUID) -> None:
    request.session[SESSION_USER_KEY] = str(user_id)
```

Persists the user's UUID in the session. The UUID is serialised to `str` because
`request.session` is a JSON-serialisable dict. The cookie is signed with `SECRET_KEY` by
Starlette's `SessionMiddleware` — no manual signing required.

### `destroy_session(request: Request) -> None`

```python
def destroy_session(request: Request) -> None:
    request.session.clear()
```

Clears all session data. `SessionMiddleware` will remove the cookie from the response.

---

## auth/crypto.py — Symmetric Fernet encryption

This module protects **secrets at rest** — third-party API tokens, integration credentials,
certificates — that are stored encrypted in the database.

### Custom exceptions

```python
class SecretsKeyMissingError(RuntimeError): ...
class SecretDecryptionError(RuntimeError): ...
```

`SecretsKeyMissingError` is raised at startup if `HUB_SECRETS_KEY` is not configured in a
non-local environment. `SecretDecryptionError` is raised during decryption if the key is wrong
or the ciphertext has been tampered with.

### `_get_fernet() -> Fernet` (cached)

```python
@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
```

Builds the `Fernet` instance once per process. Reads `HUB_SECRETS_KEY` from the environment.
If it is not defined and `HUB_DEPLOYMENT_MODE` is not `"local"`, raises
`SecretsKeyMissingError` — fail-fast in production. In local mode, it derives a key from
`HUB_SECRET_KEY` using SHA-256 and emits a `WARNING` in the log.

### `encrypt_secret(plaintext: str) -> str`

```python
def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")
```

Empty strings are returned unencrypted. This avoids storing encrypted values for optional
fields that have no value yet — important so that "no value" is never confused with "an
encrypted empty value".

### `decrypt_secret(ciphertext: str) -> str`

```python
def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptionError(...) from exc
```

Raises `SecretDecryptionError` (not `cryptography`'s `InvalidToken`) so application code does
not need to import `cryptography` to handle the error.

### `generate_key() -> str`

```python
def generate_key() -> str:
    return Fernet.generate_key().decode("utf-8")
```

Generates a fresh Fernet key. Useful in bootstrap scripts or onboarding documentation.

### `reset_cache() -> None`

```python
def reset_cache() -> None:
    _get_fernet.cache_clear()
```

Clears the LRU cache. Essential in tests that change `HUB_SECRETS_KEY` mid-run.

---

## auth/csp.py — Content-Security-Policy

### `build_csp_header(nonce: str, enforce: bool) -> tuple[str, str]`

```python
def build_csp_header(nonce: str, enforce: bool) -> tuple[str, str]:
```

Returns `(header_name, header_value)`.

- If `enforce=True`: `Content-Security-Policy` (blocks violations).
- If `enforce=False`: `Content-Security-Policy-Report-Only` (reports only, does not block).
  Useful for gradually rolling out CSP in production without breaking anything.

Base directives generated:

| Directive | Base value |
|---|---|
| `default-src` | `'self'` |
| `script-src` | `'self' 'nonce-{nonce}' 'unsafe-eval'` + extra sources |
| `style-src` | `'self' 'unsafe-inline'` + extra sources |
| `img-src` | `'self' data: blob:` + extra sources |
| `connect-src` | `'self' wss://*` (enforce) or `'self' ws://localhost:* wss://*` (report-only) |
| `font-src` | `'self'` + extra sources |
| `object-src` | `'none'` |
| `base-uri` | `'self'` |
| `form-action` | `'self'` |
| `frame-ancestors` | `'none'` |

Note that `ws://localhost:*` only appears in report-only mode (development), not in enforce
mode (production). This prevents blocking the local WebSocket connection in dev.

If `settings.CSP_TRUSTED_TYPES` is `True`, the following is appended:
```
require-trusted-types-for 'script'
trusted-types default iconify 'allow-duplicates'
```

Extra sources are read from `settings.CSP_ALLOWED_SOURCES`, a dict with keys `"script"`,
`"style"`, `"connect"`, `"img"`, and `"font"`.

---

## auth/csrf.py — Double-submit cookie CSRF middleware

### Constants

```python
COOKIE_NAME = "csrf_token"
HEADER_NAME = "x-csrf-token"
FORM_FIELD  = "csrf_token"
```

The token travels in three places: a cookie, an HTTP header, or a form field. The middleware
accepts any of the three as proof of intent.

### `generate_csrf_token() -> str`

```python
def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)
```

32 URL-safe bytes = 256 bits of entropy, generated with `secrets` (CSPRNG).

### `CSRFMiddleware`

```python
class CSRFMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, exempt_prefixes: tuple[str, ...] | None = None) -> None:
```

Implements the **double-submit cookie** strategy: the token is present in the cookie and must
also appear in the request body or header. An attacker on a different origin cannot read the
cookie (Same-Origin Policy), so they cannot include the token in the forged request.

#### Flow in `dispatch`

1. **WebSocket upgrade**: passed through directly (`call_next`), no CSRF processing. WebSockets
   authenticate via the session.
2. **`/static/` routes**: passed through so CDNs can cache without cookies.
3. **Read existing token**: looks up the `csrf_token` cookie. If absent, generates a new one
   (`new_token = True`).
4. **Store in `request.state.csrf_token`**: so Jinja2 templates can access it.
5. **Unsafe methods (POST/PUT/PATCH/DELETE)**: if the route is not in `_exempt_prefixes`,
   validates that the submitted token matches the cookie using `secrets.compare_digest`
   (constant-time, timing-attack resistant).
6. **Set-Cookie**: if the token is new, sets it on the response with `samesite="lax"`,
   `httponly=False` (it must be readable by JavaScript to be sent as a header), and
   `secure=True` when the connection is HTTPS.

#### Exempt routes

```python
self._exempt_prefixes = tuple(settings.CSRF_EXEMPT_PREFIXES)
```

Exempt by default: `/api/`, `/health`, `/static/`. REST API routes that use Bearer tokens do
not need CSRF protection.

#### `_get_submitted_token`

Looks for the token in this order:
1. The `x-csrf-token` header (for AJAX/fetch requests).
2. The `csrf_token` field in an `application/x-www-form-urlencoded` or `multipart/form-data`
   body.

---

## auth/current_user.py — Dependency injection

This file centralises the most commonly used FastAPI dependencies in the framework.

### `get_db() -> AsyncGenerator[ISession, None]`

```python
async def get_db() -> AsyncGenerator[ISession, None]:
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

Dependency that yields a database session. Commits on clean exit, rolls back on any exception.
The return type is `ISession` (a protocol), not `AsyncSession` directly.

`DbSession = Annotated[ISession, Depends(get_db)]` is the alias you use in route signatures:

```python
@router.get("/products")
async def list_products(db: DbSession):
    ...
```

### `get_current_user(request, db) -> Any`

```python
async def get_current_user(request: Request, db: DbSession) -> Any:
```

Full flow:

1. Reads `user_id` from the session with `get_session_user_id`.
2. If there is no `user_id`, raises `HTTP 401`.
3. Dynamically imports the user class from `settings.AUTH_USER_MODEL`.
4. Queries `SELECT * FROM users WHERE id = ? AND is_active = true`.
5. If no user is found, raises `HTTP 401`.
6. Resolves permissions in this order of precedence:
   - If `user.is_admin` → `["*"]` (superuser).
   - If the user has a `get_permissions()` method → calls it and uses the result.
   - If the user has `role.permissions` → extracts `permission_pattern` from each permission
     in the role.
7. Stores permissions in `request.state.user_permissions`.
8. Stores the user in `request.state.current_user`.
9. Returns the user.

### `get_current_user_optional(request, db) -> Any | None`

Same as `get_current_user` but returns `None` instead of raising 401. Useful for public views
that render different content depending on whether a session exists (e.g. a "My Account" button
vs. a "Login" button).

### Typed aliases

```python
CurrentUser  = Annotated[Any, Depends(get_current_user)]
OptionalUser = Annotated[Any | None, Depends(get_current_user_optional)]
```

Used in route signatures:

```python
@router.get("/dashboard")
async def dashboard(user: CurrentUser):
    ...
```

### Registry dependencies

```python
def get_event_bus(request: Request) -> AsyncEventBus: ...
def get_hooks(request: Request) -> HookRegistry: ...
def get_slots(request: Request) -> SlotRegistry: ...

EventBus = Annotated["AsyncEventBus", Depends(get_event_bus)]
Hooks    = Annotated["HookRegistry",  Depends(get_hooks)]
Slots    = Annotated["SlotRegistry",  Depends(get_slots)]
```

Access to global registries from any route. All read from `request.app.state` and raise
`HTTP 503` if the registry is not initialised (which only happens if `create_app` did not run
correctly).

---

## auth/permissions.py — Permission engine

### `has_permission(user_permissions: list[str], required: str) -> bool`

```python
def has_permission(user_permissions: list[str], required: str) -> bool:
    for perm in user_permissions:
        if perm == "*":           return True
        if perm == required:      return True
        if fnmatch(required, perm): return True
    return False
```

Supports three matching modes:

| User pattern | Required permission | Result |
|---|---|---|
| `*` | anything | `True` — superadmin |
| `inventory.view_product` | `inventory.view_product` | `True` — exact match |
| `inventory.*` | `inventory.view_product` | `True` — wildcard via `fnmatch` |
| `pos.open_register` | `inventory.view_product` | `False` |

Order matters: if the user has `*`, `True` is returned immediately without iterating further.

### `require_permission(*perms, any_perm=False) -> Depends`

```python
def require_permission(*perms: str, any_perm: bool = False) -> Any:
```

Dependency factory. Creates a `Depends` that verifies permissions when the route is resolved.

- `any_perm=False` (default): the user must hold **all** listed permissions.
- `any_perm=True`: holding **any one** of them is sufficient.

```python
# Requires both permissions
@router.delete("/products/{id}", dependencies=[Depends(require_permission("inventory.delete_product", "admin.modify"))])

# Requires at least one of the two
@router.get("/reports", dependencies=[Depends(require_permission("reports.view", "admin.all", any_perm=True))])
```

Note: the inner `_check_permissions` dependency reads `request.state.user_permissions` that was
populated by `get_current_user`. If `get_current_user` has not run first, the list will be
empty and the check will fail.

### `RequireAdmin`

```python
RequireAdmin = Depends(_require_admin)
```

Shortcut for requiring the `*` permission. Used on administration routes:

```python
@router.get("/admin/modules", dependencies=[RequireAdmin])
```

---

## auth/rate_limit.py — `PINRateLimiter`

An in-memory rate limiter designed specifically to defend against brute-force attacks on PINs.

### Escalating thresholds

```python
THRESHOLDS: list[tuple[int, int | None]] = [
    (5,  5 * 60),   # 5 attempts  → 5-minute lockout
    (10, 30 * 60),  # 10 attempts → 30-minute lockout
    (20, None),     # 20 attempts → permanent lockout
]
```

Lockouts are progressive: the more failed attempts, the longer the wait. `None` indicates a
permanent lockout that requires manual unlocking by an admin.

### `_AttemptRecord`

```python
@dataclass(slots=True)
class _AttemptRecord:
    attempts: int = 0
    locked_until: float | None = None  # monotonic timestamp; None = permanent
    permanently_locked: bool = False
```

One record per key (device token or IP). `slots=True` optimises memory usage.

### `RateLimitResult`

```python
@dataclass
class RateLimitResult:
    allowed: bool
    retry_after: int | None = None  # seconds until unlock; None if permanent
```

### `PINRateLimiter`

```python
class PINRateLimiter:
    def __init__(self) -> None:
        self._records: dict[str, _AttemptRecord] = {}
        self._lock = Lock()
```

Thread-safe via `threading.Lock`. Uses prefixed keys:
- `dev:{device_token}` when a device token is present.
- `ip:{ip}` when no device token is available.
- `"unknown"` as a fallback.

The device token takes priority over IP because it is more specific (multiple users may share
the same IP on a corporate network).

#### `check_rate_limit(device_token, ip) -> RateLimitResult`

Queries the current state without modifying it. If a temporary lockout has expired
(`locked_until < now`), it clears it and allows the attempt.

#### `record_failed_attempt(device_token, ip) -> RateLimitResult`

Increments the counter and applies the highest exceeded threshold (iterates
`reversed(THRESHOLDS)` to always enforce the worst applicable penalty).

#### `record_success(device_token, ip) -> None`

Deletes the record entirely. A successful login resets the counter to zero.

#### `unlock_device(device_token, ip) -> None`

Admin action: deletes the record regardless of its state, unlocking even permanent lockouts.

#### `get_status(device_token, ip) -> dict`

Returns a dict with `attempts`, `locked`, `permanently_locked`, and `retry_after`. Useful for
diagnostic endpoints.

---

## auth/session_helpers.py — Session decoding for WebSocket

### The problem it solves

Starlette's `SessionMiddleware` decodes the session for HTTP requests. WebSocket connections
receive the same scope (including cookies) but **the middleware does not decode the session
before the WebSocket handler runs**. This means `websocket.session` is not available in
WebSocket handlers the same way it is in HTTP handlers.

### `get_session_data(scope_or_request) -> dict[str, Any]`

```python
def get_session_data(scope_or_request: _HasCookies) -> dict[str, Any]:
```

Accepts any object with a `.cookies: dict[str, str]` attribute (a `Request`, a `WebSocket`,
or any `HTTPConnection`). Decodes Starlette's session cookie manually:

1. Reads the cookie named `settings.SESSION_COOKIE_NAME`.
2. Verifies the signature with `itsdangerous.TimestampSigner` using `settings.SECRET_KEY`.
3. Checks that it has not expired (`max_age=settings.SESSION_MAX_AGE`).
4. Decodes the base64 JSON payload.
5. If any step fails, returns `{}` without propagating exceptions.

```python
# In a WebSocket handler:
from hotframe.auth.session_helpers import get_session_data
from hotframe.auth.auth import SESSION_USER_KEY
from uuid import UUID

async def ws_handler(websocket: WebSocket):
    data = get_session_data(websocket)
    user_id_raw = data.get(SESSION_USER_KEY)
    if not user_id_raw:
        await websocket.close(code=4001)
        return
    user_id = UUID(user_id_raw)
    await websocket.accept()
    ...
```

---

## How it fits into the rest of the framework

- **`@view` (HTML views)**: uses `get_current_user` and `require_permission` for authentication
  and authorisation. The CSRF token from `request.state.csrf_token` (set by `CSRFMiddleware`)
  is injected into the template context automatically.
- **`LiveRuntime` (WebSocket)**: uses `get_session_data` from `session_helpers.py` to
  authenticate the WebSocket connection before accepting it.
- **Middlewares in `create_app`**: `CSRFMiddleware` and the CSP middleware are mounted in the
  stack automatically.
- **`settings.py`**: `CSP_ENFORCE`, `CSP_ALLOWED_SOURCES`, `CSP_TRUSTED_TYPES`,
  `CSRF_EXEMPT_PREFIXES`, `AUTH_USER_MODEL`, `SECRET_KEY`, `SESSION_COOKIE_NAME`,
  `SESSION_MAX_AGE`, `HUB_SECRETS_KEY`, and `HUB_DEPLOYMENT_MODE` control the behaviour of
  all these modules.
- **`db/protocols.py`**: `get_db` in `current_user.py` returns `ISession`, not `AsyncSession`,
  to decouple application code from SQLAlchemy.

---

## Gotchas and design decisions

**`create_session` stores the UUID as a `str`.** Starlette serialises the session as JSON.
UUID is not natively JSON-serialisable, so it is converted to `str` on write and parsed back
with `UUID(str(raw))` on read. The `str(raw)` conversion before `UUID()` guards against the
case where the value is already a string.

**`_get_fernet` is cached with `lru_cache(maxsize=1)`.** Building a `Fernet` instance involves
decoding the key. Caching it avoids that overhead on every operation. The downside: if the
environment key changes (only in tests), `reset_cache()` must be called explicitly.

**`build_csp_header` reads settings on every call.** It does not cache because the nonce is
different per request and the header is built fresh each time. Reading `CSP_ALLOWED_SOURCES` is
O(1) since `get_settings()` returns a singleton.

**`CSRFMiddleware` does not protect WebSocket.** WebSockets do not have the classic CSRF
problem (Same-Origin Policy + the fact that the server validates the session before `accept`).
The middleware detects the `Upgrade: websocket` header and lets the request through without
validation.

**`has_permission` uses `fnmatch`.** `fnmatch` is case-sensitive on Unix and case-insensitive
on Windows. Permissions in hotframe must always be lowercase to guarantee consistent behaviour
across platforms.

**`PINRateLimiter` is a singleton per process.** If you deploy with multiple workers
(Gunicorn multi-process), each process has its own in-memory counter. For coordinated
multi-process protection you would need a Redis backend. The documentation notes this as a
known limitation.

**`get_current_user` resolves permissions via three strategies with defined priority.** The
`is_admin → get_permissions() → role.permissions` logic is progressive duck-typing that allows
the user model to be fully customisable without implementing a specific interface. The trade-off
is that if the model has a `role` attribute but no `role.permissions`, permissions will be
silently empty.
