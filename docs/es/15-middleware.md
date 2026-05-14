# 15. La pila de middleware (`middleware/`)

> Capa de intercepción que envuelve toda petición HTTP antes de que llegue
> a una vista, y toda respuesta antes de salir al cliente. Aquí vive la
> seguridad, la observabilidad, el control de recursos y la extensibilidad
> de módulos dinámicos — todo activado por defecto, sin una sola línea en
> tu código de aplicación.

---

## Para qué sirve esta carpeta

En FastAPI/Starlette el middleware es una cadena de wrappers ASGI que se
construye una sola vez al arrancar y se aplica a cada petición. hotframe
centraliza aquí tres responsabilidades:

1. **Seguridad** — CSP con nonce por petición, sesiones robustas, CSRF
   (gestionado desde `auth/csrf.py` pero orquestado aquí), rate limiting
   y límite de cuerpo.
2. **Infraestructura** — timeout global, proxy fix, observabilidad con
   OpenTelemetry, i18n.
3. **Hot-mount** — soporte para añadir y retirar middleware de módulos
   dinámicos en runtime sin reiniciar el proceso.

La carpeta expone dos puntos de entrada públicos:

```python
from hotframe.middleware.stack import build_middleware_stack
from hotframe.middleware.stack_manager import MiddlewareStackManager
```

`build_middleware_stack` se llama una vez en `create_app`. `MiddlewareStackManager`
se usa desde el `ModuleRuntime` cada vez que se activa o desactiva un módulo.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`__init__.py`](../src/hotframe/middleware/__init__.py) | Docstring de módulo, exportaciones conceptuales |
| [`body_limit.py`](../src/hotframe/middleware/body_limit.py) | Rechaza peticiones con `Content-Length` excesivo (413) |
| [`csp.py`](../src/hotframe/middleware/csp.py) | Genera nonce por petición, añade cabeceras CSP y HSTS |
| [`error_pages.py`](../src/hotframe/middleware/error_pages.py) | Captura excepciones no manejadas, renderiza HTML o JSON de error |
| [`i18n_support.py`](../src/hotframe/middleware/i18n_support.py) | Motor de traducción con gettext, dominios por módulo, `LazyString` |
| [`language.py`](../src/hotframe/middleware/language.py) | Detecta y activa el idioma de cada petición (5 fuentes en cascada) |
| [`module_middleware.py`](../src/hotframe/middleware/module_middleware.py) | Delega en el middleware registrado por módulos activos |
| [`observability.py`](../src/hotframe/middleware/observability.py) | Vincula contexto de observabilidad, graba histograma de duración |
| [`proxy_fix.py`](../src/hotframe/middleware/proxy_fix.py) | Reescribe host/scheme del scope ASGI cuando hay proxy inverso |
| [`rate_limit.py`](../src/hotframe/middleware/rate_limit.py) | Rate limiting por IP con ventana deslizante en memoria, 3 buckets |
| [`session_safe.py`](../src/hotframe/middleware/session_safe.py) | Wrapper de `SessionMiddleware` que absorbe cookies corruptas |
| [`stack.py`](../src/hotframe/middleware/stack.py) | Constructor de la pila en el arranque desde `settings.MIDDLEWARE` |
| [`stack_manager.py`](../src/hotframe/middleware/stack_manager.py) | Reconstrucción atómica de la pila para hot-mount en runtime |
| [`timeout.py`](../src/hotframe/middleware/timeout.py) | Cancela peticiones que superan el umbral (default 30 s) |

---

## `stack.py` — el constructor al arranque

[`middleware/stack.py`](../src/hotframe/middleware/stack.py) es el único
archivo que se llama durante `create_app`. Todo su trabajo lo hace una
función pública.

### `build_middleware_stack(app, settings)`

```python
def build_middleware_stack(app: FastAPI, settings: HotframeSettings) -> None:
```

Lee `settings.MIDDLEWARE` — una lista de dotted paths como
`"hotframe.middleware.csp.CSPMiddleware"` — y añade cada clase al app de
FastAPI. La trampa de Starlette es que **la última clase añadida es la más
exterior** (la que ve primero cada petición). Como `settings.MIDDLEWARE`
está en orden "de exterior a interior", el loop itera en reverso:

