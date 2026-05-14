# 14. Events, hooks, and signals (signals/)

> This section takes a deep dive into the three decoupled-communication mechanisms hotframe provides: the async pub/sub bus (`AsyncEventBus`), the WordPress-style filter and action registry (`HookRegistry`), and the Pydantic-typed event system. Section 11 of GUIDE.md introduces these concepts briefly; here we analyze them from the source code up.

---

## What this folder is for

`signals/` is hotframe's extensibility backbone. It lets distinct modules communicate without directly importing each other: a billing module emits `invoice.paid`; an accounting module and an email module are both subscribed to that event and react without the billing module ever knowing they exist.

The three mechanisms solve different problems:

| Mechanism | When to use it |
|---|---|
| `AsyncEventBus` | Decoupled communication, cross-module integrations, reacting to ORM changes |
| `HookRegistry` | Transforming values in transit (filters) or running side effects with guaranteed ordering (actions) |
| Typed events (`BaseEvent` + `register_event`) | Stable domain contracts where you want autocomplete, validation, and automatically documented interfaces |

---

## File map

| File | Responsibility |
|---|---|
| [`signals/__init__.py`](../src/hotframe/signals/__init__.py) | Public package docstring; lists the canonical imports |
| [`signals/dispatcher.py`](../src/hotframe/signals/dispatcher.py) | `AsyncEventBus`, `HandlerEntry`, `EmitResult` — the pub/sub engine |
| [`signals/hooks.py`](../src/hotframe/signals/hooks.py) | `HookRegistry`, `HookEntry`, `ActionResult` — filters and actions |
| [`signals/types.py`](../src/hotframe/signals/types.py) | `BaseEvent`, `EventRegistry`, `ValidationMode`, `register_event` — Pydantic contracts |
| [`signals/catalog.py`](../src/hotframe/signals/catalog.py) | All framework event classes registered with `@register_event` |
| [`signals/builtins.py`](../src/hotframe/signals/builtins.py) | String constants for signal names, plus `get_signal_event_map()` |

---

## `signals/dispatcher.py` — `AsyncEventBus`

### Overview

`AsyncEventBus` is the framework's primary async pub/sub bus. It implements two parallel interfaces — a *legacy* untyped interface and a *typed* Pydantic interface — that share the same handler pool. This enables a gradual migration: you can add an `emit_typed(...)` call today while existing `subscribe(...)` handlers keep working.

### Supporting data structures

**`HandlerEntry`** (dataclass with `slots=True`):

```python
@dataclass(slots=True)
class HandlerEntry:
    handler: Callable
    priority: int = 10
    module_id: str | None = None
    once: bool = False
    typed: bool = False
```

Every entry registered on the bus is a `HandlerEntry`. The `typed=True` flag indicates that the handler expects to receive a `BaseEvent` object directly; `typed=False` (legacy) expects `(event=str, sender=..., **data)`. The `module_id` field is critical for hot-cleanup when a module is deactivated.

**`EmitResult`** (dataclass with `slots=True`):

```python
@dataclass(slots=True)
class EmitResult:
    event: str
    handler_count: int
    errors: list[Exception]

    @property
    def success(self) -> bool:
        return not self.errors
```

Every call to `emit()` or `emit_typed()` returns an `EmitResult`. If `errors` is empty, `success` is `True`. Callers that ignore the return value won't break (backward compatibility).

### `AsyncEventBus.__init__`

```python
def __init__(
    self,
    *,
    registry: EventRegistry | None = None,
    validation_mode: ValidationMode = ValidationMode.PERMISSIVE,
) -> None:
```

- `_handlers: dict[str, list[HandlerEntry]]` — map from pattern to list of entries.
- `_lock: asyncio.Lock` — protects concurrent writes to `_handlers`.
- `_registry` — uses the global singleton `event_registry` if none is provided.
- `_validation_mode` — controls whether a warning is issued when `emit()` is called (untyped) for an event that already has a registered typed class.

### Legacy interface (untyped)

#### `subscribe(event, handler, *, priority, module_id, once)`

Registers a handler for an event pattern. Accepts wildcards (`sales.*`). Acquires `_lock` before writing.

```python
await bus.subscribe("invoice.*", my_handler, priority=5, module_id="billing")
```

#### `unsubscribe(event, handler)`

