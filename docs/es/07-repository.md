# 7. El repositorio genérico (repository/)

> Propósito: proporcionar una capa CRUD de alto nivel, tipada, sobre `HubQuery`, con búsqueda de texto libre, paginación, serialización y scoping automático por `hub_id`.

---

## Para qué sirve esta carpeta

`repository/` implementa el patrón Repository sobre `HubQuery`. Donde `HubQuery` es un query builder de propósito general que opera a nivel de condiciones SQL, `BaseRepository[T]` es una interfaz CRUD semántica: `list`, `get`, `create`, `update`, `delete`. No tienes que escribir queries, solo instanciar el repositorio con el modelo y el session.

Además, el módulo expone `serialize` y `serialize_list` para convertir instancias ORM a dicts limpios (UUIDs como `str`, datetimes como ISO-8601, Decimales como `str`) aptos para respuestas JSON en rutas REST.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`repository/__init__.py`](../src/hotframe/repository/__init__.py) | Re-exportaciones del paquete |
| [`repository/base.py`](../src/hotframe/repository/base.py) | `BaseRepository[T]`, `serialize`, `serialize_list` |

---

## repository/base.py — `BaseRepository[T]`

### Estructura general

```python
from hotframe import BaseRepository

class UserRepo(BaseRepository[User]):
    pass

# Uso
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

| Parámetro | Tipo | Descripción |
|---|---|---|
| `model` | `type[T]` | Clase declarativa SQLAlchemy del modelo gestionado |
| `db` | `ISession` | Sesión asíncrona de la base de datos |
| `hub_id` | `UUID` | Tenant activo; se inyecta en `hub_id` al crear y en todos los filtros |
| `search_fields` | `list[str] | None` | Columnas de texto sobre las que busca `list(search=...)` |
| `default_order` | `str` | Columna de ordenación por defecto (por defecto `"created_at"`) |

### `q() -> HubQuery[T]`

```python
def q(self) -> HubQuery[T]:
    return HubQuery(self.model, self.db, self.hub_id)
```

Punto de escape hacia `HubQuery` cuando necesitas una consulta que los métodos de alto nivel no cubren. Todos los métodos de `BaseRepository` llaman a `self.q()` internamente.

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

Retorna `{"items": list[T], "total": int}`. El `total` es el count sin paginación (útil para componentes de paginación en el frontend).

Lógica interna:

1. Si `search` y hay `search_fields`, genera `OR(col.ilike("%search%"))` para cada campo.
2. Para cada `**filters` kwarg, añade `WHERE model.field == value` si el atributo existe en el modelo y el valor no es `None`.
3. Calcula el `total` con `query.count()` (antes de aplicar offset/limit).
4. Aplica `order_by`: si es `str`, resuelve `getattr(model, order_by, None)`; si es una expresión SQLAlchemy, la usa directamente.
5. Aplica `offset` y `limit` y ejecuta.

Ejemplo de uso en una ruta:

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

Obtiene un registro por clave primaria. Scoped a `hub_id` automáticamente: si el ID pertenece a otro tenant, retorna `None`. El parámetro `options` permite pasar `selectinload` o `joinedload` para carga ansiosa de relaciones.

### `create(**kwargs) -> T`

```python
async def create(self, **kwargs: Any) -> T:
    instance = self.model(hub_id=self.hub_id, **kwargs)
    self.db.add(instance)
    await self.db.flush()
    return instance
```

Crea e inserta una nueva fila. Inyecta `hub_id` automáticamente — no tienes que pasarlo en `kwargs`. El `flush` hace visible el ID generado (UUID Python) sin hacer commit: la transacción sigue abierta para que el `get_db` dependency haga el commit al cerrar.

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

Carga la instancia (con scoping), aplica los cambios campo a campo y hace flush. Si el ID no existe o pertenece a otro tenant, retorna `None`. Silencia silenciosamente claves que no existen en el modelo (`if hasattr`).

### `delete(id) -> bool` y `hard_delete(id) -> bool`

```python
async def delete(self, id: UUID) -> bool:
    return await self.q().delete(id)

