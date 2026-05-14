# 16. Seguridad y autenticación (auth/)

> Propósito: implementar la pila de seguridad completa de hotframe — sesiones, hashing de contraseñas y PINs, cifrado simétrico de secretos, protección CSRF, cabeceras CSP, comprobación de permisos con wildcards, rate limiting con bloqueo escalonado, y decodificación de sesiones en WebSocket.

---

## Para qué sirve esta carpeta

`auth/` cubre todo lo que se interpone entre una petición HTTP y el código de aplicación desde el punto de vista de la seguridad. Cada archivo tiene una responsabilidad acotada y bien definida:

- Gestión de sesiones de usuario (quién está logueado).
- Hashing y verificación de contraseñas y PINs.
- Cifrado simétrico de secretos en reposo.
- Generación y validación de tokens CSRF.
- Construcción de cabeceras Content-Security-Policy con nonce dinámico.
- Dependency injection de usuario actual y permisos.
- Sistema de permisos con wildcards.
- Rate limiting con bloqueos escalonados.
- Decodificación de cookies de sesión fuera del ciclo HTTP (WebSocket).

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`auth/__init__.py`](../src/hotframe/auth/__init__.py) | Documentación del paquete y re-exportaciones |
| [`auth/auth.py`](../src/hotframe/auth/auth.py) | Sesiones: `create_session`, `destroy_session`, `hash_password`, `verify_password`, `hash_pin`, `verify_pin`, `get_session_user_id` |
| [`auth/crypto.py`](../src/hotframe/auth/crypto.py) | Cifrado simétrico Fernet: `encrypt_secret`, `decrypt_secret`, `generate_key` |
| [`auth/csp.py`](../src/hotframe/auth/csp.py) | Construcción de la cabecera CSP: `build_csp_header` |
| [`auth/csrf.py`](../src/hotframe/auth/csrf.py) | Middleware CSRF double-submit cookie: `CSRFMiddleware` |
| [`auth/current_user.py`](../src/hotframe/auth/current_user.py) | FastAPI dependencies: `get_current_user`, `get_db`, `DbSession`, `CurrentUser`, etc. |
| [`auth/permissions.py`](../src/hotframe/auth/permissions.py) | Motor de permisos: `has_permission`, `require_permission`, `RequireAdmin` |
| [`auth/rate_limit.py`](../src/hotframe/auth/rate_limit.py) | Rate limiter en memoria con bloqueos escalonados: `PINRateLimiter` |
| [`auth/session_helpers.py`](../src/hotframe/auth/session_helpers.py) | Decodificación de sesión Starlette para WebSocket: `get_session_data` |

---

## auth/auth.py — Sesiones y hashing

Este es el núcleo operativo del flujo login/logout. Gestiona dos cosas distintas: el estado de la sesión HTTP y el hashing de credenciales.

### Constante `SESSION_USER_KEY`

```python
SESSION_USER_KEY = "user_id"
```

La clave bajo la que se guarda el UUID del usuario en el dict de sesión de Starlette. Centralizar esta constante evita que diferentes partes del código usen claves diferentes por error.

### `hash_password(password: str) -> str`

```python
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
```

Genera un hash bcrypt de la contraseña. `bcrypt.gensalt()` genera un salt aleatorio en cada llamada, por lo que dos llamadas con la misma contraseña producen hashes distintos. El resultado es una cadena UTF-8 que se puede almacenar directamente en la DB.

### `verify_password(password: str, password_hash: str) -> bool`

```python
def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False
```

Verifica contraseña contra su hash bcrypt. Captura `ValueError` y `TypeError` para devolver `False` en lugar de propagar excepciones si el hash está malformado — comportamiento defensivo importante para no exponer información en logs de error.

### `hash_pin(pin: str) -> str` y `verify_pin(pin: str, pin_hash: str) -> bool`

Idénticos en lógica a sus equivalentes de contraseña, pero semánticamente distintos: un PIN es típicamente 4-8 dígitos y tiene su propio rate limiter (`PINRateLimiter` en `rate_limit.py`).

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

Lee el UUID del usuario de la sesión. El try/except protege contra sesiones corruptas donde el valor almacenado no sea un UUID válido.

### `create_session(request: Request, user_id: UUID) -> None`

```python
def create_session(request: Request, user_id: UUID) -> None:
    request.session[SESSION_USER_KEY] = str(user_id)
```

Persiste el UUID del usuario en la sesión. El UUID se serializa a `str` porque `request.session` es un dict JSON-serializable. La cookie se firma con `SECRET_KEY` por el `SessionMiddleware` de Starlette — no hay que hacerlo manualmente.

