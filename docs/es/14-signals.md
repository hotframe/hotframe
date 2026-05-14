# 14. Eventos, hooks y señales (signals/)

> Esta sección profundiza en los tres mecanismos de comunicación desacoplada que hotframe proporciona: el bus pub/sub asíncrono (`AsyncEventBus`), el registro de filtros y acciones al estilo WordPress (`HookRegistry`), y el sistema de eventos Pydantic tipados. La sección 11 de GUIDE.md introduce estos conceptos brevemente; aquí los analizamos desde el código fuente.

---

## Para qué sirve esta carpeta

`signals/` es la columna vertebral de extensibilidad de hotframe. Permite que distintos módulos se comuniquen sin importarse directamente: un módulo de facturación emite `invoice.paid`; un módulo de contabilidad y otro de email están suscritos al mismo evento y reaccionan sin que el módulo de facturación los conozca.

Los tres mecanismos resuelven problemas distintos:

| Mecanismo | Cuándo usarlo |
|---|---|
| `AsyncEventBus` | Comunicación desacoplada, integraciones entre módulos, reaccionar a cambios del ORM |
| `HookRegistry` | Transformar valores en tránsito (filtros) o ejecutar efectos secundarios con orden garantizado (acciones) |
| Eventos tipados (`BaseEvent` + `register_event`) | Dominio estable donde quieres autocompletado, validación y documentación automática del contrato |

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`signals/__init__.py`](../src/hotframe/signals/__init__.py) | Docstring público del paquete; lista los imports canónicos |
| [`signals/dispatcher.py`](../src/hotframe/signals/dispatcher.py) | `AsyncEventBus`, `HandlerEntry`, `EmitResult` — el motor pub/sub |
| [`signals/hooks.py`](../src/hotframe/signals/hooks.py) | `HookRegistry`, `HookEntry`, `ActionResult` — filtros y acciones |
| [`signals/types.py`](../src/hotframe/signals/types.py) | `BaseEvent`, `EventRegistry`, `ValidationMode`, `register_event` — contratos Pydantic |
| [`signals/catalog.py`](../src/hotframe/signals/catalog.py) | Todas las clases de eventos del framework registradas con `@register_event` |
| [`signals/builtins.py`](../src/hotframe/signals/builtins.py) | Constantes de string para los nombres de señal, más `get_signal_event_map()` |

---

## `signals/dispatcher.py` — `AsyncEventBus`

### Visión general

`AsyncEventBus` es el bus principal de pub/sub asíncrono del framework. Implementa dos interfaces paralelas —*legacy* sin tipado y *typed* con Pydantic— que comparten el mismo pool de handlers. Esto permite una migración gradual: puedes añadir un `emit_typed(...)` hoy y los handlers `subscribe(...)` antiguos siguen funcionando.

### Estructuras de datos auxiliares

**`HandlerEntry`** (dataclass con `slots=True`):

```python
@dataclass(slots=True)
class HandlerEntry:
    handler: Callable
    priority: int = 10
    module_id: str | None = None
    once: bool = False
    typed: bool = False
```

Cada entrada registrada en el bus es un `HandlerEntry`. El campo `typed=True` indica que el handler espera recibir un objeto `BaseEvent` directamente; `typed=False` (legacy) espera `(event=str, sender=..., **data)`. El campo `module_id` es crítico para la limpieza en caliente al desactivar un módulo.

**`EmitResult`** (dataclass con `slots=True`):

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

Cada llamada a `emit()` o `emit_typed()` devuelve un `EmitResult`. Si `errors` está vacío, `success` es `True`. Los llamantes que ignoran el retorno no se rompen (compatibilidad hacia atrás).

### `AsyncEventBus.__init__`

```python
def __init__(
    self,
    *,
    registry: EventRegistry | None = None,
    validation_mode: ValidationMode = ValidationMode.PERMISSIVE,
) -> None:
```

- `_handlers: dict[str, list[HandlerEntry]]` — mapa de patrón a lista de entradas.
- `_lock: asyncio.Lock` — protege las escrituras concurrentes sobre `_handlers`.
- `_registry` — usa el singleton global `event_registry` si no se pasa uno.
- `_validation_mode` — controla si se advierte cuando se usa `emit()` untyped para un evento que tiene clase tipada registrada.

### Interface legacy (untyped)

#### `subscribe(event, handler, *, priority, module_id, once)`

