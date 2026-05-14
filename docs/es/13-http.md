# 13. Cliente HTTP e interceptores (`http/`)

> El subsistema `http/` dota a hotframe de **clientes HTTP salientes autenticados y observables**, con una cadena de interceptores al estilo de Angular que permite añadir reintentos, circuit breakers y refresco de credenciales de forma composable y sin tocar el código de negocio.

---

## Para qué sirve esta carpeta

Esta carpeta **no gestiona rutas entrantes** — eso es FastAPI. Su dominio es exclusivamente el tráfico HTTP que tu aplicación genera hacia terceros: APIs externas, webhooks, microservicios propios, etc.

El problema que resuelve es real y repetido: cada módulo necesita llamar a APIs externas con credenciales distintas, estrategias de retry distintas y observabilidad compartida. Sin un subsistema dedicado, cada módulo reinventa la rueda: autenticación a mano, reintentos con bugs, sin métricas.

La solución de hotframe tiene tres capas:

1. **Estrategias de autenticación** (`auth.py`) — objetos que saben cómo firmar una petición.
2. **Interceptores** (`interceptors.py`, `builtin_interceptors.py`) — middlewares que envuelven el envío y pueden reintentar, cortocircuitar o refrescar tokens.
3. **`AuthenticatedClient`** (`client.py`) — el cliente que orquesta todo, construido sobre `httpx.AsyncClient`.
4. **`HttpClientRegistry`** (`registry.py`) — el catálogo central donde módulos y apps registran sus clientes con nombre.
5. **`discover_interceptors`** (`loader.py`) — descubrimiento automático de interceptores desde el sistema de ficheros.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`__init__.py`](../src/hotframe/http/__init__.py) | Superficie pública del subsistema; re-exporta todo lo necesario |
| [`auth.py`](../src/hotframe/http/auth.py) | Estrategias de autenticación: `Auth`, `BearerAuth`, `ApiKeyAuth`, `QueryApiKeyAuth`, `BasicAuth`, `HmacAuth`, `CustomAuth`, `NoAuth` |
| [`interceptors.py`](../src/hotframe/http/interceptors.py) | Protocolo `Interceptor`, clase base `InterceptorBase`, función `build_chain`, alias `CallNext` |
| [`builtin_interceptors.py`](../src/hotframe/http/builtin_interceptors.py) | Interceptores incluidos: `RetryInterceptor`, `CircuitBreakerInterceptor`, `RefreshInterceptor`, helper `exponential_backoff` |
| [`client.py`](../src/hotframe/http/client.py) | `AuthenticatedClient` — cliente httpx con auth + interceptores + eventos |
| [`registry.py`](../src/hotframe/http/registry.py) | `HttpClientRegistry` — catálogo de clientes por nombre, con soporte de ownership por módulo |
| [`loader.py`](../src/hotframe/http/loader.py) | `discover_interceptors` — escanea directorios buscando instancias que satisfagan el protocolo |
| [`events.py`](../src/hotframe/http/events.py) | Constantes de nombres de eventos emitidos al `AsyncEventBus` |

---

## `auth.py` — Estrategias de autenticación

El diseño es minimalista: una clase base abstracta con un solo método async, y varias implementaciones concretas que mutan el `httpx.Request` en place. Todas son **stateless** respecto a la sesión: las credenciales se re-leen en cada petición.

### `CredentialSource`

```python
CredentialSource = str | Callable[[], str] | Callable[[], Awaitable[str]]
```

Tipo alias que aceptan todas las estrategias como fuente de credencial. La clave está en que puede ser un **callable sync o async**: cuando el token rota (OAuth, metadata de AWS, etc.), el callable devuelve siempre el valor actual sin necesidad de reiniciar el proceso ni recrear el cliente.

### `_resolve_source(source: CredentialSource) -> str`

Función interna que resuelve el `CredentialSource` a una cadena plana. Maneja los tres casos (string literal, callable sync, callable async) y lanza `TypeError` si el callable devuelve algo que no es string.

### `Auth` — clase base

```python
class Auth:
    async def apply(self, request: httpx.Request) -> None: ...
```

