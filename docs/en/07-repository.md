# 7. The generic repository (repository/)

> Purpose: provide a high-level, typed CRUD layer on top of `HubQuery`, with full-text search, pagination, serialization, and automatic scoping by `hub_id`.

---

## What this folder is for

`repository/` implements the Repository pattern on top of `HubQuery`. Where `HubQuery` is a general-purpose query builder that operates at the SQL-condition level, `BaseRepository[T]` is a semantic CRUD interface: `list`, `get`, `create`, `update`, `delete`. You do not write queries — you instantiate the repository with the model and the session.

The module also exposes `serialize` and `serialize_list` to convert ORM instances to clean dicts (UUIDs as `str`, datetimes as ISO-8601, Decimals as `str`) suitable for JSON responses in REST routes.

---

## File map

| File | Responsibility |
|---|---|
| [`repository/__init__.py`](../src/hotframe/repository/__init__.py) | Package re-exports |
| [`repository/base.py`](../src/hotframe/repository/base.py) | `BaseRepository[T]`, `serialize`, `serialize_list` |

---

## repository/base.py — `BaseRepository[T]`

### General structure

```python
from hotframe import BaseRepository

class UserRepo(BaseRepository[User]):
    pass

# Usage
repo = UserRepo(User, db, hub_id, search_fields=["email", "name"])
result = await repo.list(search="john", limit=20, is_active=True)
user   = await repo.get(some_uuid)
new    = await repo.create(email="j@example.com", name="John")
upd    = await repo.update(some_uuid, name="Johnny")
ok     = await repo.delete(some_uuid)
```

### Constructor

```python
def __init__(
    self,
    model: type[T],
    db: ISession,
    hub_id: UUID,
    *,
    search_fields: list[str] | None = None,
    default_order: str = "created_at",
) -> None:
```

| Parameter | Type | Description |
|---|---|---|
| `model` | `type[T]` | SQLAlchemy declarative class for the managed model |
| `db` | `ISession` | Async database session |
| `hub_id` | `UUID` | Active tenant; injected into `hub_id` on create and into all filters |
| `search_fields` | `list[str] | None` | Text columns searched by `list(search=...)` |
| `default_order` | `str` | Default sort column (defaults to `"created_at"`) |

### `q() -> HubQuery[T]`

```python
def q(self) -> HubQuery[T]:
    return HubQuery(self.model, self.db, self.hub_id)
```

Escape hatch to `HubQuery` when you need a query that the high-level methods do not cover. All `BaseRepository` methods call `self.q()` internally.

### `list(...) -> dict[str, Any]`

```python
async def list(
    self,
    *,
    search: str | None = None,
    order_by: str | Any | None = None,
    limit: int = 50,
    offset: int = 0,
    options: Sequence[Any] | None = None,
    **filters: Any,
) -> dict[str, Any]:
```

Returns `{"items": list[T], "total": int}`. The `total` is the count without pagination (useful for frontend pagination components).

Internal logic:

1. If `search` is provided and `search_fields` is set, generates `OR(col.ilike("%search%"))` for each field.
2. For each `**filters` kwarg, adds `WHERE model.field == value` if the attribute exists on the model and the value is not `None`.
3. Computes `total` with `query.count()` (before applying offset/limit).
4. Applies `order_by`: if it is a `str`, resolves `getattr(model, order_by, None)`; if it is a SQLAlchemy expression, uses it directly.
5. Applies `offset` and `limit` and executes.

Example usage in a route:

```python
@router.get("/products")
async def list_products(db: DbSession, hub_id: UUID, q: str | None = None):
    repo = BaseRepository(Product, db, hub_id, search_fields=["name", "sku"])
    return await repo.list(search=q, limit=30, is_active=True)
```

### `get(id, *, options) -> T | None`

```python
async def get(self, id: UUID, *, options: Sequence[Any] | None = None) -> T | None:
```

Retrieves a record by primary key. Automatically scoped to `hub_id`: if the ID belongs to a different tenant, returns `None`. The `options` parameter accepts `selectinload` or `joinedload` for eager relationship loading.

### `create(**kwargs) -> T`

```python
async def create(self, **kwargs: Any) -> T:
    instance = self.model(hub_id=self.hub_id, **kwargs)
    self.db.add(instance)
    await self.db.flush()
    return instance
```