async def hard_delete(self, id: UUID) -> bool:
    return await self.q().hard_delete(id)
```

Delegan directamente a `HubQuery`. `delete` hace borrado lógico si el modelo tiene `SoftDeleteMixin`. `hard_delete` borra físicamente. Ambos retornan `True` si encontraron el registro.

### `count(**filters) -> int` y `exists(**filters) -> bool`

```python
async def count(self, **filters: Any) -> int:
async def exists(self, **filters: Any) -> bool:
```

Misma lógica de filtros por kwargs que `list`, pero sin paginación ni búsqueda. Útiles para validaciones rápidas:

```python
already_exists = await repo.exists(email="j@example.com")
total_active   = await repo.count(is_active=True)
```

---

## `serialize` y `serialize_list`

```python
def serialize(
    obj: Any,
    *,
    fields: list[str] | None = None,
    exclude: set[str] | None = None,
) -> dict[str, Any]:
```

Convierte una instancia ORM a `dict` JSON-serializable. Conversiones automáticas:

| Tipo Python | Serializado como |
|---|---|
| `uuid.UUID` | `str` |
| `Decimal` | `str` |
| `datetime` | ISO-8601 (`isoformat()`) |
| `date` | ISO-8601 (`isoformat()`) |

Si `fields` es `None`, usa `obj.__table__.columns` para inferir las columnas. El parámetro `exclude` excluye columnas sensibles (contraseñas, hashes).

```python
def serialize_list(
    items: list[Any],
    *,
    fields: list[str] | None = None,
    exclude: set[str] | None = None,
) -> list[dict[str, Any]]:
```

Aplica `serialize` a cada elemento de la lista. Útil en rutas REST de listado.

---

## Cómo encaja con el resto del framework

- **Depende de `models/queryset.py`**: `BaseRepository.q()` instancia un `HubQuery` y todos los métodos CRUD delegan en él.
- **Depende de `db/protocols.py`**: el parámetro `db` es de tipo `ISession`. En producción es siempre `AsyncSession` de SQLAlchemy, pero en tests puedes pasar un fake.
- **Se usa desde rutas FastAPI**: junto con `DbSession` (el Annotated type de `auth/current_user.py`), el patrón típico es instanciar el repositorio dentro del handler con `BaseRepository(Model, db, hub_id)`.
- **`serialize` se usa en rutas REST** para serializar antes de devolver la respuesta, especialmente en tools de IA integradas donde la respuesta debe ser JSON puro.

---

## Gotchas y decisiones de diseño

**`BaseRepository` es stateless por diseño.** No guarda estado entre llamadas. Cada método crea un `HubQuery` fresco. Esto facilita el testing: instancia, llama, descarta.

**`hub_id` obligatorio.** No hay repositorio sin `hub_id`. Esto garantiza que nunca puedes acceder a datos de otro tenant por accidente. Si tu modelo no tiene `hub_id` (modelos de sistema como `Module`), puedes usar `HubQuery` directamente pasando un UUID ficticio y asegurándote de que el modelo no tenga la columna.

**`list` siempre cuenta.** `list()` hace siempre dos queries: uno para `count()` y otro para `all()`. Si no necesitas el total, usa `self.q().filter(...).all()` directamente. En tablas grandes esto puede marcar la diferencia.

**`update` es "patch" semántico.** Solo actualiza los campos que le pases. No hace un reemplazo total del objeto. Es la semántica PATCH, no PUT.

**`filters=None` se ignora.** En `list` y `count`, los filtros con valor `None` se omiten silenciosamente. Esto permite pasar parámetros opcionales de query string directamente sin lógica condicional:

```python
await repo.list(is_active=query_param_or_none)  # si es None, no filtra
```
