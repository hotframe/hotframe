# 20. Observabilidad (utils/)

> Propósito: instrumentar el framework con trazas distribuidas, métricas de aplicación y logging estructurado, y proporcionar un mecanismo de contexto de petición que propaga identificadores (request_id, hub_id, user_id) a través de cadenas async sin paso explícito de parámetros.

---

## Para qué sirve esta carpeta

`utils/` contiene las cuatro piezas de observabilidad de hotframe. Son ortogonales al código de dominio: están presentes en cada petición, evento y operación de módulo, pero el código de aplicación no necesita conocerlas para funcionar.

Las cuatro piezas:

1. **Context** (`observability_context.py`): un `ContextVar` que transporta identificadores de petición a través de corrutinas async sin pasar argumentos manualmente.
2. **Logging** (`observability_logging.py`): logging estructurado con `structlog`, que inyecta automáticamente el contexto de la petición en cada línea de log.
3. **Metrics** (`observability_metrics.py`): instrumentos OpenTelemetry (histogramas, contadores, gauges) para medir latencia, carga de módulos, eventos, hooks, errores y tareas en background.
4. **Telemetry** (`observability_telemetry.py`): configuración del proveedor OpenTelemetry, auto-instrumentación de FastAPI/SQLAlchemy/httpx, exportadores OTLP/console, y helpers para crear spans.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`utils/__init__.py`](../src/hotframe/utils/__init__.py) | Documentación del paquete |
| [`utils/observability_context.py`](../src/hotframe/utils/observability_context.py) | `RequestContext`, `request_context`, `bind_context`, `update_context` |
| [`utils/observability_logging.py`](../src/hotframe/utils/observability_logging.py) | `setup_logging`, `get_logger` |
| [`utils/observability_metrics.py`](../src/hotframe/utils/observability_metrics.py) | Instrumentos OTel: histogramas, contadores, gauges para request, módulos, eventos, hooks, errores, tareas |
| [`utils/observability_telemetry.py`](../src/hotframe/utils/observability_telemetry.py) | `setup_telemetry`, auto-instrumentación, `start_span`, `create_event_span`, `create_hook_span`, `create_module_span` |

---

## observability_context.py — Contexto de petición

### El problema

En un servidor async con muchas peticiones concurrentes, ¿cómo sabes a qué petición pertenece un log emitido desde una función de utilidad profunda sin necesidad de pasar `request_id` como argumento por toda la cadena de llamadas?

Python 3.7+ resuelve esto con `contextvars.ContextVar`: una variable "pegada" a la tarea asyncio (o hilo) actual, invisible para otras tareas concurrentes.

### `RequestContext`

```python
@dataclass(slots=True)
class RequestContext:
    request_id: str = ""
    hub_id: str = ""
    user_id: str = ""
    module_id: str = ""
    trace_id: str = ""

    def bind_dict(self) -> dict[str, str]:
        """Retorna solo los campos no vacíos, para bindear en structlog."""
```

Un dataclass inmutable-ish (slots para eficiencia) con los cinco identificadores de contexto. `bind_dict()` filtra campos vacíos para no contaminar los logs con claves vacías.

### `request_context: ContextVar[RequestContext]`

```python
request_context: ContextVar[RequestContext] = ContextVar(
    "request_context",
    default=RequestContext(),
)
```

La variable global de contexto. El default es un `RequestContext` vacío, así que `request_context.get()` nunca lanza `LookupError`.

### `bind_context(**kwargs) -> Generator[RequestContext, None, None]`

```python
@contextmanager
def bind_context(**kwargs: str) -> Generator[RequestContext, None, None]:
```

Context manager que establece el contexto para un bloque de código y lo restaura al salir (usando `ContextVar.reset(token)`). Es el uso recomendado desde middleware:

```python
# En el middleware de cada petición:
with bind_context(request_id=str(uuid4()), hub_id=str(hub_id), user_id=str(user.id)):
    response = await call_next(request)
```

Dentro del bloque —y en cualquier función async llamada desde él— `request_context.get()` retorna el contexto enlazado. Al salir del bloque, se restaura el contexto anterior (soporte para anidamiento).

### `update_context(**kwargs) -> None`

```python
def update_context(**kwargs: str) -> None:
```

Actualiza campos del contexto actual sin usar un context manager. Útil cuando aprendes información mid-request (p.ej. el `user_id` después de resolver la sesión):

```python
# Después de resolver el usuario:
update_context(user_id=str(user.id))
```

