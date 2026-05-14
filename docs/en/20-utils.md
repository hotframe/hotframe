# 20. Observability (`utils/`)

> Purpose: instrument the framework with distributed traces, application metrics, and structured logging, and provide a request-context mechanism that propagates identifiers (request_id, hub_id, user_id) through async call chains without explicit parameter passing.

---

## What this folder is for

`utils/` contains the four observability pieces of hotframe. They are orthogonal to domain code: present in every request, event, and module operation, yet application code does not need to know about them to function.

The four pieces:

1. **Context** (`observability_context.py`): a `ContextVar` that carries request identifiers across async coroutines without manually threading arguments.
2. **Logging** (`observability_logging.py`): structured logging with `structlog`, which automatically injects the request context into every log line.
3. **Metrics** (`observability_metrics.py`): OpenTelemetry instruments (histograms, counters, gauges) for measuring latency, module loads, events, hooks, errors, and background tasks.
4. **Telemetry** (`observability_telemetry.py`): OpenTelemetry provider configuration, auto-instrumentation of FastAPI/SQLAlchemy/httpx, OTLP/console exporters, and helpers for creating spans.

---

## File map

| File | Responsibility |
|---|---|
| [`utils/__init__.py`](../src/hotframe/utils/__init__.py) | Package documentation |
| [`utils/observability_context.py`](../src/hotframe/utils/observability_context.py) | `RequestContext`, `request_context`, `bind_context`, `update_context` |
| [`utils/observability_logging.py`](../src/hotframe/utils/observability_logging.py) | `setup_logging`, `get_logger` |
| [`utils/observability_metrics.py`](../src/hotframe/utils/observability_metrics.py) | OTel instruments: histograms, counters, gauges for requests, modules, events, hooks, errors, tasks |
| [`utils/observability_telemetry.py`](../src/hotframe/utils/observability_telemetry.py) | `setup_telemetry`, auto-instrumentation, `start_span`, `create_event_span`, `create_hook_span`, `create_module_span` |

---

## `observability_context.py` — Request context

### The problem

In an async server handling many concurrent requests, how do you know which request a log line belongs to when it is emitted from a deep utility function — without threading `request_id` as an argument all the way down the call stack?

Python 3.7+ solves this with `contextvars.ContextVar`: a variable "attached" to the current asyncio task (or thread), invisible to other concurrent tasks.

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
        """Returns only non-empty fields, for binding in structlog."""
```

A slots-based dataclass (slots for efficiency) holding the five context identifiers. `bind_dict()` filters out empty fields to avoid polluting logs with empty keys.

### `request_context: ContextVar[RequestContext]`

```python
request_context: ContextVar[RequestContext] = ContextVar(
    "request_context",
    default=RequestContext(),
)
```

The global context variable. The default is an empty `RequestContext`, so `request_context.get()` never raises `LookupError`.

### `bind_context(**kwargs) -> Generator[RequestContext, None, None]`

```python
@contextmanager
def bind_context(**kwargs: str) -> Generator[RequestContext, None, None]:
```

Context manager that sets the context for a block of code and restores it on exit (using `ContextVar.reset(token)`). This is the recommended usage from middleware:

```python
# In per-request middleware:
with bind_context(request_id=str(uuid4()), hub_id=str(hub_id), user_id=str(user.id)):
    response = await call_next(request)
```

Inside the block — and in any async function called from within it — `request_context.get()` returns the bound context. On exit, the previous context is restored (supporting nesting).

### `update_context(**kwargs) -> None`

```python
def update_context(**kwargs: str) -> None:
```

Updates fields on the current context without using a context manager. Useful when you learn information mid-request (e.g. the `user_id` after resolving the session):

```python
# After resolving the user:
update_context(user_id=str(user.id))
```

Does not automatically restore the previous value. For use in linear flows where you do not need to nest contexts.

---

## `observability_logging.py` — Structured logging

### Philosophy

`setup_logging` configures **structlog** as the logging engine, with two modes:

- **Development** (`json_output=False`): colored, human-readable output to the console.
- **Production** (`json_output=True`): JSON lines to stderr, one line per event, suitable for ingestion by Loki, CloudWatch, Datadog, etc.

### `setup_logging(*, log_level="INFO", json_output=False) -> None`

```python
def setup_logging(*, log_level: str = "INFO", json_output: bool = False) -> None:
```

Called once from `create_app`. It does four things:

1. **Configures structlog processors.** The processor chain is the same for both structlog and stdlib:
   - `structlog.contextvars.merge_contextvars` — merges structlog context variables.
   - `_add_request_context` — injects `request_id`, `hub_id`, etc. from `request_context`.
   - `structlog.stdlib.add_log_level` — adds the log level (`info`, `error`, etc.).
   - `structlog.stdlib.add_logger_name` — adds the logger name.
   - `structlog.processors.TimeStamper(fmt="iso")` — ISO-8601 timestamp.
   - `structlog.processors.StackInfoRenderer` — renders stack traces.
   - `structlog.processors.UnicodeDecoder` — decodes bytes to str.
   - `structlog.processors.CallsiteParameterAdder` — adds filename, lineno, func_name.
   - Final renderer: `JSONRenderer` (production) or `ConsoleRenderer` (development).

2. **Configures the stdlib root logger.** All `logging.getLogger(...)` calls from the Python ecosystem (SQLAlchemy, uvicorn, FastAPI, etc.) pass through structlog's `ProcessorFormatter`, which applies the same processor chain to them.

3. **Silences noisy loggers.** `uvicorn.access`, `watchfiles`, `httpcore`, `httpx`, and `hpack` are raised to `WARNING` to avoid flooding logs during development.

4. **Configures uvicorn.** `uvicorn.error` propagates to root (structlog); `uvicorn.root` has its handlers cleared to prevent duplicates.

### `_add_request_context` (internal processor)

```python
def _add_request_context(logger, method_name, event_dict):
    ctx = request_context.get()
    if ctx.request_id: event_dict.setdefault("request_id", ctx.request_id)
    if ctx.hub_id:     event_dict.setdefault("hub_id", ctx.hub_id)
    # ...
    return event_dict