No es un Protocol formal: es una clase abstracta clásica que lanza `NotImplementedError`. Subclasear `Auth` es la forma canónica de crear estrategias propias.

### `BearerAuth`

```python
class BearerAuth(Auth):
    def __init__(self, source: CredentialSource) -> None: ...
    async def apply(self, request: httpx.Request) -> None: ...
```

Añade la cabecera `Authorization: Bearer <token>`. El token se resuelve en cada llamada a `apply`, por lo que funciona directamente con una función que devuelva el token actual del store de OAuth.

### `ApiKeyAuth`

```python
class ApiKeyAuth(Auth):
    def __init__(self, source: CredentialSource, header: str = "X-Api-Key") -> None: ...
```

Clave en una cabecera configurable. El parámetro `header` permite adaptarse a APIs que usen nombres no estándar (`Stripe-Key`, `X-Token`, etc.).

### `QueryApiKeyAuth`

```python
class QueryApiKeyAuth(Auth):
    def __init__(self, source: CredentialSource, param: str = "api_key") -> None: ...
```

Para APIs que exigen la clave en la query string. Usa `httpx.URL.copy_merge_params` para añadir el parámetro sin destruir los parámetros existentes ni dejar claves antiguas tras una rotación.

### `BasicAuth`

```python
class BasicAuth(Auth):
    def __init__(self, username: str, password: str) -> None: ...
```

HTTP Basic (RFC 7617). Recalcula el header Base64 en cada request aunque `username`/`password` no cambian — por coherencia con el resto del diseño.

### `HmacAuth`

```python
class HmacAuth(Auth):
    def __init__(self, key_id: str, secret: str, algorithm: str = "sha256") -> None: ...
```

Firma el cuerpo de la petición con HMAC. El header resultante tiene la forma:

```
Authorization: HMAC-SHA256 KeyId=<key_id>, Signature=<hexdigest>
```

Valida en `__init__` que el algoritmo esté disponible en `hashlib`. Para cuerpos streaming devuelve una firma sobre `b""` — limitación documentada que el comentario del código reconoce explícitamente.

### `CustomAuth`

```python
class CustomAuth(Auth):
    def __init__(self, apply: Callable[[httpx.Request], Awaitable[None]]) -> None: ...
```

Válvula de escape: delega la autenticación a un callable async arbitrario. Rechaza callables síncronos en el constructor (lanza `TypeError`) porque flujos como OAuth refresh o SigV4 con metadatos de AWS requieren `await`.

### `NoAuth`

```python
class NoAuth(Auth):
    async def apply(self, request: httpx.Request) -> None:
        return None
```

No-op explícito. Es el valor por defecto de `AuthenticatedClient` cuando no se pasa `auth=`. Hace el intent legible: "este cliente intencionalmente no autentica".

---

## `interceptors.py` — Primitivas de la cadena

### `CallNext`

```python
CallNext = Callable[[httpx.Request], Awaitable[httpx.Response]]
```

Alias de tipo para "la función que llama al siguiente eslabón de la cadena". Cada interceptor recibe uno como segundo argumento en `intercept`. Esta abstracción desacopla completamente los interceptores del cliente concreto: pueden usarse en tests sin importar `AuthenticatedClient`.

### `Interceptor` — Protocol

```python
@runtime_checkable
class Interceptor(Protocol):
    name: str
    applies_to: str | list[str] | Callable[[str], bool]
    order: int

    async def intercept(self, request: httpx.Request, call_next: CallNext) -> httpx.Response: ...
```

El contrato que todo interceptor debe satisfacer. Es `@runtime_checkable`, lo que permite usarlo en `isinstance()` para el descubrimiento automático del loader.

Atributos:
- `name`: identificador único; usado para logs y deduplicación.
- `applies_to`: selector de clientes — wildcard `"*"`, nombre exacto, lista de nombres, o callable predicate.
- `order`: posición en la cadena; **valores bajos quedan en el exterior** (ejecutan primero en la bajada, últimos en la subida).

### `InterceptorBase`

