# 4. Models and querysets (`models/`)

> Purpose: define the SQLAlchemy class hierarchy that all framework models inherit from, and provide a chainable query builder (`HubQuery`) that encapsulates multi-tenant scoping and soft deletion.

---

## What this folder is for

`models/` is the declarative persistence layer of hotframe. It does three things:

1. **Defines the base classes** (`Base`, `Model`, `TimeStampedModel`, `ActiveModel`) that all application models inherit from.
2. **Provides composable mixins** for adding audit columns, timestamps, soft deletion, and multi-tenant scoping without duplicating code.
3. **Implements `HubQuery[T]`**, a chainable async query builder that automatically filters by `hub_id` and excludes soft-deleted records.

All the code you see in the study guide (`Todo.where(...).all()`, `Note.create(...)`, `Note.all()`) flows through these pieces.

---

## File map

| File | Responsibility |
|---|---|
| [`models/__init__.py`](../src/hotframe/models/__init__.py) | Package re-exports |
| [`models/base.py`](../src/hotframe/models/base.py) | Declarative base classes: `Base`, `Model`, `TimeStampedModel`, `ActiveModel` |
| [`models/mixins.py`](../src/hotframe/models/mixins.py) | Composable mixins: `HubMixin`, `TimestampMixin`, `AuditMixin`, `SoftDeleteMixin` |
| [`models/queryset.py`](../src/hotframe/models/queryset.py) | `HubQuery[T]`: chainable async query builder |

---

## `models/base.py` — The declarative base classes

### `Base`

```python
class Base(DeclarativeBase):
    """Root declarative base for all models."""
    pass
```

This is the SQLAlchemy 2.0 `DeclarativeBase` that everything inherits from. It adds no columns. Its purpose is to ensure all models in the project share the same metadata and therefore the same Alembic migrations.

### `Model`

```python
class Model(Base):
    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
```

**This is the recommended base for most models.** It provides:

- `id`: a UUID generated in Python (`uuid.uuid4`) as the primary key. Note: generation happens in Python, not on the database server. This means the ID is known before the `flush`.
- `created_at` / `updated_at`: timezone-aware timestamps. `updated_at` uses `onupdate=func.now()` so it updates on every UPDATE without any intervention from application code.

`HubBaseModel` is a backwards-compatibility alias that points to `Model`.

### `TimeStampedModel`

```python
class TimeStampedModel(Base):
    __abstract__ = True
    id: Mapped[uuid.UUID] = ...
    created_at: Mapped[datetime] = ...
    updated_at: Mapped[datetime] = ...
```

Functionally identical to `Model`. It exists as a semantic alias for models where the name `TimeStampedModel` is more expressive. This is the class used in guide examples (`class User(TimeStampedModel)`).

### `ActiveModel`

```python
class ActiveModel(Base):
    __abstract__ = True
    id: Mapped[uuid.UUID] = ...
    created_at: Mapped[datetime] = ...
    updated_at: Mapped[datetime] = ...
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
```

Same as `Model` but adds `is_active`. Useful for entities that can be deactivated (users, products) without being deleted. The `get_current_user` function in `auth/current_user.py` explicitly filters for `UserModel.is_active.is_(True)`.

---

## `models/mixins.py` — Composable mixins

Mixins allow you to build models à la carte when you do not want to inherit from an all-inclusive base class. They use SQLAlchemy's `@declared_attr` so that columns are correctly declared on each concrete subclass.

### `HubMixin`

```python
class HubMixin:
    @declared_attr
    def hub_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(Uuid, nullable=False, index=True)
```

Adds `hub_id`: the tenant identifier. Because `index=True`, scoping queries (`WHERE hub_id = ?`) are efficient. `HubQuery` detects the presence of this attribute at runtime (`hasattr(model, "hub_id")`) and applies the filter automatically.

### `TimestampMixin`

```python
class TimestampMixin:
    @declared_attr
    def created_at(cls) -> Mapped[datetime]: ...
    @declared_attr
    def updated_at(cls) -> Mapped[datetime]: ...
```

Provides `created_at` and `updated_at` as standalone columns, composable with any other base. Same logic as in `Model`.

### `AuditMixin`

```python
class AuditMixin:
    @declared_attr
    def created_by(cls) -> Mapped[uuid.UUID | None]: ...
    @declared_attr
    def updated_by(cls) -> Mapped[uuid.UUID | None]: ...
```

Tracks who created and last modified each record. Both columns are `nullable=True` because a record may be created by a system process (with no user). Application code is responsible for populating these fields — the framework does not manage them automatically.

### `SoftDeleteMixin`

```python
class SoftDeleteMixin:
    @declared_attr
    def is_deleted(cls) -> Mapped[bool]:
        return mapped_column(Boolean, default=False, server_default="false", index=True)

    @declared_attr
    def deleted_at(cls) -> Mapped[datetime | None]:
        return mapped_column(DateTime(timezone=True), nullable=True)
```

Enables soft deletion. `is_deleted` is indexed because `HubQuery` uses it in every query. `deleted_at` records when the record was marked as deleted. `HubQuery.delete()` populates both fields automatically.

---

## `models/queryset.py` — `HubQuery[T]`

`HubQuery` is the hotframe query engine. It is a generic, chainable, async, scope-aware query builder. The syntax you see in the guide — `Todo.where(user_id=self.user_id).all()` — is a model convention that internally calls `HubQuery`.