```

A structlog processor that automatically injects `RequestContext` fields into every log event. Uses `setdefault` to avoid overwriting values the code already set explicitly.

### `get_logger(name=None) -> structlog.stdlib.BoundLogger`

```python
def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
```

A drop-in replacement for `logging.getLogger()`. Returns a structlog `BoundLogger` that automatically includes the request context:

```python
from hotframe.utils.observability_logging import get_logger

logger = get_logger(__name__)

async def process_order(order_id: str):
    logger.info("processing order", order_id=order_id)
    # → {"event": "processing order", "order_id": "...", "request_id": "...", "hub_id": "...", ...}
```

---

## `observability_metrics.py` — OpenTelemetry instruments

This module defines all of hotframe's metric instruments. Instruments are **lazily initialized**: they are created on first use, not at import time. If the OpenTelemetry SDK is not configured, the functions return no-op instruments with zero overhead.

### The Meter singleton

```python
_meter: metrics.Meter | None = None

def _get_meter() -> metrics.Meter:
    global _meter
    if _meter is None:
        _meter = metrics.get_meter("hotframe", version="0.1.0")
    return _meter
```

A single `Meter` named `"hotframe"` with version `"0.1.0"`. All instruments are created from it.

### Instrument categories

#### HTTP request metrics

```python
def get_request_duration_histogram() -> Histogram:
    # name="http.server.request.duration", unit="ms"
```

Histogram of HTTP request latency. Typical labels are `endpoint`, `method`, `status_code` — added by the middleware that uses this instrument when recording measurements.

#### Module metrics

```python
def get_module_load_duration_histogram() -> Histogram:
    # name="hotframe.module.load.duration", unit="ms"

def get_active_modules_counter() -> UpDownCounter:
    # name="hotframe.modules.active"
```

`UpDownCounter` is the OTel type for values that can go up and down (like a gauge). It is incremented when a module is activated and decremented when it is deactivated.

#### Event metrics

```python
def get_event_emit_counter() -> Counter:
    # name="hotframe.events.emitted"

def get_event_handler_duration_histogram() -> Histogram:
    # name="hotframe.events.handler.duration", unit="ms"
```

These allow you to answer: "How many times was `invoice.paid` emitted today?" and "What is the average duration of the `user.created` handler?".

#### Hook metrics

```python
def get_hook_duration_histogram() -> Histogram:
    # name="hotframe.hooks.duration", unit="ms"

def get_hook_callback_counter() -> Counter:
    # name="hotframe.hooks.callbacks.invoked"
```

#### Error metrics

```python
def get_error_counter() -> Counter:
    # name="hotframe.errors"