```python
class InterceptorBase:
    name: str = ""
    applies_to: str | list[str] | Callable[[str], bool] = "*"
    order: int = 100

    def applies_to_client(self, client_name: str) -> bool: ...
    async def intercept(self, request: httpx.Request, call_next: CallNext) -> httpx.Response: ...
```

Clase base opcional que provee:
- Defaults razonables para los tres atributos.
- `applies_to_client(client_name)`: resuelve el matcher con la misma lógica en todos los casos (wildcard, string exacto, lista, callable) y **falla cerrado** (devuelve `False`) ante formatos desconocidos.
- `intercept` por defecto es un pass-through puro: `return await call_next(request)`.

Usar `InterceptorBase` no es obligatorio — cualquier objeto que satisfaga el `Protocol` funciona.

### `build_chain(interceptors, terminal) -> CallNext`

```python
def build_chain(
    interceptors: list[Interceptor],
    terminal: CallNext,
) -> CallNext: ...
```

La función central del sistema. Toma una lista de interceptores y una función `terminal` (la que realmente envía la petición) y devuelve el callable que representa la cabeza de la cadena completa.

Algoritmo:
1. Ordena los interceptores por `order` ascendente.
2. Construye la cadena **de adentro hacia afuera**: primero envuelve el `terminal` con el interceptor de mayor `order`, luego ese resultado con el siguiente, y así hasta el de menor `order`.
3. Devuelve el callable más externo.

El resultado es que `order=100` (exterior) ejecuta su cuerpo antes que `order=200` (interior) en el camino de bajada, y después en el de subida — exactamente como middleware ASGI.

```python
# Ejemplo conceptual: CircuitBreaker(100) → Refresh(150) → Retry(200) → terminal
chain = build_chain([retry, circuit_breaker, refresh], terminal)
response = await chain(request)
```

---

## `builtin_interceptors.py` — Los tres interceptores incluidos

### `exponential_backoff`

```python
def exponential_backoff(
    base: float = 0.5,
    cap: float = 8.0,
    jitter: bool = True,
) -> Callable[[int], float]: ...
```

Helper que devuelve una función `compute(attempt) -> seconds`. La fórmula es `min(cap, base * 2**attempt)`. Con `jitter=True` (por defecto) multiplica el resultado por un factor aleatorio en `[0.5, 1.0]` para evitar el problema de thundering herd cuando muchas instancias reintenten a la vez.

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

Reintenta la misma petición cuando la respuesta tiene un código en `on_status`. `max_attempts` es el total de intentos (incluyendo el primero). Si `backoff` es `None` no duerme entre intentos. El `order` por defecto es `200` — intencionalmente más alto que el `CircuitBreakerInterceptor` (100) para que el reintento ocurra **dentro** del circuit breaker: si el breaker está abierto, no malgasta el presupuesto de reintentos.

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

Implementa el patrón de circuit breaker en tres estados:

| Estado | Comportamiento |
|---|---|
| `closed` | Peticiones pasan; fallos incrementan contador |
| `open` | Peticiones rechazadas con `httpx.ConnectError` inmediatamente |
| `half_open` | Una petición pasa como sonda; éxito → cierra, fallo → reabre |

Un "fallo" es cualquier excepción del downstream O una respuesta con código en `failure_statuses` (por defecto todos los `5xx`).

La transición `open → half_open` ocurre tras `recovery_seconds`. La lógica crítica está protegida con `asyncio.Lock` para evitar race conditions en entornos con múltiples corrutinas concurrentes. La propiedad `state` es de solo lectura y útil para dashboards/health checks.

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

Cuando el downstream devuelve `on_status` (default `401`), llama al callback `refresh()` y reintenta la petición. `max_retries=1` es deliberado: si después del refresh sigue llegando `401`, algo está mal y no tiene sentido seguir — devuelve la respuesta original.

El `order=150` lo sitúa entre el circuit breaker (100) y el retry (200): si el retry se agota primero y el resultado es `401`, el refresh no tiene oportunidad. Si en cambio el refresh falla, el circuit breaker puede abrir.

