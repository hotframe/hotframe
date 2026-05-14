# 4. Modelos y queryset (models/)

> Propósito: definir la jerarquía de clases SQLAlchemy de las que heredan todos los modelos del framework, y proporcionar un query builder encadenable (`HubQuery`) que encapsula el scoping multi-tenant y el borrado lógico.

---

## Para qué sirve esta carpeta

`models/` es la capa de persistencia declarativa de hotframe. Hace tres cosas:

1. **Define las clases base** (`Base`, `Model`, `TimeStampedModel`, `ActiveModel`) de las que heredan todos los modelos de la aplicación.
2. **Ofrece mixins componibles** para añadir columnas de auditoría, timestamps, borrado lógico y scoping multi-tenant sin duplicar código.
3. **Implementa `HubQuery[T]`**, un query builder encadenable que filtra automáticamente por `hub_id` y excluye registros borrados lógicamente.

Todo el código que ves en la guía de estudio (`Todo.where(...).all()`, `Note.create(...)`, `Note.all()`) pasa por estas piezas.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`models/__init__.py`](../src/hotframe/models/__init__.py) | Re-exportaciones del paquete |
| [`models/base.py`](../src/hotframe/models/base.py) | Clases base declarativas: `Base`, `Model`, `TimeStampedModel`, `ActiveModel` |
| [`models/mixins.py`](../src/hotframe/models/mixins.py) | Mixins componibles: `HubMixin`, `TimestampMixin`, `AuditMixin`, `SoftDeleteMixin` |
| [`models/queryset.py`](../src/hotframe/models/queryset.py) | `HubQuery[T]`: query builder encadenable y asíncrono |

---

## models/base.py — Las clases base declarativas

### `Base`

```python
class Base(DeclarativeBase):
    """Root declarative base for all models."""
    pass
```

Es el `DeclarativeBase` de SQLAlchemy 2.0 del que hereda todo. No añade columnas. Existe para que todos los modelos del proyecto compartan la misma metadata y, por tanto, las mismas migraciones Alembic.

### `Model`

```python
class Model(Base):
    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
```

**Esta es la base recomendada para la mayoría de los modelos.** Aporta:

- `id`: UUID generado en Python (`uuid.uuid4`) como clave primaria. Ojo: la generación ocurre en Python, no en el servidor de base de datos. Esto permite conocer el ID antes de hacer el `flush`.
- `created_at` / `updated_at`: timestamps con zona horaria. `updated_at` usa `onupdate=func.now()` para actualizarse en cada UPDATE sin intervención del código de aplicación.

`HubBaseModel` es un alias de compatibilidad hacia atrás que apunta a `Model`.

### `TimeStampedModel`

```python
class TimeStampedModel(Base):
    __abstract__ = True
    id: Mapped[uuid.UUID] = ...
    created_at: Mapped[datetime] = ...
    updated_at: Mapped[datetime] = ...
```

Funcionalmente idéntica a `Model`. Existe como alias semántico para modelos donde el nombre `TimeStampedModel` resulta más expresivo. Es la que aparece en los ejemplos de la guía (`class User(TimeStampedModel)`).

### `ActiveModel`

```python
class ActiveModel(Base):
    __abstract__ = True
    id: Mapped[uuid.UUID] = ...
    created_at: Mapped[datetime] = ...
    updated_at: Mapped[datetime] = ...
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
```

Igual que `Model` pero añade `is_active`. Útil para entidades que se pueden desactivar (usuarios, productos) sin borrarlas. `get_current_user` de `auth/current_user.py` busca explícitamente `UserModel.is_active.is_(True)`.

---

## models/mixins.py — Mixins componibles

Los mixins permiten construir modelos a la carta cuando no se quiere heredar de una clase base con todo incluido. Usan `@declared_attr` de SQLAlchemy para que las columnas se declaren correctamente en cada subclase concreta.

### `HubMixin`

```python
class HubMixin:
    @declared_attr
    def hub_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(Uuid, nullable=False, index=True)
```

Añade `hub_id`: el identificador del tenant. Al ser `index=True`, las consultas de scoping (`WHERE hub_id = ?`) son eficientes. `HubQuery` detecta la presencia de este atributo en tiempo de ejecución (`hasattr(model, "hub_id")`) y aplica el filtro automáticamente.

### `TimestampMixin`

```python
class TimestampMixin:
    @declared_attr
    def created_at(cls) -> Mapped[datetime]: ...
    @declared_attr
    def updated_at(cls) -> Mapped[datetime]: ...
```

Aporta `created_at` y `updated_at` como columna suelta, para componer con cualquier otra base. Misma lógica que en `Model`.

### `AuditMixin`

```python
class AuditMixin:
    @declared_attr
    def created_by(cls) -> Mapped[uuid.UUID | None]: ...
    @declared_attr
    def updated_by(cls) -> Mapped[uuid.UUID | None]: ...
```

Traza quién creó y modificó cada registro. Ambas columnas son `nullable=True` porque un registro puede crearse por procesos de sistema (sin usuario). El código de la aplicación es responsable de rellenar estos campos — el framework no los gestiona automáticamente.

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

Habilita borrado lógico. `is_deleted` tiene índice porque `HubQuery` lo usa en cada consulta. `deleted_at` registra cuándo se marcó el registro como borrado. `HubQuery.delete()` rellena ambos automáticamente.

---

## models/queryset.py — `HubQuery[T]`

`HubQuery` es el motor de consultas de hotframe. Es un query builder genérico encadenable, asíncrono, y scope-aware. La sintaxis que ves en la guía —`Todo.where(user_id=self.user_id).all()`— es una convención del modelo que llama internamente a `HubQuery`.

### Firma del constructor