No restaura automáticamente el valor anterior. Para uso en flujos lineales donde no necesitas anidar contextos.

---

## observability_logging.py — Logging estructurado

### Filosofía

`setup_logging` configura **structlog** como motor de logging, con dos modos:

- **Desarrollo** (`json_output=False`): salida coloreada y legible por humanos en consola.
- **Producción** (`json_output=True`): JSON lines a stderr, una línea por evento, apto para ingestión por Loki, CloudWatch, Datadog, etc.

### `setup_logging(*, log_level="INFO", json_output=False) -> None`

```python
def setup_logging(*, log_level: str = "INFO", json_output: bool = False) -> None:
```

Llamada una vez desde `create_app`. Hace cuatro cosas:

1. **Configura los processors de structlog**. La cadena de processors es la misma para structlog y para stdlib:
   - `structlog.contextvars.merge_contextvars` — fusiona variables de contexto de structlog.
   - `_add_request_context` — inyecta `request_id`, `hub_id`, etc. desde `request_context`.
   - `structlog.stdlib.add_log_level` — añade el nivel (`info`, `error`, etc.).
   - `structlog.stdlib.add_logger_name` — añade el nombre del logger.
   - `structlog.processors.TimeStamper(fmt="iso")` — timestamp ISO-8601.
   - `structlog.processors.StackInfoRenderer` — renderiza stack traces.
   - `structlog.processors.UnicodeDecoder` — decodifica bytes a str.
   - `structlog.processors.CallsiteParameterAdder` — añade filename, lineno, func_name.
   - Renderer final: `JSONRenderer` (producción) o `ConsoleRenderer` (desarrollo).

2. **Configura el root logger de stdlib**. Todos los `logging.getLogger(...)` del ecosistema Python (SQLAlchemy, uvicorn, FastAPI, etc.) pasan por el `ProcessorFormatter` de structlog, que les aplica la misma cadena de processors.

3. **Silencia loggers ruidosos**. `uvicorn.access`, `watchfiles`, `httpcore`, `httpx`, `hpack` se elevan a `WARNING` para no inundar los logs en desarrollo.

4. **Configura uvicorn**. `uvicorn.error` propaga al root (structlog); `uvicorn.root` limpia sus handlers para evitar duplicados.

### `_add_request_context` (processor interno)

```python
def _add_request_context(logger, method_name, event_dict):
    ctx = request_context.get()
    if ctx.request_id: event_dict.setdefault("request_id", ctx.request_id)
    if ctx.hub_id:     event_dict.setdefault("hub_id", ctx.hub_id)
    # ...
    return event_dict
```

Processor de structlog que inyecta automáticamente los campos del `RequestContext` en cada evento de log. Usa `setdefault` para no sobrescribir si el código ya lo incluyó explícitamente.

### `get_logger(name=None) -> structlog.stdlib.BoundLogger`

```python
def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
```

Drop-in para `logging.getLogger()`. Retorna un `BoundLogger` de structlog que incluye contexto de petición automáticamente:

```python
from hotframe.utils.observability_logging import get_logger

logger = get_logger(__name__)

async def process_order(order_id: str):
    logger.info("processing order", order_id=order_id)
    # → {"event": "processing order", "order_id": "...", "request_id": "...", "hub_id": "...", ...}
```

---

## observability_metrics.py — Instrumentos OpenTelemetry

Este módulo define todos los instrumentos de métricas de hotframe. Los instrumentos son **lazy-initialized**: se crean la primera vez que se usan, no al importar el módulo. Si el SDK de OpenTelemetry no está configurado, las funciones retornan instrumentos no-op con overhead cero.

### El Meter singleton

```python
_meter: metrics.Meter | None = None

def _get_meter() -> metrics.Meter:
    global _meter
    if _meter is None:
        _meter = metrics.get_meter("hotframe", version="0.1.0")
    return _meter
```

Un único `Meter` con nombre `"hotframe"` y versión `"0.1.0"`. Todos los instrumentos se crean desde él.

### Categorías de instrumentos

#### Métricas de petición HTTP

```python
def get_request_duration_histogram() -> Histogram:
    # name="http.server.request.duration", unit="ms"
```

Histograma de latencia de peticiones HTTP. Las etiquetas típicas son `endpoint`, `method`, `status_code` — el middleware que usa este instrumento las añade al registrar.

#### Métricas de módulos