Registra un handler para un patrón de evento. Acepta wildcards (`sales.*`). Adquiere `_lock` antes de escribir.

```python
await bus.subscribe("invoice.*", my_handler, priority=5, module_id="billing")
```

#### `unsubscribe(event, handler)`

Elimina un handler concreto por identidad de objeto (`is not handler`). Si la lista queda vacía, borra la clave del dict.

#### `emit(event, *, sender, error_policy, **data)`

El método central de dispatch. Lo más importante que hace:

1. **Resolución de `error_policy`**: si es `None`, aplica `"fail_fast"` automáticamente para eventos cuyos nombres empiezan por los prefijos en `CRITICAL_EVENT_PREFIXES` (`"sale."`, `"payment."`, `"inventory."`). El resto usa `"collect"` (recoge errores sin abortar).

2. **Advertencia de validación**: si `validation_mode == WARN` y el evento tiene clase tipada registrada, loguea un warning.

3. **Matching con wildcards**: itera `_handlers`, compara cada patrón con `fnmatch(event, pattern)`. Mezcla coincidencias exactas y wildcard en la misma lista.

4. **Orden por prioridad**: `matched.sort(key=lambda pair: pair[1].priority)`. Número menor = ejecuta antes.

5. **Invocación**: detecta si el handler es coroutine (`inspect.iscoroutinefunction`) y hace `await` o llama directo.

6. **Handlers `once`**: los acumula en `once_to_remove` y los elimina al final del dispatch (o antes de relanzar en `fail_fast`).

7. **Observabilidad**: crea un span OpenTelemetry (`create_event_span`), registra métricas con `get_event_emit_counter()` y `get_event_handler_duration_histogram()`.

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

### Interface typed (Pydantic)

#### `subscribe_typed(event_class, handler, *, priority, module_id, once)`

Registra un handler para una clase `BaseEvent`. Auto-registra la clase en el `EventRegistry` si no está ya. Crea un `HandlerEntry` con `typed=True`.

```python
await bus.subscribe_typed(InvoicePaidEvent, on_invoice_paid, module_id="accounting")
```

#### `emit_typed(event: BaseEvent)`

Emite un evento ya construido y validado por Pydantic. El flujo es casi idéntico a `emit()` pero:

- El nombre del evento se lee de `type(event).event_name`.
- Para handlers `typed=True`: los llama pasando el objeto `BaseEvent` directamente.
- Para handlers `typed=False` (legacy): llama `event.to_emit_kwargs()` una sola vez y pasa los kwargs en formato legacy.

Esto garantiza que **ambas interfaces son interoperables**: un `emit_typed(InvoicePaidEvent(...))` también activa a los suscriptores legacy de `"invoice.paid"`.

```python
await bus.emit_typed(InvoicePaidEvent(invoice_id=42, amount=Decimal("99.00")))
```

### Limpieza de módulos

#### `unsubscribe_module(module_id: str)`

Elimina todos los `HandlerEntry` cuyo `module_id` coincide. Adquiere `_lock`. Se llama desde `ModuleRuntime.deactivate()` para garantizar que un módulo desactivado no deja handlers huérfanos.

```python
await bus.unsubscribe_module("loyalty")
```

### Introspección

| Método | Qué devuelve |
|---|---|
| `list_handlers(event)` | Lista de `HandlerEntry` que coinciden (exacto + wildcard), ordenados por prioridad |
| `list_typed_events()` | `dict[str, type[BaseEvent]]` desde el registry |
| `list_event_schemas()` | `dict[str, dict]` con JSON schemas Pydantic de todos los eventos tipados |
| `handler_count` (property) | Total de handlers registrados |
| `clear()` | Borra todo — para tests |

### Señales críticas automáticas

```python
CRITICAL_EVENT_PREFIXES = {"sale.", "payment.", "inventory."}
```

Cualquier evento cuyo nombre empiece por uno de estos prefijos aplica automáticamente `error_policy="fail_fast"` a menos que el llamante lo sobreescriba explícitamente. Esto significa que un error en un handler de `sale.completed` **relanzará la excepción**, interrumpiendo la cadena. Para un evento como `newsletter.sent`, un error en un handler se recoge silenciosamente.

### `FakeEventBus` para tests

El bus no tiene una clase `FakeEventBus` incorporada en el código fuente, pero el método `clear()` y el constructor aceptan un `registry` externo, lo que permite instanciar un `AsyncEventBus()` limpio por test. También puedes pasar un mock de `EventRegistry`.

