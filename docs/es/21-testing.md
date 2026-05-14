# 21. Utilidades de testing (testing/)

> `testing/` proporciona las piezas que necesitas para escribir tests de tu aplicación hotframe sin levantar una base de datos real, sin infraestructura externa y sin el stack completo de middleware que solo tiene sentido en producción. El objetivo es que el setup de un test sea tres líneas y que las aserciones sean directas.

---

## Para qué sirve esta carpeta

Probar una aplicación FastAPI+SQLAlchemy tiene fricciones conocidas: crear el engine asíncrono para tests, asegurarse de que las tablas existen antes del test y se limpian después, deshabilitar CSRF y rate limiting para no complicar los requests de test, y disponer de dobles (fakes) para los componentes que tienen efectos secundarios (bus de eventos, hook registry). `testing/` resuelve exactamente esas fricciones con un conjunto pequeño y ortogonal de utilidades.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`testing/__init__.py`](../src/hotframe/testing/__init__.py) | Toda la lógica del subpaquete. Exporta `create_test_app`, `test_db_session`, `create_test_tables`, `drop_test_tables`, `cleanup_test_db`, `FakeEventBus`, `FakeHookRegistry`. |
| [`testing/test_lazy_imports.py`](../src/hotframe/testing/test_lazy_imports.py) | Test de regresión del paquete hotframe: verifica que todos los nombres en `hotframe.__all__` resuelven sin error de importación. |

---

## Estado global del engine de test

```python
_test_engine = None
_test_session_factory = None
```

Dos variables de módulo que actúan como singletons del engine y el factory para toda la sesión de tests. Esto es intencional: crear un engine SQLite en memoria por cada test sería costoso y generaría contención de ficheros. El patrón recomendado es:

- Crear el engine una vez por sesión de pytest (fixture `scope="session"`).
- Usar rollback al final de cada test para aislar el estado.

---

## `create_test_app(settings, **overrides)`

**Firma:**
```python
def create_test_app(settings: Any | None = None, **overrides: Any) -> FastAPI
```

Crea una aplicación FastAPI configurada para testing.

### Defaults de test

Si no se pasa `settings`, aplica automáticamente estos valores:

| Setting | Valor de test | Motivo |
|---|---|---|
| `DATABASE_URL` | `"sqlite+aiosqlite:///:memory:"` | Sin fichero, sin servidor externo |
| `DEBUG` | `True` | Mensajes de error completos |
| `DEPLOYMENT_MODE` | `"local"` | Desactiva comportamientos de producción |
| `SECRET_KEY` | `"test-secret-key-not-for-production"` | Valor fijo para cookies firmadas |
| `CSRF_EXEMPT_PREFIXES` | `["/"]` | Exime **todas** las rutas del CSRF |
| `RATE_LIMIT_API` | `999999` | Elimina el rate limiting |
| `RATE_LIMIT_AUTH` | `999999` | Elimina el rate limiting de auth |
| `LOG_LEVEL` | `"WARNING"` | Silencia logs de INFO/DEBUG en los tests |

Los `**overrides` se fusionan sobre los defaults antes de construir el objeto `HotframeSettings`:

```python
test_defaults.update(overrides)
settings = HotframeSettings(**test_defaults)
```

### Flujo interno

```python
set_settings(settings)          # registra globalmente para que create_app lo use
app = create_app(settings)      # bootstrap completo
return app
```

La función llama al mismo `create_app` que usa la aplicación real, lo que garantiza que los tests ejercitan el mismo stack (middlewares, Jinja2, ComponentRegistry, etc.).

### Ejemplo de uso

```python
# tests/conftest.py
import pytest
from hotframe.testing import create_test_app

@pytest.fixture
async def app():
    return create_test_app()

# Override específico
@pytest.fixture
async def app_with_cors():
    return create_test_app(CORS_ORIGINS=["http://localhost:3000"])
```

---

## `test_db_session()`