```

Labelable by `module` and `error_type`.

#### Background task metrics

```python
def get_tasks_pending_counter()   -> UpDownCounter  # hotframe.tasks.pending
def get_tasks_running_counter()   -> UpDownCounter  # hotframe.tasks.running
def get_tasks_completed_counter() -> Counter        # hotframe.tasks.completed
def get_tasks_failed_counter()    -> Counter        # hotframe.tasks.failed
```

Four instruments covering the complete async task lifecycle.

### `reset_metrics() -> None`

```python
def reset_metrics() -> None:
```

Resets all instruments to `None` and clears `_meter`. For testing only: allows each test to start with fresh instruments.

---

## `observability_telemetry.py` — OpenTelemetry configuration

### `setup_telemetry(*, service_name, debug, hub_id) -> None`

```python
def setup_telemetry(
    *,
    service_name: str = "hub",
    debug: bool = False,
    hub_id: str = "",
) -> None:
```

The single entry point for configuring all OTel observability. `create_app` calls it in step 1 of the bootstrap.

Trace exporter selection logic:

| Condition | Exporter |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` defined | OTLP gRPC (`opentelemetry-exporter-otlp-proto-grpc`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` empty + `debug=True` | Console (stdout) |
| Neither | No-op (no export, minimal overhead) |

If the OTLP package is not installed, `setup_telemetry` catches the `ImportError` and emits a `WARNING` instead of failing.

Resource attributes:

```python
resource_attrs = {
    ResourceAttributes.SERVICE_NAME: otel_service,
    ResourceAttributes.SERVICE_VERSION: "0.1.0",
}
if hub_id:
    resource_attrs["hub.id"] = hub_id
```

The `hub_id` is attached as a resource attribute to all spans — useful in multi-tenant deployments where multiple instances export to the same collector.

### `_setup_metrics_provider(otlp_endpoint, resource, debug) -> None`

Configures the `MeterProvider`. Same exporter selection logic: OTLP if an endpoint is configured, Console if debug, nothing otherwise. The OTLP reader exports every 30 seconds; the Console reader every 60 (less noisy in development).

### Auto-instrumentation

```python
def _auto_instrument_fastapi() -> None:
    FastAPIInstrumentor().instrument()

def _auto_instrument_sqlalchemy() -> None:
    SQLAlchemyInstrumentor().instrument()

def _auto_instrument_httpx() -> None:
    HTTPXClientInstrumentor().instrument()
```

Three functions that automatically instrument third-party frameworks. Each catches `ImportError` and logs at `DEBUG` if the instrumentation package is not installed — without crashing. This allows hotframe to be used with no OTel dependencies at all, adding them incrementally.

### Span helpers

#### `start_span(name, *, attributes, kind) -> AbstractContextManager[Span]`

```python
def start_span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
) -> AbstractContextManager[Span]:
```

Wrapper around `tracer.start_as_current_span`. Returns a context manager:

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

`AsyncEventBus` calls this helper when emitting events, automatically connecting event emission to distributed traces.

#### `create_hook_span(hook_name, hook_type="action") -> AbstractContextManager[Span]`

```python
def create_hook_span(hook_name: str, hook_type: str = "action"):
    return start_span(
        f"hook.{hook_type}:{hook_name}",
        attributes={"hook.name": hook_name, "hook.type": hook_type},
    )
```

Used by `HookRegistry` for each action/filter executed.

#### `create_module_span(operation, module_id) -> AbstractContextManager[Span]`

```python
def create_module_span(operation: str, module_id: str):
    return start_span(
        f"module.{operation}:{module_id}",
        attributes={"module.id": module_id, "module.operation": operation},
    )
```

Called by `ModuleRuntime` in `install`, `activate`, `deactivate`, and `update`. This makes every module operation visible in your OTel backend with its precise duration.

#### `get_tracer() -> Tracer`

```python
def get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer("hub", "0.1.0")
    return _tracer
```

Returns the framework's global tracer. If `setup_telemetry` was not called, returns a no-op tracer.

---

## How this fits into the rest of the framework

- **`create_app`**: calls `setup_logging` and `setup_telemetry` in the first steps of the bootstrap, before mounting middleware or registering routes.
- **`AsyncEventBus`**: uses `create_event_span` and `get_event_emit_counter` on every `emit`.
- **`HookRegistry`**: uses `create_hook_span` and `get_hook_duration_histogram`.
- **`ModuleRuntime`**: uses `create_module_span`, `get_module_load_duration_histogram`, and `get_active_modules_counter`.
- **Request middleware**: uses `bind_context` to set `request_id`, `hub_id`, `user_id` in the context, and `get_request_duration_histogram` to record latency.
- **`get_logger`**: is the direct replacement for `logging.getLogger`. All hotframe code (and application code) can use it and will automatically get enriched log output.

---

## Gotchas and design decisions

**Instruments are lazy singletons backed by module-level globals.** Each instrument (`_request_duration`, `_meter`, etc.) is a module-level global initialized on first call. This is a common pattern in OTel Python. The trade-off: in tests that import the module before configuring the SDK, instruments are no-ops and cannot be verified as called. `reset_metrics()` allows resetting between tests.

**`setup_telemetry` is idempotent in practice but not in its API.** If called twice, it creates two `TracerProvider` instances and calls `trace.set_tracer_provider` twice. The second call overwrites the first. This is not a problem in production (called once), but in tests that initialize the app multiple times you need to be careful.

**`bind_context` is not thread-safe in the traditional sense.** `ContextVar` isolates by asyncio task, not by thread. If you use `asyncio.create_task` or `asyncio.gather`, child tasks inherit the parent's context at the moment of their creation. If the context is modified afterwards (via `update_context`), child tasks do not see the change. Use `bind_context` in the parent task before spawning child tasks if you need them to inherit the full context.

**JSON in production, colors in development.** The `json_output` decision is made by `create_app` based on `settings.LOG_FORMAT` or the deployment mode. You do not need to configure it manually.

**Noisy loggers are silenced globally.** `uvicorn.access`, `watchfiles`, etc. are raised to `WARNING` in `setup_logging`. If you need their output at `DEBUG`, you will have to raise them back manually after calling `setup_logging`. There is no settings option for this in v1.0.

**OTel is zero-dependency at runtime.** All imports of optional packages (`opentelemetry-instrumentation-fastapi`, etc.) are inside `try/except ImportError` blocks. You can use hotframe without installing any OTel packages and the app works — just without traces or metrics.