### `destroy_session(request: Request) -> None`

```python
def destroy_session(request: Request) -> None:
    request.session.clear()
```

Limpia todos los datos de la sesión. El `SessionMiddleware` se encargará de borrar la cookie en la respuesta.

---

## auth/crypto.py — Cifrado simétrico Fernet

Este módulo protege **secretos en reposo** — tokens de API de terceros, credenciales de integraciones, certificados — que se guardan cifrados en la base de datos.

### Excepciones propias

```python
class SecretsKeyMissingError(RuntimeError): ...
class SecretDecryptionError(RuntimeError): ...
```

`SecretsKeyMissingError` se lanza al arrancar si `HUB_SECRETS_KEY` no está configurada en un entorno no-local. `SecretDecryptionError` se lanza al desencriptar si la clave es incorrecta o el texto está manipulado.

### `_get_fernet() -> Fernet` (cacheada)

```python
@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
```

Construye la instancia `Fernet` una sola vez por proceso. Lee `HUB_SECRETS_KEY` del entorno. Si no está definida y `HUB_DEPLOYMENT_MODE` no es `"local"`, lanza `SecretsKeyMissingError` — fail-fast en producción. En local, deriva una clave desde `HUB_SECRET_KEY` con SHA-256 y emite un `WARNING` en el log.

### `encrypt_secret(plaintext: str) -> str`

```python
def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")
```

Cadenas vacías se devuelven vacías sin cifrar. Esto evita almacenar valores cifrados de campos opcionales que aún no tienen valor — importante para no confundir "sin valor" con "valor cifrado vacío".

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

Lanza `SecretDecryptionError` (no `InvalidToken` de cryptography) — así el código de aplicación no necesita importar `cryptography` para manejar el error.

### `generate_key() -> str`

```python
def generate_key() -> str:
    return Fernet.generate_key().decode("utf-8")
```

Genera una clave Fernet fresca. Útil en scripts de bootstrap o en la documentación de onboarding.

### `reset_cache() -> None`

```python
def reset_cache() -> None:
    _get_fernet.cache_clear()
```

Limpia el caché LRU. Imprescindible en los tests que cambian `HUB_SECRETS_KEY` mid-run.

---

## auth/csp.py — Content-Security-Policy

### `build_csp_header(nonce: str, enforce: bool) -> tuple[str, str]`

```python
def build_csp_header(nonce: str, enforce: bool) -> tuple[str, str]:
```

Retorna `(nombre_cabecera, valor_cabecera)`.

- Si `enforce=True`: `Content-Security-Policy` (bloquea).
- Si `enforce=False`: `Content-Security-Policy-Report-Only` (solo reporta, no bloquea). Útil para activar CSP gradualmente en producción sin romper nada.

Las directivas base generadas:

| Directiva | Valor base |
|---|---|
| `default-src` | `'self'` |
| `script-src` | `'self' 'nonce-{nonce}' 'unsafe-eval'` + fuentes extra |
| `style-src` | `'self' 'unsafe-inline'` + fuentes extra |
| `img-src` | `'self' data: blob:` + fuentes extra |
| `connect-src` | `'self' wss://*` (en enforce) o `'self' ws://localhost:* wss://*` (en report-only) |
| `font-src` | `'self'` + fuentes extra |
| `object-src` | `'none'` |
| `base-uri` | `'self'` |
| `form-action` | `'self'` |
| `frame-ancestors` | `'none'` |

Nótese que `ws://localhost:*` solo aparece en modo report-only (desarrollo), no en enforce (producción). Esto evita bloquear el WebSocket local en dev.

Si `settings.CSP_TRUSTED_TYPES` es `True`, añade:
```
require-trusted-types-for 'script'
trusted-types default iconify 'allow-duplicates'
```

Las fuentes extra se leen de `settings.CSP_ALLOWED_SOURCES`, un dict con claves `"script"`, `"style"`, `"connect"`, `"img"`, `"font"`.

---

## auth/csrf.py — Middleware CSRF double-submit cookie

### Constantes

```python
COOKIE_NAME = "csrf_token"
HEADER_NAME = "x-csrf-token"
FORM_FIELD  = "csrf_token"
```

El token viaja en tres lugares: cookie, cabecera HTTP, o campo de formulario. El middleware acepta cualquiera de los tres como prueba.

### `generate_csrf_token() -> str`

```python
def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)
```

32 bytes URL-safe = 256 bits de entropía. Generado con `secrets` (CSPRNG).