Creates and inserts a new row. Injects `hub_id` automatically — you do not need to pass it in `kwargs`. The `flush` makes the generated ID (Python UUID) visible without committing: the transaction remains open so that the `get_db` dependency can commit on close.

### `update(id, **kwargs) -> T | None`

```python
async def update(self, id: UUID, **kwargs: Any) -> T | None:
    instance = await self.get(id)
    if instance is None:
        return None
    for key, value in kwargs.items():
        if hasattr(instance, key):
            setattr(instance, key, value)
    await self.db.flush()
    return instance
```

Loads the instance (with scoping), applies changes field by field, and flushes. If the ID does not exist or belongs to a different tenant, returns `None`. Keys that do not exist on the model are silently ignored (`if hasattr`).

### `delete(id) -> bool` and `hard_delete(id) -> bool`

```python
async def delete(self, id: UUID) -> bool:
    return await self.q().delete(id)

async def hard_delete(self, id: UUID) -> bool:
    return await self.q().hard_delete(id)
```

Delegate directly to `HubQuery`. `delete` performs a soft delete if the model has `SoftDeleteMixin`. `hard_delete` removes the row physically. Both return `True` if the record was found.

### `count(**filters) -> int` and `exists(**filters) -> bool`

```python
async def count(self, **filters: Any) -> int:
async def exists(self, **filters: Any) -> bool:
```

Same kwarg filter logic as `list`, but without pagination or search. Useful for quick validations:

```python
already_exists = await repo.exists(email="j@example.com")
total_active   = await repo.count(is_active=True)
```

---

## `serialize` and `serialize_list`

```python
def serialize(
    obj: Any,
    *,
    fields: list[str] | None = None,
    exclude: set[str] | None = None,
) -> dict[str, Any]:
```

Converts an ORM instance to a JSON-serializable `dict`. Automatic conversions:

| Python type | Serialized as |
|---|---|
| `uuid.UUID` | `str` |
| `Decimal` | `str` |
| `datetime` | ISO-8601 (`isoformat()`) |
| `date` | ISO-8601 (`isoformat()`) |

If `fields` is `None`, it uses `obj.__table__.columns` to infer the columns. The `exclude` parameter drops sensitive columns (passwords, hashes).

```python
def serialize_list(
    items: list[Any],
    *,
    fields: list[str] | None = None,
    exclude: set[str] | None = None,
) -> list[dict[str, Any]]:
```

Applies `serialize` to each element of the list. Useful in REST list routes.

---

## How it fits into the rest of the framework

- **Depends on `models/queryset.py`**: `BaseRepository.q()` instantiates a `HubQuery` and all CRUD methods delegate to it.
- **Depends on `db/protocols.py`**: the `db` parameter is typed as `ISession`. In production this is always SQLAlchemy's `AsyncSession`, but in tests you can pass a fake.
- **Used from FastAPI routes**: together with `DbSession` (the `Annotated` type from `auth/current_user.py`), the typical pattern is to instantiate the repository inside the handler with `BaseRepository(Model, db, hub_id)`.
- **`serialize` is used in REST routes** to serialize before returning the response, especially in AI-integrated tools where the response must be pure JSON.

---

## Gotchas and design decisions

**`BaseRepository` is stateless by design.** It holds no state between calls. Each method creates a fresh `HubQuery`. This makes testing straightforward: instantiate, call, discard.

**`hub_id` is required.** There is no repository without a `hub_id`. This guarantees you can never accidentally access another tenant's data. If your model does not have `hub_id` (system models such as `Module`), you can use `HubQuery` directly with a dummy UUID, provided the model does not have that column.

**`list` always counts.** `list()` always issues two queries: one for `count()` and one for `all()`. If you do not need the total, use `self.q().filter(...).all()` directly. On large tables this can make a significant difference.

**`update` has PATCH semantics.** It only updates the fields you pass. It does not replace the entire object. This is PATCH semantics, not PUT.

**`filters=None` is ignored.** In `list` and `count`, filters whose value is `None` are silently skipped. This lets you pass optional query-string parameters directly without conditional logic:

```python
await repo.list(is_active=query_param_or_none)  # if None, no filter is applied
```