```python
for dotted_path in reversed(settings.MIDDLEWARE):
    cls = _import_class(dotted_path)
    kwargs = _get_middleware_kwargs(cls, settings)
    app.add_middleware(cls, **kwargs)
```

Esto garantiza que el orden declarativo en `settings.py` coincide con el
orden real de ejecución.

### `_import_class(dotted_path)`

```python
def _import_class(dotted_path: str) -> type:
```

Hace un `importlib.import_module` del módulo e `getattr` del nombre de
clase. Lanza excepción si el path no existe, propagándola para que
`create_app` falle rápido y visible.

### `_get_middleware_kwargs(cls, settings)`

```python
def _get_middleware_kwargs(cls: type, settings: HotframeSettings) -> dict[str, Any]:
```

Función de dispatch que mapea clases conocidas a sus kwargs. Evita que
cada middleware tenga que leer `settings` directamente — el builder es el
único que sabe qué parámetro de settings va a qué middleware:

| Clase | Parámetros construidos |
|---|---|
| `RobustSessionMiddleware` | `secret_key`, `max_age`, `session_cookie`, `same_site`, `https_only` |
| `CSPMiddleware` | `enforce` |
| `APIRateLimitMiddleware` | `api_rate`, `auth_rate` (10 000 en DEBUG), `window`, `auth_prefixes` |
| `BodyLimitMiddleware` | `max_bytes` |
| `TimeoutMiddleware` | `timeout=30` |
| `ModuleMiddlewareManager` | `registry=None` (se rellena después por el ModuleRuntime) |

Para cualquier otra clase devuelve `{}` — el middleware recibe solo `app`.

---

## `stack_manager.py` — reconstrucción atómica para hot-mount

[`middleware/stack_manager.py`](../src/hotframe/middleware/stack_manager.py)
resuelve un problema no trivial: Starlette construye `app._middleware_stack`
**una sola vez**, la primera petición, y lo cachea. Si después añades o
quitas middleware con `app.add_middleware`, el cambio no tiene efecto en
peticiones ya en vuelo ni en las nuevas hasta la próxima reconstrucción.

`MiddlewareStackManager` expone una API async-safe que:

1. Invalida el caché.
2. Ejecuta una función `compose_stack` dentro del lock para mutar
   `app.user_middleware`.
3. Fuerza la reconstrucción inmediata con `app.build_middleware_stack()`.

Todo bajo `asyncio.Lock` para serializar reconstrucciones concurrentes.

### Constructor

```python
def __init__(self, app: FastAPI) -> None:
    self._app = app
    self._lock = asyncio.Lock()
```

El manager no guarda estado extra — `app.user_middleware` es la única
fuente de verdad.

### `rebuild(compose_stack=None)`

```python
async def rebuild(
    self,
    compose_stack: Callable[[], Awaitable[None]] | None = None,
) -> None:
```

El método central. Algoritmo:

1. Adquiere `self._lock`.
2. Pone `self._app.middleware_stack = None` — esto invalida el caché y
   es necesario porque `add_middleware` rechaza mutaciones si la pila
   ya está construida.
3. Ejecuta `await compose_stack()` si se proporcionó — aquí el llamante
   puede llamar `app.add_middleware(...)` o editar `app.user_middleware`.
4. Llama `self._app.middleware_stack = self._app.build_middleware_stack()`
   para reconstruir la pila de forma eager (así el coste no cae en la
   primera petición siguiente).
5. Libera el lock.

La atomicidad práctica se apoya en que en CPython la asignación de
atributo es una sola operación bytecode, y en que `build_middleware_stack`
es síncrono — no hay yield entre el reset y el rebuild.

### `add_and_rebuild(middleware_class, **options)`

```python
async def add_and_rebuild(
    self,
    middleware_class: type,
    **options: Any,
) -> None:
```

Wrapper de conveniencia. Llama `rebuild` con un `compose` que hace
`self._app.add_middleware(middleware_class, **options)`. El nuevo
middleware queda en el extremo exterior de la pila (se ejecuta primero
en peticiones).

### `remove_and_rebuild(middleware_class)`

```python
async def remove_and_rebuild(self, middleware_class: type) -> None:
```