Removes a specific handler by object identity (`is not handler`). If the list becomes empty, removes the key from the dict.

#### `emit(event, *, sender, error_policy, **data)`

The central dispatch method. The key things it does:

1. **`error_policy` resolution**: if `None`, automatically applies `"fail_fast"` for events whose names start with any prefix in `CRITICAL_EVENT_PREFIXES` (`"sale."`, `"payment."`, `"inventory."`). All other events use `"collect"` (gathers errors without aborting).

2. **Validation warning**: if `validation_mode == WARN` and the event has a registered typed class, logs a warning.

3. **Wildcard matching**: iterates `_handlers`, comparing each pattern against the event name using `fnmatch(event, pattern)`. Exact and wildcard matches are merged into the same list.

4. **Priority ordering**: `matched.sort(key=lambda pair: pair[1].priority)`. Lower number = runs first.

5. **Invocation**: detects whether the handler is a coroutine (`inspect.iscoroutinefunction`) and either `await`s it or calls it directly.

6. **`once` handlers**: accumulates them in `once_to_remove` and removes them after dispatch completes (or before re-raising in `fail_fast` mode).

7. **Observability**: creates an OpenTelemetry span (`create_event_span`), records metrics via `get_event_emit_counter()` and `get_event_handler_duration_histogram()`.

```python
result = await bus.emit(
    "invoice.paid",
    sender=invoice,
    invoice_id=invoice.id,
    amount=invoice.total,
)
if not result.success:
    logger.warning("Some handlers failed: %s", result.errors)
```

### Typed interface (Pydantic)

#### `subscribe_typed(event_class, handler, *, priority, module_id, once)`

Registers a handler for a `BaseEvent` class. Auto-registers the class in the `EventRegistry` if it isn't already there. Creates a `HandlerEntry` with `typed=True`.

```python
await bus.subscribe_typed(InvoicePaidEvent, on_invoice_paid, module_id="accounting")
```

#### `emit_typed(event: BaseEvent)`

Emits an event that has already been constructed and validated by Pydantic. The flow is nearly identical to `emit()`, except:

- The event name is read from `type(event).event_name`.
- For `typed=True` handlers: calls them passing the `BaseEvent` object directly.
- For `typed=False` (legacy) handlers: calls `event.to_emit_kwargs()` once and passes the kwargs in legacy format.

This guarantees that **both interfaces are interoperable**: an `emit_typed(InvoicePaidEvent(...))` also fires legacy subscribers of `"invoice.paid"`.

```python
await bus.emit_typed(InvoicePaidEvent(invoice_id=42, amount=Decimal("99.00")))
```

### Module cleanup

#### `unsubscribe_module(module_id: str)`

Removes all `HandlerEntry` instances whose `module_id` matches. Acquires `_lock`. Called from `ModuleRuntime.deactivate()` to ensure a deactivated module leaves no orphaned handlers behind.

```python
await bus.unsubscribe_module("loyalty")
```

### Introspection

| Method | What it returns |
|---|---|
| `list_handlers(event)` | List of `HandlerEntry` objects that match (exact + wildcard), ordered by priority |
| `list_typed_events()` | `dict[str, type[BaseEvent]]` from the registry |
| `list_event_schemas()` | `dict[str, dict]` with Pydantic JSON schemas for all typed events |
| `handler_count` (property) | Total number of registered handlers |
| `clear()` | Clears everything — for use in tests |

### Automatic critical signals

```python
CRITICAL_EVENT_PREFIXES = {"sale.", "payment.", "inventory."}
```

Any event whose name starts with one of these prefixes automatically applies `error_policy="fail_fast"` unless the caller explicitly overrides it. This means an error in a handler for `sale.completed` **will re-raise the exception**, stopping the chain. For an event like `newsletter.sent`, a handler error is collected silently.

### `FakeEventBus` for tests

The bus does not include a built-in `FakeEventBus` class, but the `clear()` method and the constructor's `registry` parameter make it straightforward to instantiate a clean `AsyncEventBus()` per test. You can also pass a mock `EventRegistry`.

---

## `signals/hooks.py` — `HookRegistry`

### Overview

`HookRegistry` implements the WordPress hook pattern, adapted for async Python. The fundamental conceptual distinction is:

- **Actions**: execute side effects. The caller receives no return value.
- **Filters**: transform a value by passing it through a chain of callbacks. The caller receives the final value.

### Data structures

**`HookEntry`** (dataclass with `slots=True`):

```python
@dataclass(slots=True)
class HookEntry:
    callback: Callable
    priority: int = 10
    module_id: str | None = None
```

**`ActionResult`** (dataclass with `slots=True`):

```python
@dataclass(slots=True)
class ActionResult:
    hook: str
    callback_count: int
    errors: list[Exception]

    @property
    def success(self) -> bool:
        return not self.errors
```

### `HookRegistry.__init__`

Maintains two separate dicts:
- `_actions: dict[str, list[HookEntry]]`
- `_filters: dict[str, list[HookEntry]]`

There is no `asyncio.Lock` in `HookRegistry` because hook registration happens at startup/activation (non-concurrent) and execution is read-only from the dict.

### Registration

#### `add_action(hook, callback, *, priority, module_id)`

```python
hooks.add_action(
    "invoice.before_complete",
    validate_stock,
    priority=5,
    module_id="inventory",
)
```

#### `add_filter(hook, callback, *, priority, module_id)`

```python
hooks.add_filter(
    "invoice.line_price",
    apply_loyalty_discount,
    priority=10,
    module_id="loyalty",
)
```

### Execution

#### `do_action(hook, **kwargs) -> ActionResult`

Calls all action callbacks in priority order. Errors are caught, logged, and accumulated in `ActionResult.errors`, but they **do not stop** subsequent callbacks. Supports both sync and async callbacks.

```python
result = await hooks.do_action("invoice.before_complete", invoice=inv)
if not result.success:
    # A pre-validation hook failed
    raise InvoiceValidationError(result.errors)
```

#### `apply_filters(hook, value, **kwargs) -> Any`

Passes `value` through each callback in priority order. Each callback receives the result of the previous one:

```python
# callback signature: (value: T, **kwargs) -> T
final_price = await hooks.apply_filters("invoice.line_price", base_price, item=item)
```

If a callback raises an exception, it is logged and the value **is not updated** for that step (the previous value is passed to the next callback). This differs from `do_action`, where the error is collected but execution continues.

### Removal

#### `remove_action(hook, callback=None, module_id=None)`
#### `remove_filter(hook, callback=None, module_id=None)`

The internal logic (`_remove_from`) is:
- If neither `callback` nor `module_id` → removes **all** callbacks for the hook.
- If only `callback` → removes entries whose `callback is callback`.
- If only `module_id` → removes entries belonging to that module.
- If both → only entries that satisfy both conditions.

#### `remove_module_hooks(module_id: str)`

Clears **all** actions and filters for a module. Called by `ModuleRuntime` on deactivation.

### Introspection

```python
hooks.has_action("invoice.before_complete")   # → bool
hooks.has_filter("invoice.line_price")         # → bool
hooks.list_hooks()                             # → dict[str, int] (hook → total count)
```

### Key differences from `AsyncEventBus`

| Feature | `AsyncEventBus` | `HookRegistry` |
|---|---|---|
| Return value | No (returns `EmitResult`) | Yes for filters (transformed value) |
| Wildcards | Yes (`fnmatch`) | No |
| `once` subscriptions | Yes | No |
| Typed handlers | Yes (`subscribe_typed`) | No |
| Module cleanup | `unsubscribe_module()` | `remove_module_hooks()` |
| Error policies | `collect` / `fail_fast` | Always collect in actions; in filters, passes previous value |

---

## `signals/types.py` — Typed events

### `ValidationMode`

```python
class ValidationMode(str, Enum):
    STRICT = "strict"         # Rejects malformed events with an exception
    WARN = "warn"             # Logs a warning but still emits
    PERMISSIVE = "permissive" # Only Pydantic's own validation
```

The bus is created with `PERMISSIVE` by default to avoid breaking existing code. You can switch to `WARN` during a migration to detect legacy uses of events that already have a typed class.

### `BaseEvent`

```python
class BaseEvent(BaseModel):
    model_config = ConfigDict(
        frozen=True,          # Immutable after construction
        extra="forbid",       # Rejects undeclared fields
        ser_json_timedelta="iso8601",
        json_schema_extra={"description": "Hotframe typed event"},
    )

    event_name: ClassVar[str]   # MUST be declared in every subclass

    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    hub_id: UUID | None = None
    triggered_by: UUID | None = None
    source_module: str | None = None
```

