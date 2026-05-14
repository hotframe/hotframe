# 5. Eventos del ORM y transacciones (orm/)

> Esta sección cubre cómo hotframe convierte automáticamente las operaciones de base de datos en eventos del bus (`AsyncEventBus`), y cómo gestiona las transacciones con soporte para savepoints y callbacks post-commit. Para entender los eventos que se emiten aquí, lee primero la sección 14 sobre `signals/`.

---

## Para qué sirve esta carpeta

`orm/` cierra el ciclo entre la capa de persistencia (SQLAlchemy) y el sistema de eventos (`signals/`). Su misión es triple:

1. **Puente ORM → EventBus**: cada INSERT, UPDATE o DELETE en cualquier modelo SQLAlchemy genera automáticamente eventos tanto tipados como legacy, sin que el desarrollador escriba una sola línea.
2. **Gestión de transacciones**: `atomic` y `on_commit` dan control preciso sobre bloques transaccionales y sobre cuándo ejecutar efectos secundarios irreversibles (enviar emails, emitir notificaciones externas).
3. **Puente PostgreSQL NOTIFY → EventBus**: `PgNotifyBridge` escucha canales de PostgreSQL y re-emite las notificaciones como eventos del bus bajo el namespace `pg.*`.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`orm/__init__.py`](../src/hotframe/orm/__init__.py) | Docstring del paquete; lista los imports canónicos |
| [`orm/events.py`](../src/hotframe/orm/events.py) | `setup_orm_events()` — registra listeners SQLAlchemy que emiten al bus |
| [`orm/listeners.py`](../src/hotframe/orm/listeners.py) | `PgNotifyBridge` — puente PostgreSQL LISTEN/NOTIFY → AsyncEventBus |
| [`orm/transactions.py`](../src/hotframe/orm/transactions.py) | `atomic()`, `on_commit()` — gestión transaccional con savepoints |

---

## `orm/events.py` — El puente ORM → EventBus

### `setup_orm_events(bus, base=None)`

```python
def setup_orm_events(bus: Any, base: type[DeclarativeBase] | None = None) -> None:
```

Esta es la función central de toda la carpeta. `create_app()` la llama una sola vez durante el bootstrap, pasando el `AsyncEventBus` singleton.

**Parámetro `base`**: si se pasa una clase `DeclarativeBase`, los listeners se registran solo sobre los mappers de esa base. Si es `None` (el default), se registra en `Mapper` directamente, capturando **todos** los modelos mapeados en el proceso.

La función registra cinco listeners SQLAlchemy mediante `@event.listens_for(target, "evento", propagate=True)`:

| Evento SA | Listener interno | Qué emite |
|---|---|---|
| `before_insert` | `_before_insert` | `ModelPreSaveEvent(created=True)` + `"model.pre_save"` |
| `after_insert` | `_after_insert` | `ModelPostSaveEvent(created=True)` + `"model.post_save"` + `"{tablename}.created"` |
| `before_update` | `_before_update` | `ModelPreSaveEvent(created=False)` + `"model.pre_save"` |
| `after_update` | `_after_update` | `ModelPostSaveEvent(created=False)` + `"model.post_save"` + `"{tablename}.updated"` |
| `after_delete` | `_after_delete` | `ModelPreDeleteEvent` + `ModelPostDeleteEvent` + `"model.post_delete"` + `"{tablename}.deleted"` |

### Funciones auxiliares internas

#### `_emit_async(bus, event_name, **kwargs)`

Los listeners de SQLAlchemy son **funciones síncronas** (SA los llama sin `await`). Para poder emitir al `AsyncEventBus` async, esta función detecta si hay un event loop activo:

```python
def _emit_async(bus: Any, event_name: str, **kwargs: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Sin event loop: migraciones, CLI, tests sin async
        logger.debug("No event loop — skipping event emission: %s", event_name)
        return

    loop.create_task(bus.emit(event_name, **kwargs))
```

Si no hay loop (durante migraciones con Alembic, en el CLI, en tests síncronos), la emisión se omite silenciosamente. Si hay loop, crea una tarea asyncio. Esto significa que la emisión es **fire-and-forget**: el listener SQLAlchemy retorna inmediatamente y los handlers del bus se ejecutan en algún momento del loop.

#### `_emit_typed_async(bus, event)`

Mismo patrón pero para eventos Pydantic: llama `loop.create_task(bus.emit_typed(event))`.

#### `_get_tablename(instance)`, `_get_hub_id(instance)`, `_get_instance_id(instance)`