El framework es agnóstico al mecanismo de refresco: OAuth, rotación de API key, SigV4 con metadatos de AWS — todo cabe en el callback async.

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

Envuelve `httpx.AsyncClient` y añade tres capas:

1. **Auth**: la estrategia se aplica en cada request.
2. **Interceptores**: la cadena envuelve el envío.
3. **Eventos observabilidad**: emite al `AsyncEventBus` si está configurado.

### Decisión de diseño: dos rutas de auth

Cuando **no hay interceptores**, la auth se conecta como un `httpx` event hook (`request` hook). Esto es eficiente y limpio.

Cuando **hay interceptores**, la auth se mueve al `terminal` de la cadena:

```python
async def terminal(req: httpx.Request) -> httpx.Response:
    await self._auth.apply(req)
    return await self._client.send(req)
```

Este cambio es crítico para el `RefreshInterceptor`: cuando el callback de refresh actualiza el token (por ejemplo en el `CredentialSource` callable), el reintento que ocurre dentro del interceptor vuelve a llamar a `terminal`, que re-lee el token fresco. Sin este diseño, el reintento enviaría el token viejo.

### Métodos principales

| Método | Firma simplificada | Descripción |
|---|---|---|
| `request` | `async def request(method, url, **kwargs) -> httpx.Response` | Despacha cualquier método; emite eventos de ciclo de vida |
| `get/post/put/patch/delete` | `async def <method>(url, **kwargs) -> httpx.Response` | Atajos que delegan a `request` |
| `stream` | `def stream(method, url, **kwargs)` | Context manager de streaming; **no** pasa por interceptores ni emite eventos lifecycle |
| `set_interceptors` | `def set_interceptors(interceptors)` | Reemplaza la cadena atómicamente; reconfigura la ruta de auth |
| `aclose` | `async def aclose()` | Cierra el `httpx.AsyncClient` subyacente |

### Eventos de observabilidad

Los eventos se emiten **una vez por llamada externa** a `request`, no por cada reintento interno:

| Evento | Cuándo | Payload extra |
|---|---|---|
| `http.request.started` | Antes del despacho | `method`, `url` |
| `http.request.completed` | Respuesta recibida | `method`, `url`, `status`, `duration_ms` |
| `http.request.failed` | Excepción antes de respuesta | `method`, `url`, `error`, `duration_ms` |

Los tres llevan `client_name` si el cliente tiene nombre. Los errores en la emisión de eventos nunca propagan — el logging continúa pero la petición HTTP sigue su curso.

### Propiedades

| Propiedad | Tipo | Descripción |
|---|---|---|
| `auth` | `Auth` | Estrategia activa |
| `name` | `str | None` | Nombre del cliente |
| `base_url` | `httpx.URL` | URL base del cliente httpx |
| `headers` | `httpx.Headers` | Cabeceras por defecto |
| `is_closed` | `bool` | Si el cliente httpx está cerrado |
| `interceptors` | `list[Interceptor]` | Copia superficial de la cadena |

---

## `registry.py` — `HttpClientRegistry`

```python
class HttpClientRegistry:
    def __init__(
        self,
        ambient_interceptors: list[Interceptor] | None = None,
    ) -> None: ...
```

Catálogo nombrado que vive en `app.state.http_clients`. Dos responsabilidades:

1. **Registro/lookup por nombre**: módulos y apps registran clientes; cualquier otro código los busca.
2. **Gestión de ciclo de vida**: cuando un módulo se desactiva, todos sus clientes se cierran y eliminan del catálogo.

### Interceptores ambientales

El registry mantiene un **pool de interceptores ambientales** (`ambient_interceptors`). Cuando se registra un cliente sin `interceptors=` explícitos, el registry aplica automáticamente los interceptores del pool cuyo `applies_to` coincida con el nombre del cliente. Esto permite configurar políticas globales (retry, circuit breaker) una sola vez en bootstrap.

```python
# En startup:
registry.set_ambient_interceptors([
    RetryInterceptor(on_status=[502, 503, 504]),
    CircuitBreakerInterceptor(threshold=10),
])
```

### Métodos de registro