### `CSRFMiddleware`

```python
class CSRFMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, exempt_prefixes: tuple[str, ...] | None = None) -> None:
```

Implementa la estrategia **double-submit cookie**: el token está en la cookie y también tiene que aparecer en el cuerpo o cabecera de la petición. Un atacante en otro origen no puede leer la cookie (Same-Origin Policy), por tanto no puede incluir el token en la petición.

#### Flujo en `dispatch`

1. **WebSocket upgrade**: pasa directamente (`call_next`), sin procesamiento CSRF. Los WebSocket tienen su propia auth via sesión.
2. **Rutas `/static/`**: pasan directamente para que las CDN puedan cachear sin cookies.
3. **Lectura del token existente**: busca la cookie `csrf_token`. Si no existe, genera uno nuevo (`new_token = True`).
4. **Almacena en `request.state.csrf_token`**: para que las plantillas Jinja2 puedan acceder a él.
5. **Métodos inseguros (POST/PUT/PATCH/DELETE)**: si la ruta no está en `_exempt_prefixes`, valida que el token enviado coincida con el de la cookie usando `secrets.compare_digest` (tiempo constante, resistente a timing attacks).
6. **Set-Cookie**: si el token es nuevo, lo pone en la respuesta con `samesite="lax"`, `httponly=False` (tiene que ser legible por JavaScript para enviarlo como cabecera), y `secure=True` si la conexión es HTTPS.

#### Rutas exentas

```python
self._exempt_prefixes = tuple(settings.CSRF_EXEMPT_PREFIXES)
```

Por defecto exentos: `/api/`, `/health`, `/static/`. Las rutas API REST que usan tokens Bearer no necesitan CSRF.

#### `_get_submitted_token`

Busca el token en este orden:
1. Cabecera `x-csrf-token` (para peticiones AJAX/fetch).
2. Campo `csrf_token` en formulario `application/x-www-form-urlencoded` o `multipart/form-data`.

---

## auth/current_user.py — Dependency injection

Este archivo centraliza los FastAPI dependencies más usados del framework.

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

Dependency que cede una sesión de DB. Commit al finalizar sin error, rollback en cualquier excepción. El tipo retornado es `ISession` (protocolo), no `AsyncSession` directamente.

`DbSession = Annotated[ISession, Depends(get_db)]` es el alias que usas en las firmas de ruta:

```python
@router.get("/products")
async def list_products(db: DbSession):
    ...
```

### `get_current_user(request, db) -> Any`

```python
async def get_current_user(request: Request, db: DbSession) -> Any:
```

Flujo completo:

1. Lee `user_id` de la sesión con `get_session_user_id`.
2. Si no hay `user_id`, lanza `HTTP 401`.
3. Importa dinámicamente la clase de usuario desde `settings.AUTH_USER_MODEL`.
4. Consulta `SELECT * FROM users WHERE id = ? AND is_active = true`.
5. Si no encuentra el usuario, lanza `HTTP 401`.
6. Resuelve permisos en este orden:
   - Si `user.is_admin` → `["*"]` (superusuario).
   - Si el usuario tiene método `get_permissions()` → llama y usa el resultado.
   - Si el usuario tiene `role.permissions` → extrae `permission_pattern` de cada permiso del rol.
7. Almacena permisos en `request.state.user_permissions`.
8. Almacena usuario en `request.state.current_user`.
9. Retorna el usuario.

### `get_current_user_optional(request, db) -> Any | None`

Igual que `get_current_user` pero retorna `None` en lugar de lanzar 401. Útil para vistas públicas que muestran contenido diferente si hay sesión (p.ej. un botón de "Mi cuenta" vs "Login").

### Aliases tipados

```python
CurrentUser  = Annotated[Any, Depends(get_current_user)]
OptionalUser = Annotated[Any | None, Depends(get_current_user_optional)]
```

Se usan en firmas de rutas:

```python
@router.get("/dashboard")
async def dashboard(user: CurrentUser):
    ...
```

### Dependencies de registries

```python
def get_event_bus(request: Request) -> AsyncEventBus: ...
def get_hooks(request: Request) -> HookRegistry: ...
def get_slots(request: Request) -> SlotRegistry: ...

EventBus = Annotated["AsyncEventBus", Depends(get_event_bus)]
Hooks    = Annotated["HookRegistry",  Depends(get_hooks)]
Slots    = Annotated["SlotRegistry",  Depends(get_slots)]
```

Acceso a los registros globales desde cualquier ruta. Todos leen de `request.app.state` y lanzan `HTTP 503` si el registry no está inicializado (lo que solo ocurre si `create_app` no se ejecutó correctamente).

