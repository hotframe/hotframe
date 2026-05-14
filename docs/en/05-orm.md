# 5. ORM Events and Transactions (`orm/`)

> This section covers how hotframe automatically converts database operations into event bus (`AsyncEventBus`) events, and how it manages transactions with savepoint support and post-commit callbacks. To understand the events emitted here, read section 14 on `signals/` first.

---

## What this folder is for

`orm/` closes the loop between the persistence layer (SQLAlchemy) and the event system (`signals/`). Its mission is threefold:

1. **ORM → EventBus bridge**: every INSERT, UPDATE, or DELETE on any SQLAlchemy model automatically generates both typed and legacy events, without the developer writing a single line of wiring.
2. **Transaction management**: `atomic` and `on_commit` give precise control over transactional blocks and over when to execute irreversible side-effects (sending emails, emitting external notifications).
3. **PostgreSQL NOTIFY → EventBus bridge**: `PgNotifyBridge` listens on PostgreSQL channels and re-emits notifications as bus events under the `pg.*` namespace.

---

## File map

| File | Responsibility |
|---|---|
| [`orm/__init__.py`](../src/hotframe/orm/__init__.py) | Package docstring; lists canonical imports |
| [`orm/events.py`](../src/hotframe/orm/events.py) | `setup_orm_events()` — registers SQLAlchemy listeners that emit to the bus |
| [`orm/listeners.py`](../src/hotframe/orm/listeners.py) | `PgNotifyBridge` — PostgreSQL LISTEN/NOTIFY → AsyncEventBus bridge |
| [`orm/transactions.py`](../src/hotframe/orm/transactions.py) | `atomic()`, `on_commit()` — transactional management with savepoints |

---

## `orm/events.py` — The ORM → EventBus bridge

### `setup_orm_events(bus, base=None)`

```python
def setup_orm_events(bus: Any, base: type[DeclarativeBase] | None = None) -> None:
```

This is the central function of the entire folder. `create_app()` calls it once during bootstrap, passing the `AsyncEventBus` singleton.

**`base` parameter**: if a `DeclarativeBase` class is provided, listeners are registered only on the mappers of that base. If `None` (the default), registration happens on `Mapper` directly, capturing **all** mapped models in the process.

The function registers five SQLAlchemy listeners via `@event.listens_for(target, "event", propagate=True)`:

| SA event | Internal listener | What it emits |
|---|---|---|
| `before_insert` | `_before_insert` | `ModelPreSaveEvent(created=True)` + `"model.pre_save"` |
| `after_insert` | `_after_insert` | `ModelPostSaveEvent(created=True)` + `"model.post_save"` + `"{tablename}.created"` |
| `before_update` | `_before_update` | `ModelPreSaveEvent(created=False)` + `"model.pre_save"` |
| `after_update` | `_after_update` | `ModelPostSaveEvent(created=False)` + `"model.post_save"` + `"{tablename}.updated"` |
| `after_delete` | `_after_delete` | `ModelPreDeleteEvent` + `ModelPostDeleteEvent` + `"model.post_delete"` + `"{tablename}.deleted"` |

### Internal helper functions

#### `_emit_async(bus, event_name, **kwargs)`

SQLAlchemy listeners are **synchronous functions** (SA calls them without `await`). To emit to the async `AsyncEventBus`, this function checks whether an event loop is running:

```python
def _emit_async(bus: Any, event_name: str, **kwargs: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop: Alembic migrations, CLI, sync tests
        logger.debug("No event loop — skipping event emission: %s", event_name)
        return

    loop.create_task(bus.emit(event_name, **kwargs))
```

If there is no loop (during Alembic migrations, in the CLI, in synchronous tests), the emission is silently skipped. If there is a loop, it schedules an asyncio task. This means emission is **fire-and-forget**: the SQLAlchemy listener returns immediately and bus handlers run at some future tick of the event loop.

#### `_emit_typed_async(bus, event)`

Same pattern but for Pydantic events: calls `loop.create_task(bus.emit_typed(event))`.