Helpers que extraen información del modelo:
- `_get_tablename`: usa `sa_inspect(type(instance)).local_table.name` para obtener el nombre de la tabla (no el de la clase Python).
- `_get_hub_id`: `getattr(instance, "hub_id", None)` — soporta los modelos con `HubMixin`.
- `_get_instance_id`: `getattr(instance, "id", None)`.

### Detalle de cada listener

#### `_before_insert` / `_before_update`

Además de emitir eventos, estos listeners realizan **mutaciones automáticas**:

- `_before_insert`: si el modelo tiene `created_at` y está a `None`, lo asigna a `datetime.now(UTC)`. Si tiene `hub_id` a `None`, intenta tomarlo de `session.info.get("hub_id")`.
- `_before_update`: si el modelo tiene `updated_at`, lo actualiza a `datetime.now(UTC)`.

Este es el mecanismo por el que los mixins `TimestampMixin` funcionan sin que el desarrollador tenga que actualizar `updated_at` manualmente.

#### `_after_insert` — los eventos `"{tablename}.created"`

```python
# Fragmento de _after_insert
if tablename:
    _emit_async(
        bus,
        f"{tablename}.created",
        sender=type(instance),
        instance=instance,
        hub_id=hub_id,
    )
```

Este es el origen de los eventos de alto nivel mencionados en GUIDE.md §11: `"models.User.created"` en realidad emite como `"users.created"` (el tablename, no el nombre de clase). Por ejemplo, si tienes `User.__tablename__ = "users"`, al crear un usuario el bus emite:
- `"model.pre_save"` (typed: `ModelPreSaveEvent`)
- `"model.post_save"` (typed: `ModelPostSaveEvent`)
- `"users.created"` (legacy, con `instance` completo)

Suscribirte a `"users.created"` te da acceso al objeto `instance` directamente en el handler legacy.

#### `_after_delete` — una peculiaridad importante

SQLAlchemy no tiene un evento `before_delete` en el mapper (existe a nivel de `Session`, no de `Mapper`). Por eso `_after_delete` emite **tanto** `ModelPreDeleteEvent` como `ModelPostDeleteEvent` en el mismo listener, consecutivamente. Ambos tienen los mismos datos, y el orden de emisión es: pre → post dentro del mismo `loop.create_task`.

### Cómo suscribirse a eventos ORM

**Interfaz legacy** (recibe el objeto completo):

```python
# Suscribirse a cualquier creación de cualquier modelo
await bus.subscribe("model.post_save", on_any_save)

# Suscribirse solo a creaciones de la tabla "invoices"
await bus.subscribe("invoices.created", on_invoice_created)

# Wildcard: cualquier evento de la tabla "invoices"
await bus.subscribe("invoices.*", on_invoice_any)
```

El handler legacy recibe `(event=str, sender=<ModelClass>, instance=<instance>, created=bool, hub_id=...)`.

**Interfaz tipada** (handler con tipo fuerte):

```python
from hotframe.signals.catalog import ModelPostSaveEvent

async def on_post_save(event: ModelPostSaveEvent) -> None:
    if event.model_name == "invoices" and event.created:
        await send_invoice_email(event.instance_id)

await bus.subscribe_typed(ModelPostSaveEvent, on_post_save, module_id="billing")
```

La interfaz tipada no recibe el objeto `instance` completo, solo el `instance_id`. Si necesitas el objeto, usa la interfaz legacy.

---

## `orm/listeners.py` — `PgNotifyBridge`

### Propósito

Permite que distintas instancias de la app (o procesos externos) se comuniquen a través de PostgreSQL sin un broker de mensajes externo. Útil para invalidar caches entre workers, sincronizar estado de módulos en deploys multi-instancia, o recibir notificaciones de procesos externos (scripts de migración, workers de Celery).

### `PgNotifyBridge`

```python
class PgNotifyBridge:
    def __init__(self) -> None:
        self._connection: asyncpg.Connection | None = None
        self._bus: Any | None = None
        self._channels: list[str] = []

    @property
    def is_connected(self) -> bool: ...

    async def start(self, dsn: str, bus: Any, channels: list[str]) -> None: ...
    async def stop(self) -> None: ...
    def _handle(self, connection, pid, channel, payload) -> None: ...

    @staticmethod
    async def notify(session: AsyncSession, channel: str, payload: Any = None) -> None: ...
```

**Dependencia opcional**: requiere `asyncpg`. Si no está instalado, `start()` lanza `ImportError` con instrucción de instalación. El import está protegido con `try/except` para no romper el arranque si no se usa.

#### `start(dsn, bus, channels)`

Abre una conexión asyncpg dedicada (distinta de la pool de SQLAlchemy) y registra `_handle` como listener para cada canal.

```python
bridge = PgNotifyBridge()
await bridge.start(
    dsn="postgresql://user:pass@localhost/myapp",
    bus=bus,
    channels=["module_sync", "cache_invalidate"],
)
```