```python
def register(
    self,
    name: str,
    client: AuthenticatedClient,
    owner_module_id: str | None = None,
    interceptors: list[Interceptor] | None = None,
) -> None: ...
```

Registra un cliente. Lanza `KeyError` si el nombre ya existe — usa `replace()` para sobreescribir. El `owner_module_id` es el id del módulo que registra el cliente; cuando ese módulo se desactiva, el registry lo limpia automáticamente.

```python
def replace(self, name, client, owner_module_id=None, interceptors=None) -> None: ...
```

Sobreescribe silenciosamente. El cliente anterior se cierra **en background** (via `asyncio.create_task`) para no bloquear el reemplazo.

### Lookup

```python
def get(self, name: str) -> AuthenticatedClient | None: ...
def __getitem__(self, name: str) -> AuthenticatedClient: ...  # KeyError si no existe
def __contains__(self, name: object) -> bool: ...
def list_registered(self) -> list[str]: ...
def owner_of(self, name: str) -> str | None: ...
```

### Ciclo de vida

```python
async def unregister(self, name: str) -> None: ...           # elimina uno
async def unregister_module(self, module_id: str) -> None:   # elimina todos los de un módulo
async def aclose_all(self) -> None:                          # shutdown: cierra todos
```

`unregister_module` es el método clave para el hot-unload de módulos: el `ModuleRuntime` lo llama automáticamente al desactivar un módulo, liberando las conexiones httpx sin que el módulo tenga que hacer cleanup manual.

---

## `loader.py` — `discover_interceptors`

```python
def discover_interceptors(
    search_paths: list[Path],
    logger: Logger | None = None,
    recursive: bool = False,
) -> list[Interceptor]: ...
```

Escanea directorios buscando **instancias** (no clases) que satisfagan el protocolo `Interceptor`. Es el mecanismo de "interceptores ambiente" descubiertos desde disco, equivalente al autodescubrimiento de apps o módulos.

Comportamiento:
- Ignora archivos que empiezan por `_` (incluido `__init__.py`).
- Un fichero que falla al importar se loggea como `WARNING` y se salta — un interceptor roto no bloquea el arranque.
- Nombres duplicados se deduplican (primer descubierto gana).
- El resultado viene ordenado por `order` ascendente.
- Paths inexistentes se loggean como `DEBUG` y se ignoran — proyectos sin interceptores arrancan limpios.

La detección usa `_looks_like_interceptor(obj)`:
1. No es clase ni módulo.
2. Tiene atributos `name`, `applies_to`, `order`, `intercept`.
3. `intercept` es corrutina (`inspect.iscoroutinefunction`).
4. `name` es string no vacío.

---

## `events.py` — Constantes de eventos

Tres constantes de string que nombran los eventos emitidos al `AsyncEventBus`:

```python
EVENT_REQUEST_STARTED   = "http.request.started"
EVENT_REQUEST_COMPLETED = "http.request.completed"
EVENT_REQUEST_FAILED    = "http.request.failed"
```

Usar las constantes en lugar de strings literales evita typos y permite que IDEs y grep encuentren todos los suscriptores.

---

## Cómo encaja con el resto del framework

- **Bootstrap (`create_app`)**: crea la `HttpClientRegistry` y la pone en `app.state.http_clients`. Llama a `discover_interceptors` sobre las rutas configuradas para poblar el pool ambient.
- **Módulos**: al activarse, un módulo puede hacer `app.state.http_clients.register("stripe", client, owner_module_id="billing")`. Al desactivarse, el `ModuleRuntime` llama a `registry.unregister_module("billing")` automáticamente.
- **`AsyncEventBus`** (`signals/`): `AuthenticatedClient` acepta opcionalmente el bus y emite los tres eventos de ciclo de vida. Otros módulos pueden suscribirse a `"http.request.*"` para métricas, logging centralizado o alertas.
- **`ISession` / repos** (`db/`): no hay dependencia directa entre `http/` y `db/`. Son subsistemas paralelos.
- **Settings**: el path de descubrimiento de interceptores se puede configurar en `settings.py` mediante `MODULE_SOURCE` o configuración custom.