Elimina todas las entradas cuyo `mw.cls is middleware_class` de
`app.user_middleware` y reconstruye. Idempotente: si la clase no está,
el rebuild ocurre igualmente.

### Por qué funciona para hot-mount

Las peticiones que ya habían entrado a la pila vieja siguen ejecutándose
sobre la clausura que capturaron al entrar. Las peticiones nuevas que
lleguen después de que `rebuild` retorne usan la nueva pila. No hay
periodo de inconsistencia; en el peor caso dos peticiones llegan al mismo
tiempo y ambas ven la pila nueva (la segunda observa el stack ya construido
por la primera).

---

## `body_limit.py` — `BodyLimitMiddleware`

[`middleware/body_limit.py`](../src/hotframe/middleware/body_limit.py)

**Qué intercepta**: toda petición HTTP.

**Qué hace**: lee la cabecera `Content-Length`. Si supera `max_bytes`
(default 10 MB) devuelve 413 sin llegar a leer el cuerpo.

```python
class BodyLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, max_bytes: int = DEFAULT_MAX_BODY) -> None:
```

Atributos clave:

- `_max_bytes`: límite en bytes, configurable desde `settings.MAX_REQUEST_BODY`.
- `DEFAULT_MAX_BODY = 10 * 1024 * 1024` (10 MB).

La comprobación es pasiva: si `Content-Length` no está presente (e.g.
streaming chunked), la petición pasa. Sólo valida cabecera, no el body
real. Esto es intencional — protege contra DoS declarado, no contra
streaming adversarial (para eso haría falta leer el body, con coste propio).

---

## `csp.py` — `CSPMiddleware`

[`middleware/csp.py`](../src/hotframe/middleware/csp.py)

**Qué intercepta**: toda petición y respuesta HTTP.

**Qué hace**:

1. Genera `nonce = secrets.token_urlsafe(32)` — criptográficamente seguro,
   único por petición.
2. Lo guarda en `request.state.csp_nonce` para que las plantillas lo usen
   en `<script nonce="...">`.
3. Construye la cabecera CSP llamando a `build_csp_header(nonce, enforce)`
   (implementada en `hotframe.auth.csp`).
4. En modo `enforce=True` con HTTPS, añade también:
   - `Strict-Transport-Security: max-age=31536000; includeSubDomains`
   - `X-Robots-Tag: noindex, nofollow` (siempre, incluso en HTTP).

```python
class CSPMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, enforce: bool = False) -> None:
```

El flag `enforce` viene de `settings.CSP_ENFORCE`. En `False` la cabecera
es `Content-Security-Policy-Report-Only` (solo reporta, no bloquea). En
`True` es `Content-Security-Policy` (bloquea).

---

## `error_pages.py` — `ErrorPageMiddleware`

[`middleware/error_pages.py`](../src/hotframe/middleware/error_pages.py)

**Qué intercepta**: toda excepción no manejada que burbujee desde la pila.

**Qué hace**:

1. Envuelve `call_next(request)` en `try/except Exception`.
2. Si hay excepción, decide entre HTML o JSON según `Accept` del cliente y
   si la ruta empieza por `/api/`.
3. En `settings.DEBUG=True` incluye el traceback completo en un `<details>`
   colapsable.

### Funciones relevantes

```python
def _wants_json(request: Request) -> bool:
```

Devuelve `True` si `Accept` contiene `application/json` o si el path empieza
por `/api/`.

```python
def _render_error_html(status_code: int, detail: str, tb: str | None = None) -> str:
```

Genera una página de error auto-contenida con estilos inline. Soporta los
códigos: 400, 403, 404, 405, 422, 429, 500, 502, 503.

```python
class ErrorPageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: ...) -> Response:
    def _handle_exception(self, request: Request, exc: Exception) -> Response:
```

`_handle_exception` lee `exc.status_code` y `exc.detail` (atributos de
`HTTPException`) con fallback a `500` y `str(exc)`.

**Gotcha**: el middleware también inspecciona respuestas con `status >= 400`,
pero actualmente no las sobreescribe (la condición termina en `pass`). Está
preparado para una futura expansión que renderice páginas de error para
respuestas vacías de downstream.

---

## `i18n_support.py` — el motor de traducción

[`middleware/i18n_support.py`](../src/hotframe/middleware/i18n_support.py)