---

## `signals/hooks.py` — `HookRegistry`

### Visión general

`HookRegistry` implementa el patrón de hooks de WordPress, adaptado para Python async. La distinción conceptual fundamental es:

- **Actions**: ejecutan efectos secundarios. El llamante no recibe ningún valor de retorno.
- **Filters**: transforman un valor pasándolo por una cadena de callbacks. El llamante recibe el valor final.

### Estructuras de datos

**`HookEntry`** (dataclass con `slots=True`):

```python
@dataclass(slots=True)
class HookEntry:
    callback: Callable
    priority: int = 10
    module_id: str | None = None
```

**`ActionResult`** (dataclass con `slots=True`):

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

Mantiene dos dicts separados:
- `_actions: dict[str, list[HookEntry]]`
- `_filters: dict[str, list[HookEntry]]`

No hay `asyncio.Lock` en el `HookRegistry` porque el registro de hooks ocurre en startup/activation (no concurrente) y la ejecución es de sólo lectura del dict.

### Registro

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

### Ejecución

#### `do_action(hook, **kwargs) -> ActionResult`

Llama todos los callbacks de acción en orden de prioridad. Los errores se capturan, se loguean y se acumulan en `ActionResult.errors`, pero **no detienen** los callbacks siguientes. Soporta callbacks sync y async.

```python
result = await hooks.do_action("invoice.before_complete", invoice=inv)
if not result.success:
    # Algún hook de pre-validación falló
    raise InvoiceValidationError(result.errors)
```

#### `apply_filters(hook, value, **kwargs) -> Any`

Pasa `value` por cada callback en orden de prioridad. Cada callback recibe el resultado del anterior:

```python
# callback firma: (value: T, **kwargs) -> T
final_price = await hooks.apply_filters("invoice.line_price", base_price, item=item)
```

Si un callback lanza excepción, se loguea y el valor **no se actualiza** para ese paso (se pasa el valor anterior al siguiente). Esto es diferente a `do_action`, donde el error se recoge pero la ejecución continúa.

### Eliminación

#### `remove_action(hook, callback=None, module_id=None)`
#### `remove_filter(hook, callback=None, module_id=None)`

La lógica interna (`_remove_from`) es:
- Si ni `callback` ni `module_id` → elimina **todos** los callbacks del hook.
- Si solo `callback` → elimina entradas cuyo `callback is callback`.
- Si solo `module_id` → elimina entradas de ese módulo.
- Si ambos → solo las entradas que cumplen los dos filtros.

#### `remove_module_hooks(module_id: str)`

Limpia **todos** los actions y filters de un módulo. Llamado por `ModuleRuntime` al desactivar.

### Introspección

```python
hooks.has_action("invoice.before_complete")   # → bool
hooks.has_filter("invoice.line_price")         # → bool
hooks.list_hooks()                             # → dict[str, int] (hook → count total)
```

### Diferencia clave con `AsyncEventBus`

| Característica | `AsyncEventBus` | `HookRegistry` |
|---|---|---|
| Retorno de valor | No (devuelve `EmitResult`) | Sí en filters (valor transformado) |
| Wildcards | Sí (`fnmatch`) | No |
| Subscripción `once` | Sí | No |
| Handlers tipados | Sí (`subscribe_typed`) | No |
| Limpieza por módulo | `unsubscribe_module()` | `remove_module_hooks()` |
| Políticas de error | `collect` / `fail_fast` | Siempre collect en actions; en filters, pasa valor anterior |

---

## `signals/types.py` — Eventos tipados

### `ValidationMode`

```python
class ValidationMode(str, Enum):
    STRICT = "strict"     # Rechaza eventos mal formados con excepción
    WARN = "warn"         # Loguea warning pero emite
    PERMISSIVE = "permissive"  # Solo la validación de Pydantic
```

El bus se crea con `PERMISSIVE` por defecto para no romper código existente. Puedes cambiar a `WARN` durante la migración para detectar usos legacy de eventos que ya tienen clase tipada.

### `BaseEvent`

```python
class BaseEvent(BaseModel):
    model_config = ConfigDict(
        frozen=True,          # Inmutable después de la construcción
        extra="forbid",       # No acepta campos no declarados
        ser_json_timedelta="iso8601",
        json_schema_extra={"description": "Hotframe typed event"},
    )

    event_name: ClassVar[str]   # DEBE declararse en cada subclase

    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    hub_id: UUID | None = None
    triggered_by: UUID | None = None
    source_module: str | None = None
```