**Firma:**
```python
async def test_db_session() -> AsyncGenerator[AsyncSession, None]
```

Generador asíncrono que cede una sesión de SQLAlchemy conectada a SQLite en memoria. Crea las tablas en el primer uso y hace rollback al final de cada sesión.

### Ciclo de vida

```python
# Primera llamada: crea el engine y las tablas
if _test_engine is None:
    _test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# Cada llamada: cede sesión y hace rollback
async with _test_session_factory() as session:
    try:
        yield session
    finally:
        await session.rollback()
```

El rollback en `finally` garantiza que cada test empieza con la base de datos limpia aunque el test anterior fallara a mitad de camino.

### Ejemplo de uso

```python
# tests/conftest.py generado por hf startproject
@pytest.fixture
async def db(app):
    async for session in test_db_session():
        yield session

# Test real
async def test_create_user(db):
    user = User(email="test@example.com")
    db.add(user)
    await db.flush()
    assert user.id is not None
```

**Nota:** el `db` fixture depende de `app` porque `create_test_app()` es quien llama a `set_settings()`, asegurando que el engine se configure antes de que `test_db_session()` trate de usarlo.

---

## `create_test_tables()`

**Firma:** `async def create_test_tables() -> None`

Crea explícitamente todas las tablas de `Base.metadata` en el engine de test. Útil cuando el test no pasa por `test_db_session()` pero necesita que las tablas existan (p.ej. tests de migraciones o tests de fixtures de datos).

```python
async def create_test_tables() -> None:
    global _test_engine
    if _test_engine is None:
        _test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
```

---

## `drop_test_tables()`

**Firma:** `async def drop_test_tables() -> None`

Elimina todas las tablas del engine de test. Útil para teardown de fixtures de sesión o para reiniciar el schema entre grupos de tests.

---

## `cleanup_test_db()`

**Firma:** `async def cleanup_test_db() -> None`

Cierra el engine y resetea los singletons `_test_engine` y `_test_session_factory` a `None`. Pensado para el teardown de fixtures de sesión (`scope="session"`):

```python
@pytest.fixture(scope="session", autouse=True)
async def db_cleanup():
    yield
    await cleanup_test_db()
```

Llama a `engine.dispose()` que cierra todas las conexiones del pool antes de resetear el estado. Después de `cleanup_test_db()`, la próxima llamada a `test_db_session()` creará un engine nuevo (y nuevas tablas).

---

## `FakeEventBus`

**Firma:**
```python
class FakeEventBus:
    events: list[tuple[str, Any]]
    typed_events: list[Any]

    async def emit(self, event_name: str, data: Any = None, **kwargs: Any) -> None
    async def emit_typed(self, event: Any) -> None
    def reset(self) -> None
```

Un bus de eventos en memoria para tests. En lugar de ejecutar los suscriptores reales, **solo registra** los eventos emitidos. Permite aserciones del tipo:

```python
bus = FakeEventBus()

# Inyecta en la app / servicio que quieres testear
await service.create_invoice(bus=bus, ...)

# Aserta lo que se emitió
assert bus.events == [("invoice.created", {"invoice_id": 42})]
assert len(bus.typed_events) == 1
assert isinstance(bus.typed_events[0], InvoiceCreated)
```

### `emit(event_name, data, **kwargs)`

Registra una tupla `(event_name, data)` en `self.events`. Ignora `**kwargs` (que en el bus real se usan para pasar payloads nombrados). Nota: si el código bajo test llama a `bus.emit("invoice.paid", invoice_id=42)` — es decir, sin `data` pero con kwargs — `data` será `None` en la tupla registrada. Los kwargs se pierden en la fake.

### `emit_typed(event)`

Registra el objeto de evento tipado en `self.typed_events`. Permite aserciones sobre el tipo y los atributos del evento.

### `reset()`

Limpia ambas listas. Útil para reutilizar la misma instancia en múltiples tests o aserciones:

```python
bus = FakeEventBus()

await op1(bus=bus)
assert ("event.1", ...) in bus.events
bus.reset()

await op2(bus=bus)
assert ("event.2", ...) in bus.events
```

---

## `FakeHookRegistry`

**Firma:**
```python
class FakeHookRegistry:
    _actions: dict[str, list]
    _filters: dict[str, list]

    async def do_action(self, name: str, *args: Any, **kwargs: Any) -> None
    async def apply_filters(self, name: str, value: Any, *args: Any, **kwargs: Any) -> Any
    def add_action(self, name: str, fn: Any, priority: int = 10) -> None
    def add_filter(self, name: str, fn: Any, priority: int = 10) -> None
```

Un registro de hooks en memoria que **sí ejecuta** las funciones registradas (a diferencia de `FakeEventBus` que solo registra). Esto permite testear código que depende de hooks con handlers de test controlados.

### `add_action(name, fn, priority=10)`

Registra `fn` para el hook `name`. `priority` se acepta para compatibilidad de firma pero no se usa — los handlers se ejecutan en orden de registro.

### `do_action(name, *args, **kwargs)`

Invoca todos los handlers registrados para `name`:

```python
async def do_action(self, name: str, *args: Any, **kwargs: Any) -> None:
    for fn in self._actions.get(name, []):
        await fn(*args, **kwargs)
```

Si no hay handlers para ese nombre, no hace nada (no lanza error).

### `add_filter(name, fn, priority=10)`

Registra `fn` como filtro para `name`.

### `apply_filters(name, value, *args, **kwargs)`

Pasa `value` por todos los filtros registrados, en orden de registro:

```python
async def apply_filters(self, name: str, value: Any, ...) -> Any:
    for fn in self._filters.get(name, []):
        value = await fn(value, *args, **kwargs)
    return value
```

### Ejemplo de uso

```python
hooks = FakeHookRegistry()

# Registra un filtro de test que dobla el subtotal
hooks.add_filter("invoice.subtotal", lambda total, ctx: total * 2)

# Aserta que el sistema aplica el filtro
result = await hooks.apply_filters("invoice.subtotal", 100.0, ctx={})
assert result == 200.0
```

---

## `test_lazy_imports.py` — test de regresión del paquete

**Archivo:** [`testing/test_lazy_imports.py`](../src/hotframe/testing/test_lazy_imports.py)

```python
def test_all_lazy_imports_resolve():
    import hotframe

    for name in hotframe.__all__:
        assert getattr(hotframe, name) is not None  # force the lazy import to resolve
```

Este test tiene una función muy específica: verificar que **ningún nombre declarado en `hotframe.__all__`** produce un `ImportError`, `AttributeError` o devuelve `None` cuando se intenta resolver. El paquete `hotframe` usa un sistema de imports lazy (los nombres del namespace público se definen con descriptores o `__getattr__` para no cargar módulos pesados al hacer `import hotframe`). Este test actúa como red de seguridad: si alguien añade un nombre a `__all__` pero olvida implementar la resolución, el test falla.

**Cuándo puede fallar:**
- Cuando se añade una nueva exportación a `hotframe.__all__` sin implementar el `__getattr__` correspondiente.
- Cuando se renombra un módulo interno sin actualizar el sistema de lazy imports.
- Cuando una dependencia circular impide que el módulo se cargue.

**Por qué está en `testing/`:**
Este test forma parte del propio paquete hotframe (no de una aplicación de usuario). Su presencia en `testing/` indica que los tests del framework interno también viven aquí, junto a las utilidades para aplicaciones de usuario.

---

## Cómo encaja con el resto del framework