```python
class HubQuery[T]:
    def __init__(self, model: type[T], session: ISession, hub_id: UUID) -> None:
```

- `model`: la clase declarativa de SQLAlchemy.
- `session`: una `ISession` (la abstracción del protocolo, en la práctica siempre `AsyncSession`).
- `hub_id`: el UUID del tenant activo. Todo query va filtrado por él si el modelo tiene `hub_id`.

### Estado interno

| Atributo | Tipo | Propósito |
|---|---|---|
| `_conditions` | `list[Any]` | Cláusulas WHERE acumuladas |
| `_order` | `list[Any]` | Columnas ORDER BY |
| `_load_options` | `list[Any]` | Opciones de carga (e.g. `selectinload`) |
| `_limit` | `int | None` | LIMIT |
| `_offset` | `int | None` | OFFSET |
| `_include_deleted` | `bool` | Si incluir registros soft-deleted |

### Métodos de construcción (retornan `Self` para encadenar)

```python
def filter(self, *conditions: Any) -> Self      # WHERE adicional
def order_by(self, *columns: Any) -> Self       # ORDER BY
def options(self, *opts: Any) -> Self            # selectinload, joinedload, etc.
def limit(self, n: int) -> Self                  # LIMIT
def offset(self, n: int) -> Self                 # OFFSET
def with_deleted(self) -> Self                   # incluir is_deleted=True
```

### Métodos terminales (ejecutan la consulta)

```python
async def all(self) -> list[T]              # todos los resultados
async def first(self) -> T | None           # primero o None
async def get(self, id: UUID) -> T | None   # por clave primaria
async def count(self) -> int                # COUNT sin cargar filas
async def sum(self, column: Any) -> Decimal # SUM de una columna
async def exists(self) -> bool              # True si hay al menos una fila
```

### `get_or_create(**defaults) -> tuple[T, bool]`

```python
async def get_or_create(self, **defaults: Any) -> tuple[T, bool]:
```

Devuelve `(instancia, created)`. Si la instancia no existe (según los filtros activos), la crea con `hub_id` + `defaults`. Gestiona la race condition entre SELECT e INSERT: si una `IntegrityError` revela que otro proceso ganó la carrera, hace rollback y re-consulta.

### `delete(id) / hard_delete(id) -> bool`

```python
async def delete(self, id: UUID) -> bool       # borrado lógico
async def hard_delete(self, id: UUID) -> bool  # borrado físico
```

`delete` rellena `is_deleted=True` y `deleted_at=now(UTC)` si el modelo tiene `SoftDeleteMixin`. Si no, hace un hard delete real y emite un `WARNING` en el log para recordarte añadir el mixin.

`hard_delete` borra físicamente, incluso si el registro ya estaba marcado como borrado lógicamente.

### Filtrado automático interno: `_base_query()`

```python
def _base_query(self) -> Select[tuple[T]]:
    stmt = select(self._model)
    if hasattr(self._model, "hub_id"):
        stmt = stmt.where(self._model.hub_id == self._hub_id)
    if not self._include_deleted and hasattr(self._model, "is_deleted"):
        stmt = stmt.where(self._model.is_deleted == False)
    # ... condiciones, options, order, limit, offset
```

Este método es llamado por todos los terminales. Los dos filtros automáticos (hub_id y soft-delete) se inyectan aquí sin que el código de aplicación los conozca.

---

## Cómo encaja con el resto del framework

- **`repository/base.py`** usa `HubQuery` directamente: `BaseRepository.q()` instancia un `HubQuery` y los métodos CRUD de alto nivel (`list`, `create`, `update`, `delete`) lo usan.
- **`auth/current_user.py`** usa `select(UserModel).where(UserModel.is_active.is_(True))` directamente con SQLAlchemy, sin pasar por `HubQuery` — porque el modelo de usuario no necesariamente tiene `hub_id`.
- **`db/protocols.py`** define `ISession` como protocolo que `HubQuery` acepta como tipo de sesión. Esto desacopla el query builder de `AsyncSession`.
- **`LiveComponent`** en la guía usa `await Todo.where(...).all()` — eso es una API de conveniencia sobre el modelo que internamente crea un `HubQuery`.

---

## Gotchas y decisiones de diseño

**IDs UUID en Python, no en la DB.** `default=uuid.uuid4` significa que el ID se genera en Python en el momento de crear el objeto, antes del `flush`. Esto permite referencias cruzadas entre objetos nuevos sin necesitar un `flush` intermedio. La contrapartida es que el UUID no usa `gen_random_uuid()` del servidor.

**`onupdate=func.now()` en `updated_at`.** SQLAlchemy aplica este valor en cada UPDATE que pase por el ORM. Si haces un UPDATE raw con SQL no lo verás actualizado.

**`is_deleted` como booleano + `deleted_at` como timestamp.** El booleano tiene índice (consultas rápidas), el timestamp da trazabilidad temporal. Ambos son necesarios.

**Los mixins usan `@declared_attr`.** Sin `@declared_attr`, SQLAlchemy no mapearía las columnas correctamente en las subclases concretas. Esto es un requisito de la API de SQLAlchemy 2.0 para mixins.

**`HubQuery` es inmutable en cada llamada de encadenamiento.** Devuelve `Self`, pero modifica `self._conditions` in-place. Esto significa que reutilizar una instancia de `HubQuery` en dos ramas puede causar contaminación. Crea siempre un `HubQuery` nuevo por consulta (a través de `BaseRepository.q()` o el modelo).

**`_filtered_select` vs `_base_query`.** `HubQuery` tiene dos constructores internos de queries: `_base_query` (para `SELECT model`) y `_filtered_select` (para `SELECT func.count(...)`, `SELECT func.sum(...)`). Esta separación existe para evitar subqueries innecesarias en las agregaciones.