**Campos auto-poblados**: el `@model_validator(mode="before")` `_populate_context` intenta leer `hub_id` y `triggered_by` de `request_context` (un contextvar). Si no hay contexto (CLI, migraciones, tests), los deja en `None` sin fallar.

**Todos los eventos son inmutables** (`frozen=True`). No puedes modificar un evento después de crearlo; eso garantiza que los handlers no interfieren entre sí aunque compartan la referencia al objeto.

#### `to_emit_kwargs() -> dict[str, Any]`

Convierte el evento a un dict compatible con la interface legacy:
```python
bus.emit(event.event_name, **event.to_emit_kwargs())
```

### `EventRegistry`

Singleton que mapea `event_name` (string) ↔ `type[BaseEvent]` (clase).

```python
class EventRegistry:
    def register(self, event_class: type[BaseEvent]) -> type[BaseEvent]
    def get_class(self, event_name: str) -> type[BaseEvent] | None
    def get_name(self, event_class: type[BaseEvent]) -> str | None
    def is_registered(self, event_name: str) -> bool
    def list_events(self) -> dict[str, type[BaseEvent]]
    def list_schemas(self) -> dict[str, dict[str, Any]]   # JSON schemas Pydantic
    def clear(self) -> None                                # Para tests
```

**Protección de duplicados**: si intentas registrar el mismo `event_name` con una clase distinta, lanza `ValueError`. Registrar la misma clase dos veces es idempotente.

El singleton global es `event_registry = EventRegistry()` al final del módulo.

### `register_event`

Decorador de conveniencia que llama `event_registry.register(cls)`:

```python
@register_event
class InvoicePaidEvent(BaseEvent):
    event_name = "invoice.paid"

    invoice_id: int
    amount: Decimal
    currency: str = "EUR"
```

Una vez decorada, la clase está disponible para `bus.subscribe_typed()` y `bus.emit_typed()`, y su JSON schema aparece en `bus.list_event_schemas()`.

---

## `signals/catalog.py` — Catálogo de eventos del framework

Define y registra con `@register_event` todos los eventos propios de hotframe. Son los que el ORM, el sistema de módulos y los subsistemas de auth emiten automáticamente.

### Eventos de ciclo de vida del modelo (emitidos por `orm/events.py`)

| Clase | `event_name` | Cuándo |
|---|---|---|
| `ModelPreSaveEvent` | `"model.pre_save"` | Antes de INSERT o UPDATE |
| `ModelPostSaveEvent` | `"model.post_save"` | Después de INSERT o UPDATE |
| `ModelPreDeleteEvent` | `"model.pre_delete"` | Al eliminar (ver gotcha en orm/) |
| `ModelPostDeleteEvent` | `"model.post_delete"` | Después de DELETE |

Campos comunes: `model_name` (tablename), `instance_id`, `created: bool`, `changes: dict`.

### Eventos de autenticación

| Clase | `event_name` | Campos propios |
|---|---|---|
| `AuthLoginEvent` | `"auth.login"` | `user_id_auth: UUID`, `method: str = "password"` |
| `AuthLogoutEvent` | `"auth.logout"` | `user_id_auth: UUID` |

### Eventos de módulos

| Clase | `event_name` | Campos propios |
|---|---|---|
| `ModuleInstalledEvent` | `"modules.installed"` | `module_id: str`, `version: str` |
| `ModuleActivatedEvent` | `"modules.activated"` | `module_id: str`, `version: str` |
| `ModuleDeactivatedEvent` | `"modules.deactivated"` | `module_id: str`, `version: str` |
| `ModuleUpdatedEvent` | `"modules.updated"` | `module_id`, `previous_version`, `new_version` |
| `ModuleUninstalledEvent` | `"modules.uninstalled"` | `module_id: str`, `version: str` |

### Eventos de sincronización

`SyncStartedEvent`, `SyncCompletedEvent` (con `records_synced: int`), `SyncFailedEvent` (con `error: str`).

### Eventos de impresión

`PrintRequestedEvent` (con `job_id`, `document_type`, `printer_id`), `PrintCompletedEvent`, `PrintFailedEvent`.

---

## `signals/builtins.py` — Constantes de señal