---

## auth/permissions.py — Motor de permisos

### `has_permission(user_permissions: list[str], required: str) -> bool`

```python
def has_permission(user_permissions: list[str], required: str) -> bool:
    for perm in user_permissions:
        if perm == "*":           return True
        if perm == required:      return True
        if fnmatch(required, perm): return True
    return False
```

Soporta tres formas de matching:

| Patrón de usuario | Permiso requerido | Resultado |
|---|---|---|
| `*` | cualquiera | `True` — superadmin |
| `inventory.view_product` | `inventory.view_product` | `True` — match exacto |
| `inventory.*` | `inventory.view_product` | `True` — wildcard con `fnmatch` |
| `pos.open_register` | `inventory.view_product` | `False` |

El orden importa: si el usuario tiene `*`, se retorna `True` inmediatamente sin iterar más.

### `require_permission(*perms, any_perm=False) -> Depends`

```python
def require_permission(*perms: str, any_perm: bool = False) -> Any:
```

Factory de dependency. Crea un `Depends` que verifica permisos al resolver la ruta.

- `any_perm=False` (defecto): el usuario debe tener TODOS los permisos listados.
- `any_perm=True`: basta con tener UNO.

```python
# Requiere ambos permisos
@router.delete("/products/{id}", dependencies=[Depends(require_permission("inventory.delete_product", "admin.modify"))])

# Requiere cualquiera de los dos
@router.get("/reports", dependencies=[Depends(require_permission("reports.view", "admin.all", any_perm=True))])
```

Nota: el dependency interno `_check_permissions` lee `request.state.user_permissions` que cargó `get_current_user`. Si `get_current_user` no se ejecutó antes, la lista estará vacía y la verificación fallará.

### `RequireAdmin`

```python
RequireAdmin = Depends(_require_admin)
```

Shortcut para exigir el permiso `*`. Se usa en rutas de administración:

```python
@router.get("/admin/modules", dependencies=[RequireAdmin])
```

---

## auth/rate_limit.py — `PINRateLimiter`

Rate limiter en memoria diseñado específicamente para ataques de fuerza bruta sobre PINs.

### Umbrales escalonados

```python
THRESHOLDS: list[tuple[int, int | None]] = [
    (5,  5 * 60),   # 5 intentos  → 5 min de bloqueo
    (10, 30 * 60),  # 10 intentos → 30 min de bloqueo
    (20, None),     # 20 intentos → bloqueo permanente
]
```

El bloqueo es progresivo: a mayor número de intentos fallidos, más tiempo de espera. `None` indica bloqueo permanente que requiere desbloqueado manual por un admin.

### `_AttemptRecord`

```python
@dataclass(slots=True)
class _AttemptRecord:
    attempts: int = 0
    locked_until: float | None = None  # timestamp monotónico, None = permanente
    permanently_locked: bool = False
```

Un record por clave (device_token o IP). `slots=True` optimiza memoria.

### `RateLimitResult`

```python
@dataclass
class RateLimitResult:
    allowed: bool
    retry_after: int | None = None  # segundos hasta desbloqueo, None si permanente
```

### `PINRateLimiter`

```python
class PINRateLimiter:
    def __init__(self) -> None:
        self._records: dict[str, _AttemptRecord] = {}
        self._lock = Lock()
```

Thread-safe mediante `threading.Lock`. Usa claves prefijadas:
- `dev:{device_token}` si hay device token.
- `ip:{ip}` si no hay device token.
- `"unknown"` como fallback.

El device token tiene prioridad porque es más específico que la IP (varios usuarios pueden compartir la misma IP en una red corporativa).

#### `check_rate_limit(device_token, ip) -> RateLimitResult`

Consulta el estado sin modificarlo. Si el bloqueo temporal ha expirado (`locked_until < now`), lo limpia y permite el intento.

#### `record_failed_attempt(device_token, ip) -> RateLimitResult`

Incrementa el contador y aplica el umbral más alto superado (itera en `reversed(THRESHOLDS)` para aplicar siempre la peor penalización vigente).

#### `record_success(device_token, ip) -> None`

Elimina el record completo. Un login exitoso resetea el contador a cero.

#### `unlock_device(device_token, ip) -> None`

Acción de admin: elimina el record independientemente del estado. Desbloquea incluso bloqueos permanentes.

#### `get_status(device_token, ip) -> dict`

Retorna un dict con `attempts`, `locked`, `permanently_locked`, `retry_after`. Útil para endpoints de diagnóstico.