#### `_get_tablename(instance)`, `_get_hub_id(instance)`, `_get_instance_id(instance)`

Helpers that extract information from the model:
- `_get_tablename`: uses `sa_inspect(type(instance)).local_table.name` to get the table name (not the Python class name).
- `_get_hub_id`: `getattr(instance, "hub_id", None)` — supports models with `HubMixin`.
- `_get_instance_id`: `getattr(instance, "id", None)`.

### Listener details

#### `_before_insert` / `_before_update`

In addition to emitting events, these listeners perform **automatic mutations**:

- `_before_insert`: if the model has `created_at` and it is `None`, sets it to `datetime.now(UTC)`. If it has `hub_id` set to `None`, attempts to pull it from `session.info.get("hub_id")`.
- `_before_update`: if the model has `updated_at`, updates it to `datetime.now(UTC)`.

This is the mechanism by which `TimestampMixin` works without the developer having to update `updated_at` manually.

#### `_after_insert` — the `"{tablename}.created"` events

```python
# Excerpt from _after_insert
if tablename:
    _emit_async(
        bus,
        f"{tablename}.created",
        sender=type(instance),
        instance=instance,
        hub_id=hub_id,
    )
```

This is the origin of the high-level events mentioned in GUIDE.md §11: `"models.User.created"` is actually emitted as `"users.created"` (the tablename, not the class name). For example, if you have `User.__tablename__ = "users"`, creating a user causes the bus to emit:
- `"model.pre_save"` (typed: `ModelPreSaveEvent`)
- `"model.post_save"` (typed: `ModelPostSaveEvent`)
- `"users.created"` (legacy, with the full `instance`)

Subscribing to `"users.created"` gives your legacy handler direct access to the `instance` object.

#### `_after_delete` — an important quirk

SQLAlchemy has no `before_delete` event at the mapper level (it exists at the `Session` level, not the `Mapper` level). Therefore `_after_delete` emits **both** `ModelPreDeleteEvent` and `ModelPostDeleteEvent` from the same listener, consecutively. Both carry the same data, and the emission order is: pre → post within the same `loop.create_task`.

### How to subscribe to ORM events

**Legacy interface** (receives the full object):

```python
# Subscribe to saves on any model
await bus.subscribe("model.post_save", on_any_save)

# Subscribe only to inserts on the "invoices" table
await bus.subscribe("invoices.created", on_invoice_created)

# Wildcard: any event on the "invoices" table
await bus.subscribe("invoices.*", on_invoice_any)
```

The legacy handler receives `(event=str, sender=<ModelClass>, instance=<instance>, created=bool, hub_id=...)`.

**Typed interface** (strongly-typed handler):

```python
from hotframe.signals.catalog import ModelPostSaveEvent

async def on_post_save(event: ModelPostSaveEvent) -> None:
    if event.model_name == "invoices" and event.created:
        await send_invoice_email(event.instance_id)

await bus.subscribe_typed(ModelPostSaveEvent, on_post_save, module_id="billing")
```

The typed interface does not receive the full `instance` object, only the `instance_id`. If you need the object, use the legacy interface.

---

## `orm/listeners.py` — `PgNotifyBridge`

### Purpose

Allows different instances of the app (or external processes) to communicate via PostgreSQL without an external message broker. Useful for invalidating caches across workers, synchronizing module state in multi-instance deployments, or receiving notifications from external processes (migration scripts, Celery workers).

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

**Optional dependency**: requires `asyncpg`. If it is not installed, `start()` raises `ImportError` with installation instructions. The import is guarded with `try/except` to avoid breaking startup when the bridge is not in use.

#### `start(dsn, bus, channels)`

Opens a dedicated asyncpg connection (separate from the SQLAlchemy pool) and registers `_handle` as a listener on each channel.

```python
bridge = PgNotifyBridge()
await bridge.start(
    dsn="postgresql://user:pass@localhost/myapp",
    bus=bus,
    channels=["module_sync", "cache_invalidate"],
)
```

#### `_handle(connection, pid, channel, payload)`