Proporciona las constantes string para todos los nombres de señal del sistema, evitando magic strings dispersos por el código:

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
# ... sync y print
```

### `SYSTEM_SIGNALS`

Dict autogenerado al vuelo:

```python
SYSTEM_SIGNALS: dict[str, str] = {
    name: value
    for name, value in globals().items()
    if isinstance(value, str) and not name.startswith("_") and "." in value
}
```

Incluye todos los strings que contengan un punto. Útil para introspección o para validar que una señal que llega es conocida por el framework.

### `get_signal_event_map()` y `get_event_class(signal)`

Mapean constante string → clase `BaseEvent` del catálogo, con lazy initialization para no forzar la importación del catálogo al arrancar:

```python
event_class = get_event_class(builtins.MODEL_POST_SAVE)  # → ModelPostSaveEvent
```

---

## Cómo encaja con el resto del framework

```
Bootstrap (create_app)
    │
    ├─ crea AsyncEventBus (singleton de app)
    ├─ crea HookRegistry (singleton de app)
    │
    ├─ llama setup_orm_events(bus)
    │     └─ registra listeners SQLAlchemy → emiten al bus
    │
    └─ al activar un módulo (ModuleRuntime.activate)
          ├─ el módulo registra sus handlers: bus.subscribe(...)
          ├─ el módulo registra sus hooks: hooks.add_filter(...)
          └─ al desactivar: bus.unsubscribe_module(id) + hooks.remove_module_hooks(id)
```

Los singletons del bus y el registry viven en `app.state` (gestionados por `create_app`). Los módulos los reciben por inyección de dependencias o a través del `AppContext`.

---

## Gotchas y decisiones de diseño

### 1. Prioridad: número menor ejecuta antes

El default es `10`. Si quieres que tu hook corra antes que los del framework, usa `priority=1`. Si quieres que corra al final, usa `priority=100`. Es el mismo convenio de WordPress.

### 2. `emit()` es fire-and-forget para errores en modo `collect`

En el modo por defecto (`collect`), si un handler lanza excepción, los demás handlers **siguen ejecutando**. La excepción se recoge en `EmitResult.errors`. Nunca bloquea al emisor. Para eventos críticos (`sale.*`, `payment.*`, `inventory.*`), el comportamiento se invierte: la primera excepción aborta la cadena y se relanza al llamante.

### 3. Handlers `once` se eliminan aunque fallen

Si un handler marcado como `once=True` lanza excepción durante `emit_typed`, se elimina de todos modos al finalizar el dispatch. Esto es intencional: si el evento ya se procesó (aunque con error), no se quiere que se procese de nuevo.

### 4. Los eventos tipados son inmutables (frozen)

`BaseEvent` usa `ConfigDict(frozen=True)`. Esto significa que no puedes hacer `event.hub_id = something` después de la construcción. Si necesitas enriquecer un evento en un handler, debes crear uno nuevo.

### 5. Compatibilidad cruzada de interfaces

Cuando se llama `emit_typed(MyEvent(...))`, los handlers legacy suscritos a `bus.subscribe("my.event", fn)` también se llaman, recibiendo `(event="my.event", sender=None, **event.to_emit_kwargs())`. La dirección inversa también funciona: `emit("my.event", **data)` intenta construir la clase tipada y llamar a los handlers `typed=True`.

### 6. Sin wildcards en `HookRegistry`

A diferencia del `AsyncEventBus`, el `HookRegistry` no soporta wildcards. `add_action("sale.*", cb)` registra literalmente un hook llamado `"sale.*"`, que nunca va a coincidir con `do_action("sale.completed")`. Es una diferencia intencional de diseño: los hooks son puntos de extensión bien definidos, no buses de eventos.

### 7. `apply_filters` absorbe excepciones silenciosamente

Si un filtro falla, el error se loguea pero el valor del filtro **no avanza** — se pasa el valor anterior al siguiente callback. Esto es diferente de `do_action` (que sí recoge el error en `ActionResult`) porque en un filtro no hay forma de saber qué valor devolver ante un fallo sin conocer el contexto completo.

### 8. Limpieza del módulo al desactivar

El sistema de módulos llama automáticamente `bus.unsubscribe_module(module_id)` y `hooks.remove_module_hooks(module_id)` al desactivar. Por eso es crítico pasar siempre `module_id` al registrar handlers, si el código pertenece a un módulo dinámico. Si no lo pasas, los handlers quedan huérfanos en memoria indefinidamente.