---

## auth/session_helpers.py — Decodificación de sesión en WebSocket

### El problema que resuelve

`SessionMiddleware` de Starlette decodifica la sesión para peticiones HTTP. Los WebSocket reciben el mismo scope (con las cookies) pero **el middleware no decodifica la sesión antes de que el handler del WebSocket corra**. Esto significa que `websocket.session` no está disponible en los handlers de WebSocket de la misma forma que en HTTP.

### `get_session_data(scope_or_request) -> dict[str, Any]`

```python
def get_session_data(scope_or_request: _HasCookies) -> dict[str, Any]:
```

Acepta cualquier objeto que tenga `.cookies: dict[str, str]` (un `Request`, un `WebSocket`, o cualquier `HTTPConnection`). Decodifica la cookie de sesión de Starlette manualmente:

1. Lee la cookie `settings.SESSION_COOKIE_NAME`.
2. Verifica la firma con `itsdangerous.TimestampSigner` usando `settings.SECRET_KEY`.
3. Comprueba que no haya expirado (`max_age=settings.SESSION_MAX_AGE`).
4. Decodifica el JSON base64.
5. Si cualquier paso falla, retorna `{}` (sin propagar excepciones).

```python
# En un handler WebSocket:
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

## Cómo encaja con el resto del framework

- **`@view` (vistas HTML)**: usa `get_current_user` y `require_permission` para auth y permisos. El CSRF token de `request.state.csrf_token` (puesto por `CSRFMiddleware`) llega al contexto de la plantilla automáticamente.
- **`LiveRuntime` (WebSocket)**: usa `get_session_data` de `session_helpers.py` para autenticar la conexión WebSocket antes de aceptarla.
- **Middlewares en `create_app`**: `CSRFMiddleware` y el middleware CSP se montan en la pila automáticamente.
- **`settings.py`**: `CSP_ENFORCE`, `CSP_ALLOWED_SOURCES`, `CSP_TRUSTED_TYPES`, `CSRF_EXEMPT_PREFIXES`, `AUTH_USER_MODEL`, `SECRET_KEY`, `SESSION_COOKIE_NAME`, `SESSION_MAX_AGE`, `HUB_SECRETS_KEY`, `HUB_DEPLOYMENT_MODE` controlan el comportamiento de todos estos módulos.
- **`db/protocols.py`**: `get_db` en `current_user.py` retorna `ISession`, no `AsyncSession`, para desacoplar el código de aplicación de SQLAlchemy.

---

## Gotchas y decisiones de diseño

**`create_session` guarda el UUID como `str`.** Starlette serializa la sesión como JSON. UUID no es JSON-serializable de forma nativa, así que se convierte a `str` al guardar y se parsea de vuelta con `UUID(str(raw))` al leer. La conversión `str(raw)` antes de `UUID()` protege contra el caso donde ya es un string.

**`_get_fernet` está cacheada con `lru_cache(maxsize=1)`.** Construir `Fernet` implica decodificar la clave. Cachearla evita ese overhead en cada operación. El inconveniente: si la clave del entorno cambia (solo pasa en tests), hay que llamar a `reset_cache()` explícitamente.

**`build_csp_header` lee settings en cada llamada.** No usa caché porque el nonce es distinto por petición y el header se construye fresco. La lectura de `CSP_ALLOWED_SOURCES` es O(1) ya que `get_settings()` retorna un singleton.

**`CSRFMiddleware` no defiende WebSocket.** Los WebSocket no tienen el problema CSRF clásico (Same-Origin Policy + el hecho de que el servidor valida la sesión antes de `accept`). El middleware detecta el header `Upgrade: websocket` y deja pasar la petición sin validar.

**`has_permission` usa `fnmatch`.** `fnmatch` es case-sensitive en Unix y case-insensitive en Windows. Los permisos en hotframe deben ser siempre en minúsculas para garantizar comportamiento consistente.

**`PINRateLimiter` es un singleton por proceso.** Si despliegas con múltiples workers (Gunicorn multi-process), cada proceso tiene su propio contador en memoria. Para protección coordinada multi-proceso necesitarías un backend Redis. La documentación lo menciona como limitación conocida.

**`get_current_user` resuelve permisos en tres estrategias con prioridad.** La lógica `is_admin → get_permissions() → role.permissions` es un duck-typing progresivo que permite que el modelo de usuario sea completamente personalizable sin implementar una interfaz específica. El precio es que si el modelo tiene `role` pero no `role.permissions`, los permisos quedan vacíos silenciosamente.