Este archivo no es un middleware ASGI, sino **la biblioteca interna de i18n**.
Expone todo lo que el resto del framework necesita para traducir cadenas.

### `ContextVar` de idioma

```python
_current_language: ContextVar[str] = ContextVar("current_language", default=DEFAULT_LANGUAGE)
```

Cada tarea asyncio tiene su propio valor, así dos peticiones concurrentes en
idiomas distintos no se interfieren.

### `activate(language)` / `deactivate()`

```python
def activate(language: str) -> None:
def deactivate() -> None:
```

`activate` valida que el código esté en `SUPPORTED_LANGUAGES` (`en`, `es`)
y lo escribe en el `ContextVar`. `deactivate` lo resetea a `DEFAULT_LANGUAGE`.
El middleware `LanguageMiddleware` llama siempre a `deactivate()` al final
de cada petición para limpiar el contextvar.

### `_(text, module_id=None)`

```python
def _(text: str, module_id: str | None = None) -> str:
```

La función de traducción principal. Cadena de fallback:

1. Si el idioma activo es `en`, devuelve `text` sin consultar nada.
2. Si `module_id` está registrado en `_module_locales`, busca en el
   dominio del módulo.
3. Fallback al dominio `"messages"` del core (`hotframe/locales/`).
4. Si tampoco hay traducción, devuelve `text` original.

### `ngettext(singular, plural, n, module_id=None)`

```python
def ngettext(singular: str, plural: str, n: int, module_id: str | None = None) -> str:
```

Versión pluralizada con la misma cadena de fallback.

### Cache LRU de traducciones

```python
@lru_cache(maxsize=128)
def _get_translation(domain: str, locales_dir: str, language: str) -> ...:
```

Cachea objetos `GNUTranslations` por `(domain, locales_dir, language)`.
La cache se invalida con `_clear_cache()` cada vez que un módulo registra
o desregistra sus locales — `_get_translation.cache_clear()`.

### Registro de locales de módulos

```python
def register_module_locales(module_id: str, locales_dir: Path) -> None:
def unregister_module_locales(module_id: str) -> None:
```

El `ModuleLoader` llama a `register_module_locales` cuando activa un módulo
con carpeta `locales/`, y a `unregister_module_locales` al desactivarlo.

### `LazyString`

```python
class LazyString:
    def __init__(self, text: str, module_id: str | None = None) -> None:
```

Un objeto que se parece a una `str` pero retrasa la traducción hasta que
`str()` o `f"{lazy_str}"` lo evalúa. Útil para constantes de módulo
definidas al import time:

```python
# modules/inventory/module.py
MODULE_NAME = LazyString("Inventory", module_id="inventory")
```

Cuando esto se evalúa en una petición con idioma `es`, `str(MODULE_NAME)` →
`"Inventario"`. Implementa `__str__`, `__repr__`, `__eq__`, `__hash__`,
`__contains__`, `__add__`, `__radd__`, `__len__`, `__bool__` y `__format__`
para comportarse como string en la mayoría de contextos.

La propiedad `.source` devuelve el texto original sin traducir.

### `_RequestTranslations` y `get_translations()`

```python
class _RequestTranslations:
    def gettext(self, message: str) -> str: ...
    def ngettext(self, singular: str, plural: str, n: int) -> str: ...
```

Adaptador para `jinja2.Environment.install_gettext_translations()`.
Delega a `_()` y `ngettext()` del módulo, así Jinja2 respeta el idioma
del request actual.

---

## `language.py` — `LanguageMiddleware`

[`middleware/language.py`](../src/hotframe/middleware/language.py)

**Qué intercepta**: toda petición HTTP (excepto `/static/`).

**Qué hace**: determina el idioma del usuario y lo activa para la duración
de la petición.

```python
class LanguageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: ...) -> Response:
```

### Cascada de detección (en orden de prioridad)

| Fuente | Condición |
|---|---|
| 1. Sesión | `request.state.session["language"]` existe y es válido |
| 2. Cookie | Cookie `_lang` presente y válida |
| 3. Cabecera `Accept-Language` | Parseada con negotiation por calidad (`q=`) |
| 4. Preferencia del usuario | `request.state.user.language` si hay usuario autenticado |
| 5. Settings default | `settings.LANGUAGE` |