| Componente | Relación con `testing/` |
|---|---|
| `hotframe.bootstrap.create_app` | `create_test_app` lo llama directamente — los tests ejercitan el bootstrap real |
| `hotframe.config.settings.HotframeSettings` / `set_settings` | `create_test_app` llama a `set_settings` para que el resto del framework vea los settings de test |
| `hotframe.models.base.Base` | `create_test_tables` y `test_db_session` usan `Base.metadata` para crear/eliminar tablas |
| `hotframe.signals.dispatcher.AsyncEventBus` | `FakeEventBus` tiene la misma interfaz pública (`emit`, `emit_typed`); se inyecta donde el código real esperaría `AsyncEventBus` |
| `hotframe.signals.hooks.HookRegistry` | `FakeHookRegistry` replica la interfaz de `HookRegistry` |
| `management/cli.py` → `startproject` | El `conftest.py` generado por `hf startproject` ya usa `create_test_app` y `test_db_session` — el andamiaje y las utilidades están sincronizados |

---

## Patrón recomendado para `conftest.py`

El `conftest.py` que genera `hf startproject` es el punto de partida:

```python
# tests/conftest.py
import pytest
from hotframe.testing import create_test_app, test_db_session

@pytest.fixture
async def app():
    return create_test_app()

@pytest.fixture
async def db(app):
    async for session in test_db_session():
        yield session
```

Para un proyecto más avanzado, añade fixtures de sesión y limpieza:

```python
import pytest
from hotframe.testing import (
    create_test_app, test_db_session, cleanup_test_db,
    FakeEventBus, FakeHookRegistry,
)

@pytest.fixture(scope="session")
async def app():
    return create_test_app()

@pytest.fixture(scope="session", autouse=True)
async def _cleanup(app):
    yield
    await cleanup_test_db()

@pytest.fixture
async def db(app):
    async for session in test_db_session():
        yield session

@pytest.fixture
def event_bus():
    return FakeEventBus()

@pytest.fixture
def hooks():
    return FakeHookRegistry()
```

---

## Gotchas y decisiones de diseño

**1. `CSRF_EXEMPT_PREFIXES = ["/"]` exime todas las rutas.**
En producción este setting solo exime `/api/`, `/health` y `/static/`. En test se exime todo para no tener que incluir el token CSRF en cada request de test. Si quieres testear específicamente la protección CSRF, pasa `CSRF_EXEMPT_PREFIXES=[]` como override de `create_test_app`.

**2. SQLite en memoria no persiste entre tests.**
`test_db_session()` hace rollback después de cada test, pero el engine SQLite en memoria sí persiste las tablas entre tests de la misma sesión de pytest. Esto es el comportamiento deseado: las tablas se crean una vez y cada test trabaja dentro de una transacción que se descarta.

**3. `FakeEventBus` no ejecuta suscriptores.**
A diferencia de `FakeHookRegistry`, `FakeEventBus` no llama a los handlers registrados — solo acumula los eventos. Si tu código bajo test **depende** de que los suscriptores se ejecuten (p.ej. envío de email disparado por evento), necesitas registrar los suscriptores en el fake manualmente o usar el bus real en una fixture de integración.

**4. `FakeHookRegistry` ignora `priority`.**
El `HookRegistry` real ordena los handlers por prioridad. El fake los ejecuta en orden de registro. Si tienes lógica que depende del orden por prioridad, el fake puede dar falsos positivos en los tests.

**5. El fixture `db` debe depender de `app`.**
`test_db_session()` necesita que `set_settings()` haya sido llamado antes (para saber qué URL de BD usar). `create_test_app()` llama a `set_settings()`. Por eso el fixture `db` debe declarar `app` como dependencia, aunque no lo use directamente. Si `db` se llama sin `app`, el engine de test puede crearse con los settings por defecto de hotframe en lugar de los del proyecto.

**6. `test_lazy_imports.py` como test de contrato.**
Situado en `testing/` pero es un test del propio hotframe, no de aplicaciones usuario. Actúa como test de contrato del namespace público: cualquier nombre en `__all__` debe ser resolvible. Su ubicación en `testing/` (y no en un directorio `tests/` de nivel superior) es una decisión de empaquetado — forma parte del paquete instalable y se puede ejecutar en el entorno del usuario para verificar la integridad de la instalación.