**Auto-populated fields**: the `@model_validator(mode="before")` `_populate_context` tries to read `hub_id` and `triggered_by` from `request_context` (a contextvar). If no context is available (CLI, migrations, tests), it leaves them as `None` without failing.

**All events are immutable** (`frozen=True`). You cannot modify an event after creation; this guarantees that handlers cannot interfere with each other even when they share a reference to the same object.

#### `to_emit_kwargs() -> dict[str, Any]`

Converts the event to a dict compatible with the legacy interface:
```python
bus.emit(event.event_name, **event.to_emit_kwargs())
```

### `EventRegistry`

Singleton that maps `event_name` (string) ↔ `type[BaseEvent]` (class).

```python
class EventRegistry:
    def register(self, event_class: type[BaseEvent]) -> type[BaseEvent]
    def get_class(self, event_name: str) -> type[BaseEvent] | None
    def get_name(self, event_class: type[BaseEvent]) -> str | None
    def is_registered(self, event_name: str) -> bool
    def list_events(self) -> dict[str, type[BaseEvent]]
    def list_schemas(self) -> dict[str, dict[str, Any]]   # Pydantic JSON schemas
    def clear(self) -> None                                # For tests
```

**Duplicate protection**: attempting to register the same `event_name` with a different class raises `ValueError`. Registering the same class twice is idempotent.

The global singleton is `event_registry = EventRegistry()` at the bottom of the module.

### `register_event`

Convenience decorator that calls `event_registry.register(cls)`:

```python
@register_event
class InvoicePaidEvent(BaseEvent):
    event_name = "invoice.paid"

    invoice_id: int
    amount: Decimal
    currency: str = "EUR"
```

Once decorated, the class is available for `bus.subscribe_typed()` and `bus.emit_typed()`, and its JSON schema appears in `bus.list_event_schemas()`.

---

## `signals/catalog.py` — Framework event catalog

Defines and registers with `@register_event` all events that are native to hotframe. These are the events that the ORM, the module system, and the auth subsystems emit automatically.

### Model lifecycle events (emitted by `orm/events.py`)

| Class | `event_name` | When |
|---|---|---|
| `ModelPreSaveEvent` | `"model.pre_save"` | Before INSERT or UPDATE |
| `ModelPostSaveEvent` | `"model.post_save"` | After INSERT or UPDATE |
| `ModelPreDeleteEvent` | `"model.pre_delete"` | On delete (see gotcha in orm/) |
| `ModelPostDeleteEvent` | `"model.post_delete"` | After DELETE |

Common fields: `model_name` (tablename), `instance_id`, `created: bool`, `changes: dict`.

### Authentication events

| Class | `event_name` | Own fields |
|---|---|---|
| `AuthLoginEvent` | `"auth.login"` | `user_id_auth: UUID`, `method: str = "password"` |
| `AuthLogoutEvent` | `"auth.logout"` | `user_id_auth: UUID` |

### Module events

| Class | `event_name` | Own fields |
|---|---|---|
| `ModuleInstalledEvent` | `"modules.installed"` | `module_id: str`, `version: str` |
| `ModuleActivatedEvent` | `"modules.activated"` | `module_id: str`, `version: str` |
| `ModuleDeactivatedEvent` | `"modules.deactivated"` | `module_id: str`, `version: str` |
| `ModuleUpdatedEvent` | `"modules.updated"` | `module_id`, `previous_version`, `new_version` |
| `ModuleUninstalledEvent` | `"modules.uninstalled"` | `module_id: str`, `version: str` |

### Sync events

`SyncStartedEvent`, `SyncCompletedEvent` (with `records_synced: int`), `SyncFailedEvent` (with `error: str`).

### Print events

`PrintRequestedEvent` (with `job_id`, `document_type`, `printer_id`), `PrintCompletedEvent`, `PrintFailedEvent`.

---

## `signals/builtins.py` — Signal constants

Provides string constants for all system signal names, eliminating magic strings scattered throughout the codebase:

```python
MODEL_PRE_SAVE   = "model.pre_save"
MODEL_POST_SAVE  = "model.post_save"
MODEL_PRE_DELETE = "model.pre_delete"
MODEL_POST_DELETE = "model.post_delete"

AUTH_LOGIN  = "auth.login"
AUTH_LOGOUT = "auth.logout"

MODULES_INSTALLED   = "modules.installed"
MODULES_ACTIVATED   = "modules.activated"
MODULES_DEACTIVATED = "modules.deactivated"
MODULES_UPDATED     = "modules.updated"
MODULES_UNINSTALLED = "modules.uninstalled"
# ... sync and print
```

### `SYSTEM_SIGNALS`

Dict generated on the fly:

```python
SYSTEM_SIGNALS: dict[str, str] = {
    name: value
    for name, value in globals().items()
    if isinstance(value, str) and not name.startswith("_") and "." in value
}
```

Includes all strings that contain a dot. Useful for introspection or for validating that an incoming signal is one the framework recognizes.

### `get_signal_event_map()` and `get_event_class(signal)`

Map a string constant to the corresponding `BaseEvent` class from the catalog, with lazy initialization to avoid forcing the catalog import at startup:

```python
event_class = get_event_class(builtins.MODEL_POST_SAVE)  # → ModelPostSaveEvent
```

---

## How this fits into the rest of the framework

```
Bootstrap (create_app)
    │
    ├─ creates AsyncEventBus (app singleton)
    ├─ creates HookRegistry (app singleton)
    │
    ├─ calls setup_orm_events(bus)
    │     └─ registers SQLAlchemy listeners → they emit to the bus
    │
    └─ when a module is activated (ModuleRuntime.activate)
          ├─ the module registers its handlers: bus.subscribe(...)
          ├─ the module registers its hooks: hooks.add_filter(...)
          └─ on deactivation: bus.unsubscribe_module(id) + hooks.remove_module_hooks(id)
```

The bus and registry singletons live on `app.state` (managed by `create_app`). Modules receive them via dependency injection or through the `AppContext`.

---

## Gotchas and design decisions

### 1. Priority: lower number runs first

The default is `10`. If you want your hook to run before the framework's own hooks, use `priority=1`. If you want it to run last, use `priority=100`. This follows the same convention as WordPress.

### 2. `emit()` is fire-and-forget for errors in `collect` mode

In the default mode (`collect`), if a handler raises an exception, the remaining handlers **keep running**. The exception is captured in `EmitResult.errors` and never blocks the emitter. For critical events (`sale.*`, `payment.*`, `inventory.*`), the behavior is reversed: the first exception aborts the chain and is re-raised to the caller.

### 3. `once` handlers are removed even if they fail

If a handler marked `once=True` raises an exception during `emit_typed`, it is removed from the bus regardless once dispatch finishes. This is intentional: if the event was already processed (even with an error), you don't want it to be processed again.

### 4. Typed events are immutable (frozen)

`BaseEvent` uses `ConfigDict(frozen=True)`. This means you cannot do `event.hub_id = something` after construction. If you need to enrich an event inside a handler, you must create a new one.

### 5. Cross-interface compatibility

When `emit_typed(MyEvent(...))` is called, legacy handlers subscribed via `bus.subscribe("my.event", fn)` are also called, receiving `(event="my.event", sender=None, **event.to_emit_kwargs())`. The reverse direction works too: `emit("my.event", **data)` attempts to construct the typed class and call `typed=True` handlers.

### 6. No wildcards in `HookRegistry`

Unlike `AsyncEventBus`, `HookRegistry` does not support wildcards. `add_action("sale.*", cb)` registers a hook literally named `"sale.*"`, which will never match `do_action("sale.completed")`. This is an intentional design decision: hooks are well-defined extension points, not an event bus.

### 7. `apply_filters` silently absorbs exceptions

If a filter raises an error, it is logged but the filter value **does not advance** — the previous value is passed to the next callback. This differs from `do_action` (which collects the error in `ActionResult`) because in a filter there is no way to know what value to return on failure without full knowledge of the surrounding context.

### 8. Module cleanup on deactivation

The module system automatically calls `bus.unsubscribe_module(module_id)` and `hooks.remove_module_hooks(module_id)` on deactivation. For this reason it is critical to always pass `module_id` when registering handlers, if the code belongs to a dynamic module. Without it, handlers remain as orphans in memory indefinitely.