Si el idioma detectado no está en `_SUPPORTED_CODES`, se registra un
warning y se cae al default de settings (o a `"en"` si settings tampoco
es válido).

### Cookie de persistencia

Al final de cada petición, si el idioma detectado difiere del que el cliente
envió en la cookie `_lang`, se emite `Set-Cookie: _lang=<lang>; max-age=31536000`.
La cookie tiene `httponly=False` (JavaScript puede leerla para actualizar UI)
y `samesite="lax"`.

Las rutas `/static/` se saltan completamente — ni detección ni cookie —
para no romper el caché de CDN.

### Limpieza del ContextVar

Al retornar la respuesta, siempre se llama `deactivate()` para resetear
el contextvar. Esto es crítico en asyncio: sin el reset, una tarea que
recicle una coroutine podría heredar el idioma de una petición anterior.

---

## `module_middleware.py` — `ModuleMiddlewareManager`

[`middleware/module_middleware.py`](../src/hotframe/middleware/module_middleware.py)

**Qué intercepta**: toda petición y respuesta, pero solo si hay módulos con
middleware registrado.

**Qué hace**: actúa como un meta-middleware que delega en una lista dinámica
de middleware contribuidos por módulos activos.

### `ModuleMiddlewareProtocol`

```python
@runtime_checkable
class ModuleMiddlewareProtocol(Protocol):
    async def process_request(self, request: Request) -> Response | None: ...
    async def process_response(self, request: Request, response: Response) -> Response: ...
```

El contrato que deben cumplir los middleware de módulos. `process_request`
puede retornar una `Response` para corto-circuitar el pipeline (útil para
auth de módulo, feature flags, etc.). `process_response` puede mutar la
respuesta antes de devolverla.

### `ModuleMiddlewareManager`

```python
class ModuleMiddlewareManager(BaseHTTPMiddleware):
    def __init__(self, app: Any, registry: Any | None = None) -> None:
        self.registry = registry
        self._cached_middleware: list[ModuleMiddlewareProtocol] | None = None
        self._cache_version: int = -1
```

Atributos clave:

- `registry`: referencia al registry del ModuleRuntime. Se inyecta
  **después** del arranque, por eso el default es `None`. Hasta que se
  asigna, es un passthrough transparente.
- `_cached_middleware`: lista cacheada de instancias de middleware activas.
- `_cache_version`: número de versión del registry en el último rebuild
  del cache.

### `_get_middleware_list()`

```python
def _get_middleware_list(self) -> list[ModuleMiddlewareProtocol]:
```

Compara `registry.version` con `self._cache_version`. Si coinciden,
devuelve el cache sin I/O. Si difieren (porque un módulo se activó o
desactivó), llama a `registry.get_all_middleware()` y reconstruye la
lista, filtrando cualquier objeto que no satisfaga `ModuleMiddlewareProtocol`.

### `dispatch` — el orden de ejecución

```python
# Fase request (en orden)
for mw in middleware_list:
    result = await mw.process_request(request)
    if result is not None:
        return result  # corto-circuito

response = await call_next(request)

# Fase response (en orden inverso)
for mw in reversed(middleware_list):
    response = await mw.process_response(request, response)
```

El orden de request es el mismo que el orden de registro. La respuesta se
procesa en orden inverso — patrón onion standard.

### `invalidate_cache()`

```python
def invalidate_cache(self) -> None:
```

Fuerza la reconstrucción de la lista en la siguiente petición. Se llama
desde `ModuleRuntime` cuando activa o desactiva un módulo.

---

## `observability.py` — `RequestObservabilityMiddleware`

[`middleware/observability.py`](../src/hotframe/middleware/observability.py)

**Qué intercepta**: toda petición HTTP.

**Qué hace**: vincula el `request_id` (generado por
`asgi-correlation-id.CorrelationIdMiddleware`) junto con `hub_id` y
`user_id` al contexto de observabilidad de la petición, y registra un
histograma de duración con atributos HTTP.

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

El `bind_context` es un context manager de `hotframe.utils.observability_context`
que configura estructured logging y el span de OpenTelemetry para la
duración de la petición.

### `bind_user_context(user_id, hub_id="")`

```python
def bind_user_context(user_id: str, hub_id: str = "") -> None:
```