```python
def get_module_load_duration_histogram() -> Histogram:
    # name="hotframe.module.load.duration", unit="ms"

def get_active_modules_counter() -> UpDownCounter:
    # name="hotframe.modules.active"
```

`UpDownCounter` es el tipo OTel para valores que pueden subir y bajar (como un gauge). Se incrementa al activar un módulo y decrementa al desactivarlo.

#### Métricas de eventos

```python
def get_event_emit_counter() -> Counter:
    # name="hotframe.events.emitted"

def get_event_handler_duration_histogram() -> Histogram:
    # name="hotframe.events.handler.duration", unit="ms"
```

Permiten responder: "¿cuántas veces se emitió `invoice.paid` hoy?" y "¿cuánto tarda en media el handler de `user.created`?".

#### Métricas de hooks

```python
def get_hook_duration_histogram() -> Histogram:
    # name="hotframe.hooks.duration", unit="ms"

def get_hook_callback_counter() -> Counter:
    # name="hotframe.hooks.callbacks.invoked"
```

#### Métricas de errores

```python
def get_error_counter() -> Counter:
    # name="hotframe.errors"
```

Etiquetable por `module` y `error_type`.

#### Métricas de tareas en background

```python
def get_tasks_pending_counter()   -> UpDownCounter  # hotframe.tasks.pending
def get_tasks_running_counter()   -> UpDownCounter  # hotframe.tasks.running
def get_tasks_completed_counter() -> Counter        # hotframe.tasks.completed
def get_tasks_failed_counter()    -> Counter        # hotframe.tasks.failed
```

Cuatro instrumentos para el ciclo de vida completo de tareas asíncronas.

### `reset_metrics() -> None`

```python
def reset_metrics() -> None:
```

Resetea todos los instrumentos a `None` y limpia `_meter`. Solo para tests: permite que cada test empiece con instrumentos frescos.

---

## observability_telemetry.py — Configuración OpenTelemetry

### `setup_telemetry(*, service_name, debug, hub_id) -> None`

```python
def setup_telemetry(
    *,
    service_name: str = "hub",
    debug: bool = False,
    hub_id: str = "",
) -> None:
```

El punto de entrada único para configurar toda la observabilidad OTel. `create_app` la llama en el paso 1 del bootstrap.

Lógica de selección de exportador de trazas:

| Condición | Exportador |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` definida | OTLP gRPC (`opentelemetry-exporter-otlp-proto-grpc`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` vacía + `debug=True` | Console (stdout) |
| Ninguna | No-op (sin exportación, overhead mínimo) |

Si el paquete OTLP no está instalado, `setup_telemetry` lo detecta via `ImportError` y emite un `WARNING` en lugar de fallar.

Atributos del Resource:

```python
resource_attrs = {
    ResourceAttributes.SERVICE_NAME: otel_service,
    ResourceAttributes.SERVICE_VERSION: "0.1.0",
}
if hub_id:
    resource_attrs["hub.id"] = hub_id
```

El `hub_id` se adjunta como atributo de recurso a todos los spans — útil en instalaciones multi-tenant donde varias instancias exportan al mismo colector.

### `_setup_metrics_provider(otlp_endpoint, resource, debug) -> None`

Configura el `MeterProvider`. Misma lógica de selección de exportador: OTLP si hay endpoint, Console si debug, nada en otro caso. El lector OTLP exporta cada 30 segundos; el Console cada 60 (menos ruidoso en dev).

### Auto-instrumentación

```python
def _auto_instrument_fastapi() -> None:
    FastAPIInstrumentor().instrument()

def _auto_instrument_sqlalchemy() -> None:
    SQLAlchemyInstrumentor().instrument()

def _auto_instrument_httpx() -> None:
    HTTPXClientInstrumentor().instrument()
```

Tres funciones que instrumentan automáticamente los frameworks de terceros. Cada una captura `ImportError` y loguea en `DEBUG` si el paquete de instrumentación no está instalado — sin crashear. Esto permite usar hotframe sin ninguna dependencia OTel y añadirlas progresivamente.

### Helpers de spans

#### `start_span(name, *, attributes, kind) -> AbstractContextManager[Span]`

```python
def start_span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
) -> AbstractContextManager[Span]:
```

Wrapper sobre `tracer.start_as_current_span`. Retorna un context manager:

```python
with start_span("db.query", attributes={"db.table": "products"}) as span:
    result = await db.execute(query)
    span.set_attribute("db.rows_returned", len(result))
```