The callback that asyncpg invokes when a NOTIFY arrives. It parses the payload as JSON. If the payload is not valid JSON, it wraps it as `{"raw": payload}`. It then emits `f"pg.{channel}"` on the bus:

```python
loop.create_task(self._bus.emit(f"pg.{channel}", sender=self, **data))
```

So if someone executes `NOTIFY module_sync, '{"module_id": "loyalty"}'` in PostgreSQL, the bus emits `"pg.module_sync"` with `module_id="loyalty"` as a kwarg.

#### `notify(session, channel, payload)` (static method)

Sends a NOTIFY from Python code using the active SQLAlchemy session:

```python
await PgNotifyBridge.notify(
    session,
    "module_sync",
    {"module_id": "loyalty", "action": "activated"},
)
```

Internally executes `SELECT pg_notify(:channel, :payload)`. Validates that the payload does not exceed 8000 bytes (PostgreSQL's limit):

```python
if len(json_payload.encode("utf-8")) > 8000:
    raise ValueError(
        f"NOTIFY payload exceeds PostgreSQL 8000-byte limit ..."
    )
```

#### `stop()`

Removes the asyncpg listeners and cleanly closes the connection. Must be called in the app's `lifespan` shutdown handler.

#### Compatibility alias

```python
setup_pg_notify = PgNotifyBridge
```

For legacy import paths.

---

## `orm/transactions.py` — `atomic` and `on_commit`

### `atomic(session)` — transactional context with savepoints

```python
@asynccontextmanager
async def atomic(session: ISession):
```

An async context manager that automatically detects whether a transaction is already active:

- **No active transaction** (`not session.in_transaction()`): opens with `async with session.begin()`. On clean exit, commits. On exception, rolls back.
- **Active transaction** (`session.in_transaction()`): opens with `async with session.begin_nested()`, which in async SQLAlchemy creates a `SAVEPOINT`. On exception, rolls back to the savepoint. The outer transaction is unaffected.

```python
async with atomic(session):
    session.add(invoice)

    async with atomic(session):  # → SAVEPOINT
        session.add(line_item)
        # If this fails, only the line_item is rolled back
```

After the **outermost transaction** commits, it fires the `on_commit` callbacks registered for that session:

```python
# After the outer commit:
callbacks = _commit_callbacks.pop(sid, [])
for cb in callbacks:
    result = cb()
    if hasattr(result, "__await__"):
        await result
```

Callbacks are only fired on the outermost block's commit. If the block uses a savepoint (nested), callbacks accumulate and execute when the outer block finally commits.

### `on_commit(session, callback)`

```python
def on_commit(session: ISession, callback: Callable[[], Any | Awaitable[Any]]) -> None:
```

Registers a callable (sync or async) to execute after commit. Callbacks are indexed by `id(session)` and stored in the module-level dict `_commit_callbacks`.

```python
async with atomic(session):
    await session.execute(...)
    on_commit(session, lambda: send_payment_confirmation(order_id))
    on_commit(session, lambda: invalidate_dashboard_cache(user_id))
```

If the transaction rolls back, `_commit_callbacks.pop(sid)` is never called inside the `atomic` block and the callbacks are discarded. The `pop` only occurs in the implicit `else` branch of `begin()` (when there is no exception). If an exception occurs, `begin()` rolls back and the `pop` is never reached.

### Storage detail

```python
_commit_callbacks: dict[int, list[Callable[[], Any | Awaitable[Any]]]] = {}
```

Module-level dict. The key is `id(session)`. There is no explicit thread-safety because in an asyncio context each session lives in a coroutine, and access to this dict is sequential by nature of the event loop.

---

## How this fits into the rest of the framework

```
create_app(settings)
    │
    ├─ creates AsyncEventBus (singleton)
    │
    ├─ calls setup_orm_events(bus)
    │       │
    │       └─ @event.listens_for(Mapper, "before_insert", ...)
    │          @event.listens_for(Mapper, "after_insert", ...)
    │          @event.listens_for(Mapper, "before_update", ...)
    │          @event.listens_for(Mapper, "after_update", ...)
    │          @event.listens_for(Mapper, "after_delete", ...)
    │
    │  From this point on, any ORM operation anywhere in the code
    │  automatically emits events to the bus.
    │
    └─ (optional) PgNotifyBridge.start(dsn, bus, channels=[...])
            └─ listens on PG channels and emits "pg.<channel>" to the bus

Modules that want to react to ORM events:
    async def on_user_created(event="users.created", sender, instance, hub_id):
        await send_welcome_email(instance.email)

    await bus.subscribe("users.created", on_user_created, module_id="onboarding")
```

The separation of concerns is clean:
- `orm/events.py` knows nothing about the modules listening; it only emits.
- `signals/dispatcher.py` knows nothing about SQLAlchemy; it only dispatches.
- Business modules only know the name of the event they care about.

For transactions:
- `orm/transactions.py` operates on any `ISession` (the protocol defined in `db/protocols.py`).
- Modules use `atomic` and `on_commit` without depending on SQLAlchemy directly.

---

## Gotchas and design decisions

### 1. ORM events are fire-and-forget

SQLAlchemy listeners are synchronous. They use `loop.create_task(bus.emit(...))`, which means bus handlers run **after** the listener returns, at some future tick of the event loop. The INSERT/UPDATE/DELETE does not wait for handlers to finish.

Practical implication: you cannot abort an INSERT from a `"users.created"` handler. If you need to validate beforehand, use a `"model.pre_save"` handler or a `HookRegistry` hook called before the flush.

### 2. `_before_insert` mutates the object directly

The `_before_insert` listener assigns `created_at`, `updated_at`, and `hub_id` to the in-memory object **before** SQLAlchemy generates the SQL. This is correct because `before_*` listeners run right before the SQL statement. However, if the object already has `created_at` set (not `None`), it is not overwritten.

### 3. SQLAlchemy has no `before_delete` on Mapper

The `before_delete` event exists on `Session` but not on `Mapper`. That is why `_after_delete` emits both `ModelPreDeleteEvent` and `ModelPostDeleteEvent`. Both carry the same data. If your use case requires acting **before** the DELETE occurs, use `@event.listens_for(Session, "before_bulk_delete")` or validate in the repository before calling `session.delete(instance)`.

### 4. `on_commit` does not work outside `atomic`

`on_commit` registers callbacks that are fired from the `atomic` block. If you commit directly with `await session.commit()` without going through `atomic`, the `on_commit` callbacks are never executed. The `_commit_callbacks` dict would accumulate orphaned entries.

### 5. `on_commit` with savepoints

If you have nested `atomic` blocks, `on_commit` accumulates callbacks at all levels but only fires them when the outermost block closes. If the outer block rolls back, all callbacks are lost. If the inner savepoint rolls back but the outer block commits, the callbacks from the inner block **also execute** (because they accumulate under the same `sid` regardless of nesting depth).

### 6. `PgNotifyBridge` uses an independent connection

asyncpg does not use the SQLAlchemy pool. The bridge connection is persistent and dedicated to LISTEN. This is intentional: PostgreSQL LISTEN/NOTIFY requires a connection that stays open; it is incompatible with the normal connection-pool pattern where connections are returned to the pool between queries.

### 7. The `instance` in legacy handlers may be in a "detached" state

In `after_*` events, the `instance` object has already been processed by SQLAlchemy, but the session may have expired its attributes. Accessing lazy relationships in an async handler can raise `DetachedInstanceError`. Use `selectinload` or `joinedload` in the original query, or reload the object from the bus handler using a new session.

### 8. Typed ORM events do not carry the full object

`ModelPostSaveEvent` has `instance_id` (the object's `id`) but not the full object. This is intentional: serializing a SQLAlchemy model into a Pydantic event would require knowing its schema, which would create coupling between `orm/` and the app's models. If you need the object, use the legacy event interface (which does carry `instance`) or reload it from `instance_id` in the handler.