#### `_handle(connection, pid, channel, payload)`

Es el callback que asyncpg invoca cuando llega un NOTIFY. Parsea el payload como JSON. Si no es JSON válido, lo envuelve como `{"raw": payload}`. Luego emite `f"pg.{channel}"` en el bus:

```python
loop.create_task(self._bus.emit(f"pg.{channel}", sender=self, **data))
```

Con esto, si alguien hace `NOTIFY module_sync, '{"module_id": "loyalty"}'` en PostgreSQL, el bus emite `"pg.module_sync"` con `module_id="loyalty"` como kwarg.

#### `notify(session, channel, payload)` (método estático)

Permite enviar un NOTIFY desde código Python usando la sesión SQLAlchemy activa:

```python
await PgNotifyBridge.notify(
    session,
    "module_sync",
    {"module_id": "loyalty", "action": "activated"},
)
```

Internamente ejecuta `SELECT pg_notify(:channel, :payload)`. Valida que el payload no supere los 8000 bytes (límite de PostgreSQL):

```python
if len(json_payload.encode("utf-8")) > 8000:
    raise ValueError(
        f"NOTIFY payload exceeds PostgreSQL 8000-byte limit ..."
    )
```

#### `stop()`

Elimina los listeners asyncpg y cierra la conexión limpiamente. Debe llamarse en el `lifespan` shutdown de la app.

#### Alias de compatibilidad

```python
setup_pg_notify = PgNotifyBridge
```

Para rutas de importación legacy.

---

## `orm/transactions.py` — `atomic` y `on_commit`

### `atomic(session)` — contexto transaccional con savepoints

```python
@asynccontextmanager
async def atomic(session: ISession):
```

Un gestor de contexto async que detecta automáticamente si ya hay una transacción activa:

- **Sin transacción activa** (`not session.in_transaction()`): abre con `async with session.begin()`. Al salir exitosamente, hace commit. Al salir con excepción, hace rollback.
- **Con transacción activa** (`session.in_transaction()`): abre con `async with session.begin_nested()`, que en SQLAlchemy asíncrono crea un `SAVEPOINT`. Al salir con excepción, hace rollback al savepoint. La transacción exterior no se ve afectada.

```python
async with atomic(session):
    session.add(invoice)

    async with atomic(session):  # → SAVEPOINT
        session.add(line_item)
        # Si esto falla, solo el line_item se revierte
```

Después del commit de la **transacción más externa**, dispara los callbacks `on_commit` registrados para esa sesión:

```python
# Después del commit externo:
callbacks = _commit_callbacks.pop(sid, [])
for cb in callbacks:
    result = cb()
    if hasattr(result, "__await__"):
        await result
```

Los callbacks solo se disparan en el commit del bloque más externo. Si el bloque usa savepoint (nested), los callbacks se acumulan y se ejecutan cuando finalmente se commitea el bloque exterior.

### `on_commit(session, callback)`

```python
def on_commit(session: ISession, callback: Callable[[], Any | Awaitable[Any]]) -> None:
```

Registra un callable (sync o async) para ejecutar después del commit. Los callbacks se indexan por `id(session)` y se almacenan en el módulo-level dict `_commit_callbacks`.

```python
async with atomic(session):
    await session.execute(...)
    on_commit(session, lambda: send_payment_confirmation(order_id))
    on_commit(session, lambda: invalidate_dashboard_cache(user_id))
```

Si la transacción hace rollback, `_commit_callbacks.pop(sid)` nunca se llama en el bloque `atomic`, y los callbacks se pierden. Importante: el `pop` solo ocurre en el bloque `else` implícito del `begin()` (cuando no hay excepción). Si hay excepción, el `begin()` hace rollback y el `pop` no se ejecuta.

### Detalle del storage

```python
_commit_callbacks: dict[int, list[Callable[[], Any | Awaitable[Any]]]] = {}
```

Dict a nivel de módulo. La clave es `id(session)`. No hay thread-safety explícita porque en un contexto asyncio cada sesión vive en una corutina, y el acceso a este dict es secuencial por la naturaleza del event loop.

---

## Cómo encaja con el resto del framework