#### `create_event_span(event_name) -> AbstractContextManager[Span]`

```python
def create_event_span(event_name: str) -> AbstractContextManager[Span]:
    return start_span(
        f"event.emit:{event_name}",
        attributes={"event.name": event_name, "event.system": "event_bus"},
    )
```

El `AsyncEventBus` llama a este helper al emitir eventos, conectando automáticamente la emisión de eventos con las trazas distribuidas.

#### `create_hook_span(hook_name, hook_type="action") -> AbstractContextManager[Span]`

```python
def create_hook_span(hook_name: str, hook_type: str = "action"):
    return start_span(
        f"hook.{hook_type}:{hook_name}",
        attributes={"hook.name": hook_name, "hook.type": hook_type},
    )
```

El `HookRegistry` lo usa para cada acción/filtro ejecutado.

#### `create_module_span(operation, module_id) -> AbstractContextManager[Span]`

```python
def create_module_span(operation: str, module_id: str):
    return start_span(
        f"module.{operation}:{module_id}",
        attributes={"module.id": module_id, "module.operation": operation},
    )
```

El `ModuleRuntime` lo llama en `install`, `activate`, `deactivate`, `update`. Así puedes ver en tu backend OTel cuánto tarda cada operación de módulo.

#### `get_tracer() -> Tracer`

```python
def get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer("hub", "0.1.0")
    return _tracer
```

Retorna el tracer global del framework. Si `setup_telemetry` no se llamó, retorna un no-op tracer.

---

## Cómo encaja con el resto del framework

- **`create_app`**: llama a `setup_logging` y `setup_telemetry` en los primeros pasos del bootstrap, antes de montar middleware ni registrar rutas.
- **`AsyncEventBus`**: usa `create_event_span` y `get_event_emit_counter` en cada `emit`.
- **`HookRegistry`**: usa `create_hook_span` y `get_hook_duration_histogram`.
- **`ModuleRuntime`**: usa `create_module_span`, `get_module_load_duration_histogram` y `get_active_modules_counter`.
- **Middleware de peticiones**: usa `bind_context` para establecer `request_id`, `hub_id`, `user_id` en el contexto, y `get_request_duration_histogram` para registrar latencia.
- **`get_logger`**: es el reemplazo directo de `logging.getLogger`. Todo el código de hotframe (y el de aplicación) puede usarlo y obtendrá logs enriquecidos automáticamente.

---

## Gotchas y decisiones de diseño

**Los instrumentos son lazy singletons con variables globales de módulo.** Cada instrumento (`_request_duration`, `_meter`, etc.) es una variable global a nivel de módulo, inicializada en la primera llamada. Esto es un patrón común en OTel Python. La contrapartida: en tests que importan el módulo antes de configurar el SDK, los instrumentos son no-op y no se puede verificar que se llamaron. `reset_metrics()` permite resetear entre tests.

**`setup_telemetry` es idempotente en la práctica pero no en su API.** Si la llamas dos veces, crea dos `TracerProvider` y llama a `trace.set_tracer_provider` dos veces. El segundo sobreescribe al primero. No es un problema en producción (se llama una vez), pero en tests que inicializan la app múltiples veces hay que tener cuidado.

**`bind_context` no es thread-safe en el sentido tradicional.** `ContextVar` aísla por tarea asyncio, no por hilo. Si usas `asyncio.create_task` o `asyncio.gather`, los sub-tasks heredan el contexto del padre en el momento de su creación. Si el contexto se modifica después (con `update_context`), los sub-tasks no ven el cambio. Usa `bind_context` en el task padre antes de crear sub-tasks si necesitas que hereden el contexto completo.

**JSON en producción, colores en desarrollo.** La decisión de `json_output` la toma `create_app` basándose en `settings.LOG_FORMAT` o el modo de despliegue. No necesitas configurarlo manualmente.

**Los loggers ruidosos se silencian globalmente.** `uvicorn.access`, `watchfiles`, etc. se elevan a `WARNING` en `setup_logging`. Si necesitas su output en `DEBUG`, tendrás que elevarlos de vuelta manualmente después de llamar a `setup_logging`. No hay una opción de settings para esto en v1.0.

**OTel zero-dependency en runtime.** Todos los imports de paquetes opcionales (`opentelemetry-instrumentation-fastapi`, etc.) están dentro de `try/except ImportError`. Puedes usar hotframe sin instalar nada de OTel y la app funciona, simplemente sin trazas ni métricas.