Función auxiliar, no un middleware. Se llama desde la capa de autenticación
**después** de identificar al usuario, para enriquecer el contexto de
observabilidad con la identidad real. El middleware solo puede leer
`request.state.user_id` si la auth ya lo puso ahí, pero en caso de rutas
públicas o auth asíncrona, `bind_user_context` permite actualizar el
contexto a mitad de petición.

---

## `proxy_fix.py` — `ProxyFixMiddleware`

[`middleware/proxy_fix.py`](../src/hotframe/middleware/proxy_fix.py)

**Qué intercepta**: peticiones HTTP y WebSocket.

**Qué hace**: corrige el `host` y `scheme` del scope ASGI cuando el
servidor está detrás de un load balancer o reverse proxy.

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

A diferencia de los demás, **no hereda de `BaseHTTPMiddleware`** — es una
clase ASGI pura que implementa `__call__(scope, receive, send)`. Esto le
da control total sobre las cabeceras sin la penalización de buffering de
`BaseHTTPMiddleware`.

### Lógica de corrección

Dos ramas:

1. **ECS (AWS Elastic Container Service)**: si la cabecera `Host` del
   scope termina en `self._ecs_suffix` (e.g. `.ecs.eu-west-1.on.aws`),
   reemplaza `scope["server"]` y la cabecera `host` por el host público
   construido de `"{slug}.{domain_base}"`. También fuerza `scheme="https"`.

2. **`X-Forwarded-Host` estándar**: si la cabecera `x-forwarded-host`
   está presente, la usa como host. Si hay `X-Forwarded-Proto`, corrige
   el scheme.

El middleware bufferiza la respuesta completa (`body_chunks`) para poder
recalcular y emitir el `Content-Length` correcto tras el rewrite.

---

## `rate_limit.py` — `APIRateLimitMiddleware`

[`middleware/rate_limit.py`](../src/hotframe/middleware/rate_limit.py)

**Qué intercepta**: peticiones a `/api/`, `/m/` y los prefijos de auth
configurados.

**Qué hace**: controla el número de peticiones por IP usando una ventana
deslizante en memoria.

### `_SlidingWindow`

```python
class _SlidingWindow:
    __slots__ = ("_requests",)

    def is_allowed(self, key: str, limit: int, window: int) -> tuple[bool, int]:
    def cleanup(self, max_age: float = 300.0) -> None:
```

Almacena timestamps de peticiones por clave (`"bucket:ip"`). Para cada
petición:

1. Filtra los timestamps que caen fuera de la ventana.
2. Si quedan `>= limit`, rechaza.
3. Si no, añade el timestamp actual y devuelve `(True, remaining)`.

El `cleanup()` elimina claves inactivas (sin peticiones en los últimos
5 minutos). Se llama automáticamente cada 60 segundos del reloj monotónico
desde `dispatch`.

La instancia `_window` y `_last_cleanup` son **módulo-level singletons**
compartidos entre todas las peticiones del proceso.

### Los tres buckets

```python
def _get_rate_config(self, path: str) -> tuple[str, int] | None:
    if path.startswith("/api/"):
        return "api", self._api_rate      # default: 120 req/60s
    if path.startswith("/m/"):
        return "view", self._view_rate    # default: 300 req/60s
    if any(path.startswith(p) for p in self._auth_prefixes):
        return "auth", self._auth_rate    # default: 60 req/60s
    return None                           # sin rate limit
```

Rutas que no encajan en ningún bucket pasan sin restricción.

### Cabeceras de respuesta

Cuando la petición pasa, la respuesta lleva:

```
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 87
```

Cuando se rechaza (429):

```
Retry-After: 60
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 0
```

**Nota sobre DEBUG**: en modo debug, `_get_middleware_kwargs` en `stack.py`
sobreescribe `auth_rate` a 10 000, desactivando efectivamente el límite
de auth en desarrollo.

---

## `session_safe.py` — `RobustSessionMiddleware`

[`middleware/session_safe.py`](../src/hotframe/middleware/session_safe.py)

**Qué intercepta**: toda petición HTTP y WebSocket.

**Qué hace**: un drop-in replacement de `starlette.middleware.sessions.SessionMiddleware`
que no explota cuando la cookie de sesión está malformada.

### El problema