### Constructor signature

```python
class HubQuery[T]:
    def __init__(self, model: type[T], session: ISession, hub_id: UUID) -> None:
```

- `model`: the SQLAlchemy declarative class.
- `session`: an `ISession` (the protocol abstraction; in practice always `AsyncSession`).
- `hub_id`: the UUID of the active tenant. Every query is filtered by it when the model has `hub_id`.

### Internal state

| Attribute | Type | Purpose |
|---|---|---|
| `_conditions` | `list[Any]` | Accumulated WHERE clauses |
| `_order` | `list[Any]` | ORDER BY columns |
| `_load_options` | `list[Any]` | Load options (e.g. `selectinload`) |
| `_limit` | `int | None` | LIMIT |
| `_offset` | `int | None` | OFFSET |
| `_include_deleted` | `bool` | Whether to include soft-deleted records |

### Builder methods (return `Self` for chaining)

```python
def filter(self, *conditions: Any) -> Self      # additional WHERE
def order_by(self, *columns: Any) -> Self       # ORDER BY
def options(self, *opts: Any) -> Self            # selectinload, joinedload, etc.
def limit(self, n: int) -> Self                  # LIMIT
def offset(self, n: int) -> Self                 # OFFSET
def with_deleted(self) -> Self                   # include is_deleted=True records
```

### Terminal methods (execute the query)

```python
async def all(self) -> list[T]              # all results
async def first(self) -> T | None           # first result or None
async def get(self, id: UUID) -> T | None   # lookup by primary key
async def count(self) -> int                # COUNT without loading rows
async def sum(self, column: Any) -> Decimal # SUM of a column
async def exists(self) -> bool              # True if at least one row exists
```

### `get_or_create(**defaults) -> tuple[T, bool]`

```python
async def get_or_create(self, **defaults: Any) -> tuple[T, bool]:
```

Returns `(instance, created)`. If the instance does not exist (according to the active filters), it creates one with `hub_id` + `defaults`. Handles the race condition between SELECT and INSERT: if an `IntegrityError` signals that another process won the race, it rolls back and re-queries.

### `delete(id) / hard_delete(id) -> bool`

```python
async def delete(self, id: UUID) -> bool       # soft delete
async def hard_delete(self, id: UUID) -> bool  # physical delete
```

`delete` sets `is_deleted=True` and `deleted_at=now(UTC)` if the model has `SoftDeleteMixin`. If not, it performs a real hard delete and logs a `WARNING` to remind you to add the mixin.

`hard_delete` deletes physically, even if the record was already soft-deleted.

### Internal automatic filtering: `_base_query()`

```python
def _base_query(self) -> Select[tuple[T]]:
    stmt = select(self._model)
    if hasattr(self._model, "hub_id"):
        stmt = stmt.where(self._model.hub_id == self._hub_id)
    if not self._include_deleted and hasattr(self._model, "is_deleted"):
        stmt = stmt.where(self._model.is_deleted == False)
    # ... conditions, options, order, limit, offset
```

This method is called by all terminal methods. The two automatic filters (hub_id and soft-delete) are injected here, transparently to application code.

---

## How this fits into the rest of the framework

- **`repository/base.py`** uses `HubQuery` directly: `BaseRepository.q()` instantiates a `HubQuery` and the high-level CRUD methods (`list`, `create`, `update`, `delete`) use it.
- **`auth/current_user.py`** uses `select(UserModel).where(UserModel.is_active.is_(True))` directly via SQLAlchemy, bypassing `HubQuery` — because the user model does not necessarily have `hub_id`.
- **`db/protocols.py`** defines `ISession` as the protocol that `HubQuery` accepts as the session type. This decouples the query builder from `AsyncSession`.
- **`LiveComponent`** in the guide uses `await Todo.where(...).all()` — that is a convenience API on the model that internally creates a `HubQuery`.

---

## Gotchas and design decisions

**UUIDs are generated in Python, not in the DB.** `default=uuid.uuid4` means the ID is generated in Python when the object is created, before the `flush`. This allows cross-references between new objects without needing an intermediate `flush`. The trade-off is that the UUID does not use the server's `gen_random_uuid()`.

**`onupdate=func.now()` on `updated_at`.** SQLAlchemy applies this value on every UPDATE that goes through the ORM. Raw SQL UPDATEs will not trigger it.

**`is_deleted` as a boolean + `deleted_at` as a timestamp.** The boolean is indexed (fast queries); the timestamp provides temporal traceability. Both are necessary.

**Mixins use `@declared_attr`.** Without `@declared_attr`, SQLAlchemy would not map the columns correctly on concrete subclasses. This is a requirement of the SQLAlchemy 2.0 mixin API.

**`HubQuery` is mutated in place on each chaining call.** It returns `Self`, but modifies `self._conditions` in place. This means reusing a single `HubQuery` instance across two code branches can cause contamination. Always create a new `HubQuery` per query (via `BaseRepository.q()` or the model).

**`_filtered_select` vs `_base_query`.** `HubQuery` has two internal query constructors: `_base_query` (for `SELECT model`) and `_filtered_select` (for `SELECT func.count(...)`, `SELECT func.sum(...)`). This split exists to avoid unnecessary subqueries in aggregations.