```
create_app(settings)
    │
    ├─ crea AsyncEventBus (singleton)
    │
    ├─ llama setup_orm_events(bus)
    │       │
    │       └─ @event.listens_for(Mapper, "before_insert", ...)
    │          @event.listens_for(Mapper, "after_insert", ...)
    │          @event.listens_for(Mapper, "before_update", ...)
    │          @event.listens_for(Mapper, "after_update", ...)
    │          @event.listens_for(Mapper, "after_delete", ...)
    │
    │  Desde ese momento, cualquier operación ORM en cualquier parte del código
    │  emite automáticamente eventos al bus.
    │
    └─ (opcional) PgNotifyBridge.start(dsn, bus, channels=[...])
            └─ escucha canales PG y emite "pg.<channel>" al bus

Módulos que quieren reaccionar a eventos ORM:
    async def on_user_created(event="users.created", sender, instance, hub_id):
        await send_welcome_email(instance.email)

    await bus.subscribe("users.created", on_user_created, module_id="onboarding")
```

La separación de responsabilidades es limpia:
- `orm/events.py` no sabe nada de los módulos que escuchan; solo emite.
- `signals/dispatcher.py` no sabe nada de SQLAlchemy; solo despacha.
- Los módulos de negocio solo conocen el nombre del evento que les interesa.

Para transacciones:
- `orm/transactions.py` opera sobre cualquier `ISession` (el protocolo definido en `db/protocols.py`).
- Los módulos usan `atomic` y `on_commit` sin depender de SQLAlchemy directamente.

---

## Gotchas y decisiones de diseño

### 1. Los eventos ORM son fire-and-forget

Los listeners SQLAlchemy son síncronos. Usan `loop.create_task(bus.emit(...))`, lo que significa que los handlers del bus se ejecutan **después** de que el listener retorne, en algún tick futuro del event loop. El INSERT/UPDATE/DELETE no espera a que los handlers terminen.

Consecuencia práctica: no puedes abortar un INSERT desde un handler de `"users.created"`. Si necesitas validar antes, usa un handler de `"model.pre_save"` o un hook de `HookRegistry` llamado antes del flush.

### 2. `_before_insert` muta el objeto directamente

El listener `_before_insert` asigna `created_at`, `updated_at` y `hub_id` sobre el objeto en memoria **antes** de que SQLAlchemy genere el SQL. Esto es correcto porque los listeners `before_*` se ejecutan justo antes de la sentencia SQL. Sin embargo, si el objeto ya tiene `created_at` asignado (no es `None`), no se sobreescribe.

### 3. SQLAlchemy no tiene `before_delete` en Mapper

El evento `before_delete` existe en `Session` pero no en `Mapper`. Por eso `_after_delete` emite ambos `ModelPreDeleteEvent` y `ModelPostDeleteEvent`. Ambos tienen los mismos datos. Si tu caso de uso requiere actuar **antes** de que el DELETE ocurra, debes usar `@event.listens_for(Session, "before_bulk_delete")` o validar en el repositorio antes de llamar a `session.delete(instance)`.

### 4. `on_commit` no funciona fuera de `atomic`

`on_commit` registra callbacks que se disparan desde el bloque `atomic`. Si haces commit directamente con `await session.commit()` sin pasar por `atomic`, los callbacks de `on_commit` nunca se ejecutan. El dict `_commit_callbacks` acumularía entradas huérfanas.

### 5. `on_commit` con savepoints

Si tienes `atomic` anidados, `on_commit` acumula callbacks en todos los niveles pero solo los dispara al cerrar el bloque más externo. Si el bloque exterior hace rollback, todos los callbacks se pierden. Si el savepoint interior hace rollback pero el exterior commitea, los callbacks del bloque interior **también se ejecutan** (porque se acumulan en el mismo `sid` independientemente de la profundidad).

### 6. `PgNotifyBridge` usa una conexión independiente

asyncpg no usa la pool de SQLAlchemy. La conexión del bridge es persistente y dedicada al LISTEN. Esto es intencional: los LISTEN/NOTIFY de PostgreSQL requieren una conexión que permanezca abierta; no son compatibles con el uso normal de pool de conexiones donde las conexiones vuelven al pool entre queries.

### 7. El `instance` en handlers legacy puede estar en estado "detached"

En eventos `after_*`, el objeto `instance` ya fue procesado por SQLAlchemy pero la sesión puede haber expirado sus atributos. Si accedes a relaciones lazy en un handler async, puedes obtener un `DetachedInstanceError`. Usa `selectinload` o `joinedload` en la query original, o recarga el objeto desde el bus handler con una sesión nueva.

### 8. Los eventos tipados del ORM no llevan el objeto completo

`ModelPostSaveEvent` tiene `instance_id` (el `id` del objeto) pero no el objeto completo. Esto es deliberado: serializar un modelo SQLAlchemy en un evento Pydantic requeriría conocer su schema, lo que crearía acoplamiento entre `orm/` y los modelos de las apps. Si necesitas el objeto, usa la interfaz legacy de eventos (que sí lleva `instance`) o recárgalo desde el `instance_id` en el handler.