`SessionMiddleware` de Starlette decodifica la cookie con `base64.b64decode`
+ `json.loads` sin capturar `UnicodeDecodeError` ni `binascii.Error`. Si el
usuario tiene una cookie de una versión anterior del servidor (e.g. con
compresión zlib o un formato custom), la decodificación falla con 500.

### La solución

```python
class RobustSessionMiddleware(SessionMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
            return
        except (UnicodeDecodeError, ValueError, OSError) as exc:
            # Log y continuar con sesión vacía
            ...

        scope = _scope_without_cookie(scope, cookie_name)

        async def send_with_clear(message: Message) -> None:
            # Añade Set-Cookie con Max-Age=0 para borrar la cookie mala
            ...

        await super().__call__(scope, receive, send_with_clear)
```

Cuando falla la decodificación:

1. Registra el error en el logger `hotframe.middleware.session`.
2. Reconstruye el scope sin la cookie mala usando `_scope_without_cookie`.
3. Envuelve `send` en `send_with_clear` que añade `Set-Cookie: <name>=; Max-Age=0`
   para que el browser borre la cookie inmediatamente.
4. Vuelve a invocar el middleware padre — ahora con scope limpio, la sesión
   será vacía y la petición prosigue normalmente.

### `_scope_without_cookie(scope, cookie_name)`

```python
def _scope_without_cookie(scope: Scope, cookie_name: str) -> Scope:
```

Parsea la cabecera `Cookie` (formato `k1=v1; k2=v2`) y filtra la entrada
que coincide con `cookie_name`. Si la cookie queda vacía, elimina la
cabecera `Cookie` completamente.

---

## `timeout.py` — `TimeoutMiddleware`

[`middleware/timeout.py`](../src/hotframe/middleware/timeout.py)

**Qué intercepta**: toda petición HTTP (excepto `/health` y `/health/`).

**Qué hace**: cancela la petición si supera el timeout con `asyncio.timeout`.

```python
class TimeoutMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout  # default: 30 segundos

    async def dispatch(self, request: Request, call_next: ...) -> Response:
        if request.url.path in ("/health/", "/health"):
            return await call_next(request)
        try:
            async with asyncio.timeout(self.timeout):
                return await call_next(request)
        except TimeoutError:
            return JSONResponse({"detail": "Request timeout"}, status_code=504)
```

Devuelve 504 (Gateway Timeout). Se excluyen los health checks porque las
herramientas de monitorización (ALB, k8s liveness probes) deben recibir
siempre respuesta.

Debe ser el **middleware más exterior** de la pila — así el timeout cubre
el tiempo total incluido el procesamiento de todos los demás middleware.

---

## El orden de la pila de middleware

Este es el orden **real de ejecución** (de exterior a interior, de izquierda
a derecha en el flujo de una petición HTTP entrante):

```
Petición →
  1. TimeoutMiddleware           — cancela si tarda > 30 s
  2. ProxyFixMiddleware          — corrige host/scheme si hay proxy
  3. CorrelationIdMiddleware     — genera / propaga X-Request-ID
  4. ErrorPageMiddleware         — captura excepciones, renderiza páginas de error
  5. RobustSessionMiddleware     — decodifica cookie de sesión (o limpia si es mala)
  6. CSPMiddleware               — genera nonce CSP, lo guarda en request.state.csp_nonce
  7. CSRFMiddleware              — valida token CSRF en mutaciones (apps/auth/csrf.py)
  8. RequestObservabilityMiddleware — vincula contexto de observabilidad + histograma
  9. LanguageMiddleware          — detecta y activa idioma, guarda en request.state.language
 10. APIRateLimitMiddleware      — rate limiting por IP
 11. BodyLimitMiddleware         — rechaza peticiones demasiado grandes
 12. ModuleMiddlewareManager     — delega en middleware de módulos activos
       → vista / handler
```

La respuesta recorre la cadena en sentido inverso (12 → 1).

El orden importa especialmente para:

- `RobustSessionMiddleware` debe ir antes de `CSRFMiddleware` porque CSRF
  lee la sesión para validar el token.
- `TimeoutMiddleware` debe ser el más exterior para abarcar toda la cadena.
- `RequestObservabilityMiddleware` va después de `RobustSessionMiddleware`
  para poder leer `request.state.hub_id` si la autenticación lo pone ahí.