---

## Gotchas y decisiones de diseño

**1. Interceptores son instancias, no clases**
El loader busca instancias pre-configuradas a nivel de módulo, no clases. Esto permite que el archivo de interceptores configure parámetros reales (`RetryInterceptor(on_status=[503])`) en lugar de dejar esa responsabilidad al framework.

**2. El streaming no pasa por interceptores**
`AuthenticatedClient.stream()` delega directamente a `httpx.AsyncClient.stream()`. Los interceptores no envuelven el streaming porque `httpx` gestiona la semántica de completitud del stream. Si necesitas interceptar streams, usa un `transport` custom de httpx.

**3. Los eventos lifecycle son externos, no por reintento**
Si `RetryInterceptor` hace 3 intentos, solo hay un `EVENT_REQUEST_STARTED` y un `EVENT_REQUEST_COMPLETED` (o `FAILED`). Diseño deliberado: los reintentos son un detalle de implementación; el observador externo ve una sola operación lógica.

**4. `replace()` cierra el cliente anterior en background**
Para no bloquear el reemplazo, el cliente viejo se cierra con `asyncio.create_task`. Si no hay loop corriendo (tests), usa `asyncio.run()` en un loop temporal. Esto evita connection leaks sin imponer `await` al llamador.

**5. El `CircuitBreakerInterceptor` no es distribuido**
El estado del circuit breaker (`_state`, `_failures`) vive en memoria del proceso. En multi-instancia (varias réplicas del servidor), cada proceso tiene su propio breaker independiente. Si necesitas un breaker compartido, implementa tu propio interceptor con Redis.

**6. `CustomAuth` rechaza sync callables**
A diferencia de `CredentialSource`, `CustomAuth` requiere explícitamente `async def`. El mensaje de error es instructivo: "wrap synchronous logic in an async function if needed."

---

## Ejemplos de uso extraídos del código

### Configurar un cliente con Bearer y retry

```python
from hotframe.http import (
    AuthenticatedClient,
    BearerAuth,
    RetryInterceptor,
    CircuitBreakerInterceptor,
    exponential_backoff,
)

client = AuthenticatedClient(
    base_url="https://api.ejemplo.com",
    auth=BearerAuth(source=lambda: token_store.current_token()),
    interceptors=[
        CircuitBreakerInterceptor(threshold=5, recovery_seconds=30),
        RetryInterceptor(
            on_status=[502, 503, 504],
            max_attempts=3,
            backoff=exponential_backoff(base=0.5, cap=8.0),
        ),
    ],
    name="ejemplo_api",
)
```

### Registrar en el registry con ownership de módulo

```python
# En el módulo billing, al activarse:
registry = app.state.http_clients
registry.register(
    name="stripe",
    client=AuthenticatedClient(
        base_url="https://api.stripe.com/v1",
        auth=BearerAuth(source=settings.STRIPE_SECRET_KEY),
    ),
    owner_module_id="billing",
)

# Al desactivarse el módulo, el ModuleRuntime hace:
await registry.unregister_module("billing")
# → El cliente "stripe" se cierra automáticamente
```

### Refresco de token OAuth

```python
from hotframe.http import RefreshInterceptor, BearerAuth

token_store = {"access_token": "..."}

async def refresh_oauth():
    # Llama al token endpoint y actualiza token_store
    new_token = await oauth_client.refresh()
    token_store["access_token"] = new_token

client = AuthenticatedClient(
    auth=BearerAuth(source=lambda: token_store["access_token"]),
    interceptors=[
        RefreshInterceptor(refresh=refresh_oauth, on_status=401),
    ],
)
```

### Interceptor personalizado

```python
from hotframe.http import InterceptorBase, CallNext
import httpx

class LoggingInterceptor(InterceptorBase):
    name = "my_logger"
    order = 50  # exterior a todo, ve todas las peticiones

    async def intercept(self, request: httpx.Request, call_next: CallNext) -> httpx.Response:
        print(f"→ {request.method} {request.url}")
        response = await call_next(request)
        print(f"← {response.status_code}")
        return response
```