- `LanguageMiddleware` debe ir después de `RobustSessionMiddleware` porque
  lee el idioma de la sesión.

---

## Cómo encaja con el resto del framework

### Bootstrap (`create_app`)

`create_app` en `hotframe/__init__.py` llama a `build_middleware_stack(app, settings)`
como paso 2 del arranque (ver sección 4 de GUIDE.md). Esto registra todos los
middleware antes de que llegue la primera petición.

### ModuleRuntime (módulos dinámicos)

Cuando el `ModuleRuntime` activa un módulo con middleware propio:

1. El módulo registra sus instancias en el registry.
2. `ModuleMiddlewareManager` detecta el cambio de `registry.version` en la
   siguiente petición y recarga la lista del cache.

Si el módulo necesita añadir middleware ASGI completo (no solo
`ModuleMiddlewareProtocol`), el `ModuleRuntime` puede usar el
`MiddlewareStackManager` para un `add_and_rebuild` atómico.

### Plantillas Jinja2

- `request.state.csp_nonce` lo pone `CSPMiddleware` y lo consumen los
  contextos globales del motor Jinja2 registrados en `create_app`.
- `request.state.language` lo pone `LanguageMiddleware` y lo lee el
  adaptador `_RequestTranslations` para traducir strings en templates.

### Settings

Todos los parámetros de middleware se controlan desde `settings.py`:

```python
MIDDLEWARE: list[str]             # orden y lista de clases
CSP_ENFORCE: bool                 # CSPMiddleware
RATE_LIMIT_API: int               # APIRateLimitMiddleware
RATE_LIMIT_AUTH: int
RATE_LIMIT_AUTH_PREFIXES: list[str]
MAX_REQUEST_BODY: int             # BodyLimitMiddleware
SESSION_COOKIE_NAME: str          # RobustSessionMiddleware
SESSION_MAX_AGE: int
SECRET_KEY: str
DEPLOYMENT_MODE: str              # "local" vs producción (https_only)
```

---

## Gotchas y decisiones de diseño

### 1. Ventana deslizante en memoria, no Redis

`_SlidingWindow` es un singleton en el proceso. En un despliegue multi-instancia
cada instancia tiene su propio contador, lo que efectivamente multiplica el límite
por el número de instancias. Esto es aceptable para la mayoría de apps, pero si
necesitas límites estrictos en producción con múltiples workers, deberás implementar
un backend Redis.

### 2. `BaseHTTPMiddleware` vs middleware ASGI puro

La mayoría de middleware hereda de `BaseHTTPMiddleware` por simplicidad.
`ProxyFixMiddleware` es la excepción — es ASGI puro porque necesita bufferizar
la respuesta completa para recalcular `Content-Length`, algo que `BaseHTTPMiddleware`
no facilita. La penalización de `BaseHTTPMiddleware` (bufferiza el body en memoria)
es aceptable para los demás porque sus operaciones son en cabeceras.

### 3. El manager invalida, no reemplaza

`MiddlewareStackManager` no construye una pila alternativa y hace un swap
atómico. Invalida el caché de Starlette y deja que `build_middleware_stack()`
la reconstruya. La "atomicidad" es best-effort bajo CPython (no hay garantía
en otras implementaciones).

### 4. `RobustSessionMiddleware` absorbe solo errores de decodificación

La clase no absorbe `Exception` genérico — solo `UnicodeDecodeError`,
`ValueError` y `OSError` (que cubre `binascii.Error`). Errores legítimos
de la sesión (e.g. un handler que corrompe el state) siguen propagándose.

### 5. `ModuleMiddlewareManager.registry` se inyecta post-construcción

En el bootstrap, el manager se construye con `registry=None`. El
`ModuleRuntime`, que se crea después, asigna su registry al manager.
Hasta ese momento, el manager es un passthrough. Este patrón evita una
dependencia circular entre el middleware y el runtime de módulos.

### 6. La exclusión de `/static/` en `LanguageMiddleware` es deliberada

Las peticiones a assets estáticos no deben llevar `Set-Cookie` porque
rompería el caché de CDN. Los archivos estáticos son idénticos para todos
los idiomas; el idioma solo afecta a la UI dinámica.
