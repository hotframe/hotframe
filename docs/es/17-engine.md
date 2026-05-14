# 17. El motor de módulos (`engine/`)

> El *hot-mount module engine* es la pieza que hace posible instalar,
> activar, desactivar, desinstalar y actualizar módulos Python **en
> runtime, sin reiniciar el proceso**. Es la característica diferencial
> de hotframe frente a cualquier framework web Python convencional.

---

## Para qué sirve esta carpeta

En un framework tradicional, añadir un plugin implica reiniciar el
servidor: el código nuevo no existe en `sys.modules`, las rutas no están
registradas en la app y las tablas de base de datos no están creadas.

hotframe resuelve este problema con un motor de orquestación que trata
cada módulo como un artefacto con **estado persistente en BD** y
**estado en memoria** que se pueden manejar de forma independiente. La
BD es la fuente de verdad de qué hay instalado; el proceso Python mantiene
en RAM las rutas montadas, los eventos suscritos, los hooks, los slots y
las clases importadas.

Las operaciones que el motor soporta son:

| Operación   | Qué hace |
|-------------|----------|
| `install`   | Descarga el código, valida, migra la BD, llama `on_install`, monta rutas. |
| `activate`  | Vuelve a montar un módulo desactivado sin re-descargar el código. |
| `deactivate`| Desmonta rutas, limpia `sys.modules`, actualiza el estado en BD. |
| `uninstall` | Desmonta, revierte migraciones, borra la fila de BD. |
| `update`    | Descarga la versión nueva, desmonta la vieja, migra, monta la nueva. |
| `boot`      | Al arrancar el proceso, restaura en memoria todos los módulos activos en BD. |

Todo esto ocurre sin `kill`, sin `SIGTERM`, sin reinicio de uvicorn.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---------|----------------|
| [`__init__.py`](#initpy--api-pública) | Re-exporta la API pública de la subcarpeta. |
| [`models.py`](#modelspy--modelo-de-estado) | Modelo SQLAlchemy `Module` (tabla `hotframe_module`). |
| [`state.py`](#statepy--capa-crud) | `ModuleStateDB` — todas las escrituras/lecturas sobre el estado en BD. |
| [`pipeline.py`](#pipelinepy--máquina-de-estados-con-rollback-lifo) | `HotMountPipeline` — primitiva de fases con rollback LIFO. |
| [`import_manager.py`](#import_managerpy--gestión-precisa-de-sysmodules) | `ImportManager` — importa paquetes, registra submodulos en `sys.modules` y detecta clases zombie. |
| [`loader.py`](#loaderpy--carga-y-descarga-en-fastapi) | `ModuleLoader` — monta y desmonta rutas FastAPI, eventos, hooks, slots, componentes, middleware, locales. |
| [`lifecycle.py`](#lifecyclepy--hooks-del-módulo) | `ModuleLifecycleManager` — llama a los hooks `on_install/activate/deactivate/uninstall/upgrade`. |
| [`dependency.py`](#dependencypy--gestor-de-dependencias) | `DependencyManager` — orden topológico, comprobación de versiones, cascada de desactivaciones. |
| [`s3_source.py`](#s3_sourcepy--descarga-desde-s3) | `S3ModuleSource` — descarga, verifica SHA-256 y cachea módulos desde AWS S3. |
| [`marketplace_client.py`](#marketplace_clientpy--cliente-http-del-marketplace) | `MarketplaceClient` — resuelve y descarga módulos desde cualquier servidor marketplace. |
| [`boundary.py`](#boundarypy--barrera-de-aislamiento-de-errores) | `ModuleBoundaryMiddleware` — captura excepciones de rutas de módulos, evita que tiren el Hub Core. |
| [`module_runtime.py`](#module_runtimepy--el-orquestador-central) | `ModuleRuntime` — orquestador único que coordina todo lo anterior. |

---

## `__init__.py` — API pública

Re-exporta exactamente lo que un desarrollador externo necesita para
trabajar con el engine:

```python
from hotframe.engine import (
    ModuleRuntime,       # orquestador
    ModuleLoader,        # carga/descarga
    ImportManager,       # gestión sys.modules
    ImportedBundle,      # resultado de import
    PurgeReport,         # resultado de purga
    HotMountPipeline,    # primitiva de fases
    PhaseResult,
    PhaseStatus,
    PipelineState,
    RollbackHandle,
    InstallResult,
    ActivateResult,
    DeactivateResult,
    UninstallResult,
    UpdateResult,
)
```

El `__init__.py` no contiene lógica, sólo importaciones. Esto es
deliberado: el motor es testeable pieza a pieza porque cada módulo tiene
dependencias mínimas.

---

## `models.py` — Modelo de estado

Define el modelo SQLAlchemy `Module` que se almacena en la tabla
`hotframe_module`. Es el modelo *por defecto*; los proyectos pueden
sustituirlo con `settings.MODULE_STATE_MODEL` (necesario en entornos
multi-tenant que añaden un campo `hub_id`).

```python
class Module(Base):
    __tablename__ = "hotframe_module"

    id: Mapped[uuid.UUID]           # PK
    module_id: Mapped[str]          # "invoice", "loyalty", etc. — único
    version: Mapped[str]            # "1.4.2"
    status: Mapped[str]             # "installing" | "active" | "disabled" | "error" | "degraded"
    checksum_sha256: Mapped[str]    # SHA-256 del archivo zip
    manifest: Mapped[dict]          # JSON — copia del ModuleManifest en el momento de activar
    config: Mapped[dict]            # JSON — configuración por hub/tenant
    error_message: Mapped[str|None] # mensaje de la última excepción, si hay
    is_system: Mapped[bool]         # True → no se puede desactivar ni desinstalar
    installed_at: Mapped[datetime]
    activated_at: Mapped[datetime|None]
    disabled_at: Mapped[datetime|None]
```

### Ciclo de valores de `status`

```
installing → active → disabled → active ...
             ↓
             error
             ↓
             degraded
```

- `installing` — transición durante `install()`. Si el proceso muere a
  mitad, el módulo queda en este estado; el operador tiene que limpiar
  manualmente o volver a instalar.
- `active` — completamente operativo. `get_active_modules` lo devuelve
  en el boot.
- `disabled` — el usuario lo desactivó; el código sigue en disco pero no
  está montado en memoria.
- `error` — falló install/activate/uninstall; no se puede usar.
- `degraded` — `ModuleBoundaryMiddleware` detectó demasiados errores en
  producción; sigue montado pero el UI avisa.

---

## `state.py` — Capa CRUD

`ModuleStateDB` centraliza todas las operaciones de lectura/escritura
sobre la tabla de estado. Nunca hace lógica de negocio; sólo SQL.

```python
class ModuleStateDB:
    def _model(self) -> type: ...                     # resuelve el modelo desde settings
    async def get_active_modules(session, **filters)  # SELECT WHERE status='active'
    async def get_all_modules(session, **filters)
    async def get_module(session, module_id, **filters) -> Any | None
    async def create(session, module_id, version, *, checksum, status, **extra)
    async def activate(session, module_id, manifest_dict, **filters)
    async def deactivate(session, module_id, **filters)
    async def set_status(session, module_id, status, error, **filters)
    async def set_error(session, module_id, error_message, **filters)
    async def set_degraded(session, module_id, error_message, **filters)
    async def update_manifest(session, module_id, manifest_dict, **filters)
    async def delete(session, module_id, **filters)
```

### `**filters` como multi-tenant

Todos los métodos aceptan `**filters` arbitrarios que se convierten en
cláusulas `WHERE`. En proyectos multi-tenant el filtro habitual es
`hub_id=<uuid>`:

```python
await state.get_active_modules(session, hub_id=hub_id)
await state.activate(session, module_id, manifest, hub_id=hub_id)
```

Esto permite usar `ModuleStateDB` con cualquier modelo personalizado que
tenga `hub_id` u otros campos de partición, sin tocar el código del motor.

### `ModuleAlreadyInstallingError`

La función `create` hace `session.flush()` después de insertar. Si hay
una restricción `UNIQUE` sobre `module_id` (y opcionalmente `hub_id`),
SQLAlchemy lanza `IntegrityError` que `create` captura y re-lanza como
`ModuleAlreadyInstallingError`. El runtime lo usa para detectar
instalaciones concurrentes del mismo módulo.

### `_get_module_model()`

Función de módulo que resuelve la clase ORM configurada. Permite que
toda la capa `state.py` funcione con modelos de proyecto sin cambiar ni
una línea:

```python
def _get_module_model() -> type:
    settings = get_settings()
    if settings.MODULE_STATE_MODEL:
        # "myproject.modules.HubModule" → importa y devuelve la clase
        module_path, class_name = settings.MODULE_STATE_MODEL.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)
    from hotframe.engine.models import Module
    return Module
```

---

## `pipeline.py` — Máquina de estados con rollback LIFO

`HotMountPipeline` es una primitiva de propósito general para ejecutar
una secuencia de pasos donde cada uno puede fallar y necesita deshacer
sólo su propio efecto. Es el núcleo de cómo `ModuleRuntime.install`
garantiza que nunca deja el sistema a medias.

### Conceptos clave

**`RollbackHandle`** — protocol con un único método `async def undo()`.
Cada fase del pipeline produce uno.

```python
@runtime_checkable
class RollbackHandle(Protocol):
    async def undo(self) -> None: ...
```

**`PhaseResult`** — lo que devuelve cada fase al completarse:

```python
@dataclass(slots=True)
class PhaseResult:
    phase_name: str         # "DOWNLOADING", "MIGRATING", etc.
    rollback: RollbackHandle
    payload: dict           # datos que la fase quiere pasar a la siguiente
```

**`PipelineState`** — el estado mutable del pipeline:

```python
@dataclass(slots=True)
class PipelineState:
    module_id: str
    current_phase: str | None
    completed_phases: list[str]
    rollback_stack: list[RollbackHandle]  # ← apilado en orden de ejecución
    status: PhaseStatus                   # PENDING | RUNNING | ACTIVE | ERROR
    error: Exception | None
```

**`HotMountPipeline`** — la clase principal:

```python
class HotMountPipeline:
    PHASES = [
        "INIT", "DOWNLOADING", "EXTRACTING", "VALIDATING",
        "MIGRATING", "IMPORTING", "MOUNTING", "STACK_REBUILD", "ACTIVE",
    ]

    async def run_phase(self, phase_name, fn, *args, **kwargs) -> PhaseResult
    async def commit(self) -> None
    async def rollback(self) -> list[Exception]

    @property
    def state(self) -> PipelineState
```

### Flujo de ejecución

```
pipeline = HotMountPipeline("invoice")

r1 = await pipeline.run_phase("DOWNLOADING", fn_download, ...)
# → rollback_stack = [r1.rollback]

r2 = await pipeline.run_phase("MIGRATING", fn_migrate, ...)
# → rollback_stack = [r1.rollback, r2.rollback]

r3 = await pipeline.run_phase("MOUNTING", fn_mount, ...)
# → rollback_stack = [r1.rollback, r2.rollback, r3.rollback]

# Si r3 falla antes de llegar aquí:
errors = await pipeline.rollback()
# → ejecuta: r3.undo(), r2.undo(), r1.undo()  (LIFO)
```

El rollback es *best-effort*: si `r2.undo()` lanza, se recoge la
excepción y se continúa con `r1.undo()`. Al final se devuelve la lista
de excepciones para que el orquestador las registre.

### Por qué LIFO

El orden inverso garantiza que se deshacen los efectos en el orden
correcto desde el punto de vista de las dependencias: si `MOUNTING`
cargó en memoria código que usa una tabla creada en `MIGRATING`, el
`undo` de `MOUNTING` debe descargar ese código *antes* de que `MIGRATING`
lo borre de la BD.

---

## `import_manager.py` — Gestión precisa de `sys.modules`

`ImportManager` resuelve un problema sutil: cuando Python importa un
paquete, no sólo añade el paquete a `sys.modules`, sino todos sus
submodulos. Si al desactivar un módulo sólo eliminamos la entrada
`"invoice"` de `sys.modules` pero no `"invoice.routes"`,
`"invoice.models"`, etc., hay referencias huérfanas que impiden la
reclamación de memoria y causan errores en la próxima instalación.

### Clases principales

**`ImportedBundle`** — resultado de una importación:

```python
@dataclass(slots=True)
class ImportedBundle:
    module_id: str
    package_name: str
    base_path: Path
    imported_submodules: list[str]    # todo lo que apareció en sys.modules
    exported_classes: list[weakref.ref]  # refs débiles a clases registradas
```

**`PurgeReport`** — resultado de una purga:

```python
@dataclass(slots=True)
class PurgeReport:
    module_id: str
    purged_count: int           # entradas eliminadas de sys.modules
    zombie_classes: list[str]   # clases cuya weakref sobrevivió a gc.collect()
```

**`ImportManager`** — la clase principal:

```python
class ImportManager:
    def import_package(self, module_id, package_name, base_path) -> ImportedBundle
    def register_exported_class(self, module_id, cls: type) -> None
    def purge(self, module_id) -> PurgeReport
    def get_bundle(self, module_id) -> ImportedBundle | None
```

### `import_package` en detalle

```python
def import_package(self, module_id, package_name, base_path):
    with self._lock:
        # 1. Añade base_path.parent a sys.path si no está
        # 2. Snapshot de sys.modules ANTES del import
        before = set(sys.modules.keys())
        # 3. importlib.import_module(package_name)
        # 4. Snapshot DESPUÉS
        after = set(sys.modules.keys())
        new_modules = sorted(after - before)
        # 5. Guarda bundle con la lista exacta de entradas nuevas
        bundle = ImportedBundle(module_id=..., imported_submodules=new_modules)
        self._bundles[module_id] = bundle
```

Si el import falla, hace cleanup de las entradas que ya se añadieron
antes de re-lanzar la excepción. El módulo nunca queda a medias en
`sys.modules`.

### Detección de zombies con `weakref`

Después de purgar `sys.modules` y llamar a `gc.collect()`, si una clase
registrada sigue viva (la `weakref` no es `None`), existe algún cache
externo que la retiene. Las causas más comunes son:

- El registro de mappers de SQLAlchemy.
- El cache interno de Pydantic.
- Suscriptores de señales en el `AsyncEventBus`.

`PurgeReport.zombie_classes` no es un error fatal —es informativo—, pero
si aparece de forma consistente indica que el módulo tiene una fuga y
puede necesitar un reinicio de proceso para limpiar completamente.

### Thread-safety

`ImportManager` protege su `_bundles` con `threading.Lock`. Esto es
necesario porque los `asyncio` event loops pueden correr en threads
distintos en tests o en configuraciones multi-worker.

---

## `loader.py` — Carga y descarga en FastAPI

`ModuleLoader` es el único componente que toca la instancia `FastAPI` y
sus registros: rutas, eventos, hooks, slots, componentes, middleware,
locales. Opera estrictamente a nivel Python/FastAPI; no sabe nada de
S3, BD ni marketplace.

### Constructor

```python
class ModuleLoader:
    def __init__(
        self,
        app: FastAPI,
        registry: ModuleRegistry,
        event_bus: AsyncEventBus,
        hooks: HookRegistry,
        slots: SlotRegistry,
        components: ComponentRegistry | None = None,
        import_manager: ImportManager | None = None,
        stack_manager: MiddlewareStackManager | None = None,
    ) -> None
```

- `components` es opcional para compatibilidad con el CLI (`ModuleRuntime`
  puede instanciarse sin ellos).
- `import_manager` y `stack_manager` se crean internamente si no se
  inyectan, lo que facilita el testing con instancias aisladas.

### `load_module` — los 16 pasos

```python
async def load_module(
    self,
    module_id: str,
    module_path: Path,
    manifest: ModuleManifest,
) -> RegisteredModule
```

Los pasos, en orden:

1. **Import del paquete** via `ImportManager.import_package`. Si ya había
   un bundle registrado (recarga), se purga primero.
2. **Registro de clases ORM exportadas** (`_register_exported_models`):
   inspecciona `{module_id}.models`, registra cada subclase de `Base`
   como `weakref` en `ImportManager` y guarda `(clases, tablas)` en
   `_module_metadata`.
3. **`routes.py`** — importa `{module_id}.routes` y obtiene `router`
   (`APIRouter`).
4. **`api.py`** — importa `{module_id}.api` y obtiene `api_router`.
5. **Montar router HTML** en `/m/{module_id}` (comprueba conflicto de
   ruta antes de añadir).
6. **Montar router API** en `/api/v1/m/{module_id}`.
7. **Eventos**: importa `{module_id}.events`, llama a
   `register_events(bus, module_id)`.
8. **Hooks**: importa `{module_id}.hooks`, llama a
   `register_hooks(hooks, module_id)`.
9. **Slots**: importa `{module_id}.slots`, llama a
   `register_slots(slots, module_id)`.
10. **Servicios**: llama a `register_services(module_id)` del facade de
    servicios.
11. **Middleware**: importa la clase de middleware desde
    `manifest.MIDDLEWARE` (dotted path `"module.ClassName"`).
12. **Locales i18n**: registra el directorio `module_path/locales/`.
13. **Archivos estáticos**: monta `module_path/static/{module_id}/` en
    `/static/m/{module_id}/`.
13b. **Componentes**: descubre, registra y monta routers y statics de
    componentes via `discover_module_components`.
14. **Bust de caché OpenAPI**: `app.openapi_schema = None`.
15. **Registro en `ModuleRegistry`**.
16. **Añadir middleware a la pila Starlette** via `MiddlewareStackManager`.

### Rollback de `load_module`

Si *cualquiera* de los pasos 3–16 falla, el bloque `except` deshace
exactamente lo que se completó, en orden inverso:

- Elimina las rutas montadas de `app.routes`.
- Llama `bus.unsubscribe_module(module_id)`.
- Llama `hooks.remove_module_hooks(module_id)`.
- Llama `slots.unregister_module(module_id)`.
- Desmonta routers y statics de componentes.
- Elimina clientes HTTP del módulo.
- Desregistra locales.
- Elimina el middleware de la pila.
- Elimina el mount de estáticos.
- Llama `_drop_module_metadata(module_id)` para limpiar SQLAlchemy.
- Llama `_purge_module(module_id)` para limpiar `sys.modules`.
- Busta el caché de OpenAPI.

```python
# Extracto del bloque except en load_module:
for mount in mounted_routes:
    try:
        self.app.routes.remove(mount)
    except ValueError:
        pass

if events_registered:
    try:
        await self.bus.unsubscribe_module(module_id)
    except Exception:
        pass
# ... (continúa con hooks, slots, componentes, locales, middleware, metadata, purge)
```

### `unload_module` — los pasos de descarga

```python
async def unload_module(self, module_id: str) -> None
```

1. Elimina rutas `/m/{module_id}` y `/api/v1/m/{module_id}` de
   `app.routes`.
2. `bus.unsubscribe_module(module_id)`.
3. `hooks.remove_module_hooks(module_id)`.
4. `slots.unregister_module(module_id)`.
4b. Desmonta routers y statics de componentes, llama
   `components.unregister_module(module_id)`.
4c. Elimina clientes HTTP del módulo.
5. Desregistra locales.
6. Elimina mount de estáticos.
7. `unregister_module_services(module_id)`.
8. `_drop_module_metadata(module_id)` — elimina tablas de `Base.metadata`
   y dispone mappers de SQLAlchemy.
9. `_purge_module(module_id)` — elimina entradas de `sys.modules` via
   `ImportManager`, llama `gc.collect()`, detecta zombies.
9b. **Segunda pasada de limpieza**: si `_verify_metadata_cleared`
   detecta tablas residuales (porque el orden drop→purge las dejó),
   fuerza `Base.registry._dispose_cls` y `Base.metadata.remove` de forma
   explícita.
8b. Elimina middleware de la pila Starlette y llama `gc.collect()` extra
   para romper ciclos de referencias creados por `BaseHTTPMiddleware`.
9. `registry.unregister(module_id)`.
10. Busta el caché de OpenAPI.
11. `gc.collect()` final.

### `_drop_module_metadata` y `_verify_metadata_cleared`

Estas dos funciones privadas son la respuesta al error más frecuente en
reinstraciones:

```
InvalidRequestError: Table 'invoice_item' is already defined for this MetaData instance.
```

`_drop_module_metadata` usa el diccionario `_module_metadata` que
`load_module` pobló con las clases y tablas del módulo:

```python
def _drop_module_metadata(self, module_id: str) -> None:
    classes, tables = self._module_metadata.pop(module_id, ([], []))
    for tbl in tables:
        Base.metadata.remove(tbl)        # elimina la tabla del MetaData
    for cls in classes:
        mapper = cls.__mapper__
        mapper._dispose()                 # desconecta el mapper
        Base.registry._dispose_cls(cls)  # elimina del registro central
```

`_verify_metadata_cleared` hace una segunda comprobación después de
`_purge_module`: busca en `Base.metadata.tables` cualquier tabla cuyo
módulo propietario coincida con el `module_id`. Si encuentra alguna,
`unload_module` fuerza una limpieza de emergencia y lo registra como
`WARNING`.

### `reload_module`

```python
async def reload_module(self, module_id, module_path, manifest) -> RegisteredModule:
    await self.unload_module(module_id)
    return await self.load_module(module_id, module_path, manifest)
```

Usado por `hot_reload` en modo desarrollo: recarga el código en caliente
preservando el estado en BD.

---

## `lifecycle.py` — Hooks del módulo

`ModuleLifecycleManager` llama a funciones opcionales definidas en
`{module_id}/lifecycle.py`. El módulo no está obligado a tenerlas.

```python
# Hooks válidos (frozenset)
LIFECYCLE_HOOKS = {"on_install", "on_activate", "on_deactivate", "on_uninstall", "on_upgrade"}
```

```python
class ModuleLifecycleManager:
    async def call(
        self,
        module_id: str,
        hook_name: str,
        session: ISession,
        hub_id: UUID,
        **kwargs,       # p.ej. from_version, to_version para on_upgrade
    ) -> None

    async def has_hook(self, module_id: str, hook_name: str) -> bool
```

### Funcionamiento de `call`

1. Valida que `hook_name` esté en `LIFECYCLE_HOOKS`.
2. Intenta `importlib.import_module(f"{module_id}.lifecycle")`.
   Si no existe (`ModuleNotFoundError`), retorna silenciosamente.
3. Obtiene `getattr(lifecycle_mod, hook_name, None)`. Si el módulo existe
   pero no define el hook, registra un debug log y retorna.
4. Si el hook es una corrutina (`iscoroutinefunction`), hace `await`.
   Si es síncrono, lo llama directamente (compatibilidad para hooks simples).
5. Si el hook lanza, hace `logger.exception` y re-lanza para que el
   orquestador decida si abortar.

### Ejemplo de `lifecycle.py` en un módulo

```python
# modules/invoice/lifecycle.py
async def on_install(session, hub_id):
    """Seed initial data."""
    await session.execute("INSERT INTO invoice_settings ...")

async def on_uninstall(session, hub_id):
    """Clean up before tables are dropped."""
    await session.execute("DELETE FROM ...")

async def on_upgrade(session, hub_id, from_version, to_version):
    """Data migration."""
    if from_version < "2.0.0":
        await migrate_invoice_format(session, hub_id)
```

---

## `dependency.py` — Gestor de dependencias

`DependencyManager` maneja tres problemas relacionados con las
dependencias entre módulos:

1. Comprobar que las dependencias de un módulo están instaladas y activas
   antes de instalarlo.
2. Impedir desactivar un módulo mientras otro activo depende de él
   (o desactivarlos en orden si se pide cascada).
3. Ordenar los módulos topológicamente al hacer boot para que cada
   dependencia se cargue antes que quien la necesita.

### Formato de dependencias

```python
# En ModuleManifest.DEPENDENCIES:
DEPENDENCIES = ["customers", "inventory>=2.0.0", "billing==1.5.0"]
```

El patrón `_DEP_PATTERN` extrae `(module_id, op, version)`:

```python
_DEP_PATTERN = re.compile(
    r"^(?P<module_id>[a-z][a-z0-9_]*)"
    r"(?:(?P<op>>=|<=|==|!=|>|<)(?P<version>\d+\.\d+\.\d+))?$"
)
```

### `DependencyCheckResult`

```python
@dataclass
class DependencyCheckResult:
    ok: bool = True
    missing: list[str]          # no están en la BD del hub
    inactive: list[str]         # instaladas pero no activas
    version_mismatch: list[tuple[str, str, str]]  # (dep_id, required, actual)
    auto_installable: list[str]
```

### Métodos principales

**`check_install_deps(session, manifest, **filters)`**

Itera `manifest.DEPENDENCIES`, busca cada `module_id` en la BD y
comprueba status y versión. Devuelve `DependencyCheckResult.ok=True`
sólo si todas las dependencias están activas y sus versiones son
compatibles.

**`check_can_deactivate(session, module_id, **filters)`**

Busca en BD todos los módulos activos cuyo `manifest["dependencies"]`
contiene `module_id`. Si hay alguno, construye el orden de cascada (BFS)
y devuelve `DeactivateCheckResult(can_deactivate=False, dependents=[...])`.

**`check_can_uninstall(session, module_id, **filters)`**

Más restrictivo que deactivate: busca módulos en status
`active|installed|disabled` que dependan de este. Devuelve
`UninstallCheckResult`. El uninstall nunca es en cascada; el usuario
debe desinstalar los dependientes primero.

**`resolve_load_order(modules: list[dict]) -> list[dict]`**

Ordenamiento topológico puro para el boot. Algoritmo:

1. Elimina módulos cuyas dependencias no están en el conjunto disponible.
2. Calcula el in-degree de cada nodo.
3. Kahn's algorithm: cola con nodos de in-degree=0, procesa en orden.
4. Si quedan nodos sin procesar al final, hay un ciclo; se registra como
   error y se excluyen.

**`deactivate_cascade(session, module_id, runtime, **filters)`**

Cuando el usuario confirma la desactivación en cascada, llama a
`runtime.deactivate(session, hub_id, mid, cascade=False)` para cada
módulo dependiente en el orden calculado por `_build_cascade_order`.

---

## `s3_source.py` — Descarga desde S3

`S3ModuleSource` gestiona la descarga, verificación y caché de módulos
almacenados en AWS S3. Es la fuente de código en entornos de producción
donde los módulos se distribuyen como artefactos en un bucket S3.

### Convención de claves S3

```
cloud/modules/{module_id}/v{version}.zip
```

La clave se construye con `build_module_object_key(module_id, version)`.
El módulo nunca almacena la URL completa en BD; la reconstruye bajo
demanda.

### Constructor

```python
class S3ModuleSource:
    def __init__(
        self,
        bucket: str,
        cache_dir: Path,   # /tmp/modules/ en ECS
        region: str | None = None,
    )
```

Requiere `aioboto3` (instalado con `pip install aioboto3`). Lanza
`ImportError` si no está disponible, evitando fallos silenciosos.

### API principal

**`download(module_id, version, expected_sha256=""") -> Path`**

1. Construye la clave S3.
2. Comprueba si el directorio local de caché existe Y el ETag coincide
   con el almacenado. Si hay hit de caché → devuelve la ruta local
   sin descargar.
3. Descarga los bytes del objeto S3 con retry exponencial (3 intentos,
   delays de 1, 2, 4 segundos).
4. Verifica SHA-256 con `hashlib.sha256`. Lanza `IntegrityError` si no
   coincide.
5. Extrae el archivo (ZIP o tar.gz) a `cache_dir/{module_id}/`.
6. Guarda el ETag en memoria y en disco (`.{module_id}.etag`).

**`download_many(modules: list[tuple[str,str,str]]) -> dict[str, Path]`**

Descarga paralela via `asyncio.gather`. Los fallos individuales se
loguean y excluyen del resultado; no abortan el resto.

**`load_cached_etags()`**

Al arrancar el proceso, lee todos los `.{module_id}.etag` del disco para
restaurar el caché de ETags. Sin esto, un contenedor caliente re-descarga
todos los módulos en cada arranque.

**`clear_cache(module_id=None)`**

Elimina el directorio de caché y el ETag. Si `module_id=None`, limpia
todo.

### Extracción segura

`_extract` filtra entradas con rutas absolutas o con `..` para evitar
path traversal attacks:

```python
if info.filename.startswith("/") or ".." in info.filename:
    logger.warning("Skipping unsafe zip member: %s", info.filename)
    continue
```

También detecta y elimina el prefijo común de directorios en ZIPs
(p.ej. `assistant/module.py` → `module.py`).

---

## `marketplace_client.py` — Cliente HTTP del marketplace

`MarketplaceClient` implementa el protocolo HTTP para resolver y descargar
módulos desde cualquier servidor que implemente el endpoint:

```
GET {base_url}/{module_id}/resolve/
GET {base_url}/{module_id}/resolve/?version=2.4.7

Response:
{
    "module_id": "sales",
    "version": "2.4.7",
    "download_url": "https://cdn.example.com/modules/sales/v2.4.7.zip",
    "checksum_sha256": "abc123...",
    "dependencies": ["customers>=2.0.0", "inventory"],
    "size_bytes": 204800
}
```

### `ModuleDownloadInfo`

```python
@dataclass
class ModuleDownloadInfo:
    module_id: str
    version: str
    download_url: str
    checksum_sha256: str = ""
    dependencies: list[str] = field(default_factory=list)
    size_bytes: int = 0
```

### `MarketplaceClient`

```python
class MarketplaceClient:
    def __init__(self, base_url: str, timeout: float = 60.0)

    async def resolve(self, module_id, version=None) -> ModuleDownloadInfo
    async def download(self, download_url, dest_dir, checksum="") -> Path
    async def resolve_all_dependencies(
        self, module_id, version=None, *, already_installed=None
    ) -> list[ModuleDownloadInfo]
    @staticmethod
    def _extract_zip(zip_path, dest_dir) -> Path
```

**`resolve`** — hace `GET {base_url}/{module_id}/resolve/`, lanza
`MarketplaceError` para 404 y otros errores HTTP.

**`download`** — descarga el ZIP a un archivo temporal, verifica el
checksum SHA-256, extrae con `_extract_zip`, devuelve la ruta al
directorio del módulo.

**`resolve_all_dependencies`** — BFS sobre el grafo de dependencias,
visitando el módulo raíz y todos sus transitivos. Al final ordena
topológicamente (dependencias primero). Maneja ciclos con un log de
warning y continúa.

**`_extract_zip`** — extrae el ZIP a `dest_dir`. Detecta path traversal
con rutas absolutas o `..`. Busca `module.py` en la raíz o un nivel
abajo. Deriva el nombre del módulo del directorio encontrado (elimina
sufijos de versión con `-`).

---

## `boundary.py` — Barrera de aislamiento de errores

`ModuleBoundaryMiddleware` es un `BaseHTTPMiddleware` de Starlette que
actúa como cortafuegos entre el código de un módulo y el Hub Core.

### El problema que resuelve

Sin este middleware, una excepción no capturada en una ruta de módulo
(`/m/invoice/orders`) puede:

1. Llegar al manejador de errores global de FastAPI/Starlette.
2. Exponer un traceback en la respuesta.
3. En el peor caso, dejar la app en un estado inconsistente.

Y si el módulo tiene un bug sistemático (p.ej. falla en toda petición),
no hay ningún mecanismo para detectarlo y notificar al operador.

### Alcance

El middleware intercepta **solo** rutas de módulos:

```python
_MODULE_URL = re.compile(r"^/(?:api/v1/)?m/([a-z0-9_-]+)(?:/|$)")
```

- `/m/{module_id}/...` — rutas HTML.
- `/api/v1/m/{module_id}/...` — rutas API.

Rutas del Hub Core (`/health`, `/dashboard`, `/ws/_live`, etc.) pasan
sin tocar.

### `_ModuleErrorTracker`

Por cada módulo, mantiene una ventana deslizante de errores:

```python
@dataclass
class _ModuleErrorTracker:
    threshold: int = 10
    window_seconds: float = 60.0
    errors: deque[float] = field(default_factory=lambda: deque(maxlen=50))

    def record(self) -> bool:
        """Añade timestamp; devuelve True si se alcanzó el umbral."""
        now = time.monotonic()
        self.errors.append(now)
        cutoff = now - self.window_seconds
        while self.errors and self.errors[0] < cutoff:
            self.errors.popleft()
        return len(self.errors) >= self.threshold

    def reset(self) -> None:
        self.errors.clear()
```

Usa `time.monotonic()` (no `time.time()`) para ser inmune a saltos del
reloj del sistema (ajustes NTP). La `deque` está acotada a 50 elementos
para que la memoria sea O(threshold) por módulo.

### Flujo de `dispatch`

```python
async def dispatch(self, request, call_next):
    module_id = self._extract_module_id(request.url.path)
    if module_id is None:
        return await call_next(request)  # no es ruta de módulo → pass-through

    try:
        return await call_next(request)
    except Exception as exc:
        logger.exception(...)
        await self._handle_error(request, module_id, exc)
        return self._render_error(request, module_id, exc)
```

### `_handle_error`

1. Registra el error en `_ModuleErrorTracker.record()`.
2. Emite `module.error` en el `AsyncEventBus` (si está disponible en
   `app.state.event_bus`).
3. Si se alcanza el umbral, llama `_mark_degraded` y emite
   `module.degraded`.

### `_mark_degraded`

Intenta persistir `status='degraded'` en la BD:
1. Primero usa `request.state.session` (la sesión de la petición,
   si hay middleware de BD).
2. Si no, abre una sesión transitoria con `get_session_factory()`.

Ambas ramas son best-effort; si fallan, el error se loguea pero la
respuesta contenida sigue llegando al cliente.

### `_render_error`

Devuelve una respuesta contenida sin usar Jinja2 (evita doble fallo si
el template engine está roto). Si es ruta API o el cliente acepta JSON:

```json
{
    "error": "module_unavailable",
    "module_id": "invoice",
    "detail": "Module 'invoice' raised an unhandled exception...",
    "error_type": "RuntimeError"
}
```

Para rutas HTML: un `<html>` mínimo hardcoded con un mensaje de error.

### API pública

```python
class ModuleBoundaryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, threshold=10, window_seconds=60.0)
    def reset_tracker(self, module_id: str) -> None
    async def dispatch(self, request, call_next) -> Response
```

`reset_tracker` es llamado por el endpoint de reactivación del
marketplace cuando el usuario decide darle una segunda oportunidad al
módulo.

---

## `module_runtime.py` — El orquestador central

`ModuleRuntime` es el corazón del motor. Reúne todos los subsistemas en
un único punto de entrada que tanto los endpoints REST como las vistas
HTML de la marketplace UI pueden usar.

### Constructor

```python
class ModuleRuntime:
    def __init__(
        self,
        app: FastAPI,
        settings: HotframeSettings,
        event_bus: AsyncEventBus,
        hooks: HookRegistry,
        slots: SlotRegistry,
        components: ComponentRegistry | None = None,
    ) -> None:
        self.registry = ModuleRegistry()
        self.loader = ModuleLoader(app, registry, event_bus, hooks, slots, components)
        self.state = ModuleStateDB()
        self.s3 = S3ModuleSource(...) if settings.MODULE_SOURCE == "s3" else None
        self.deps = DependencyManager()
        self.lifecycle = ModuleLifecycleManager()
        self.migrations = ModuleMigrationRunner()
        self.watcher = ModuleWatcher()
```

### Resultado dataclasses

Cada operación devuelve un dataclass específico:

```python
@dataclass
class InstallResult:
    success: bool
    module_id: str
    version: str
    error: str | None
    auto_installed: list[str]

@dataclass
class ActivateResult:
    success: bool; module_id: str; error: str | None

@dataclass
class DeactivateResult:
    success: bool; module_id: str; error: str | None
    dependents: list[str]       # módulos que bloquean la desactivación
    cascade_order: list[str]    # orden propuesto si se confirma cascada
    cascaded: list[str]         # módulos que se desactivaron en cascada

@dataclass
class UninstallResult:
    success: bool; module_id: str; error: str | None
    dependents: list[tuple[str, str]]   # (module_id, status) que bloquean

@dataclass
class UpdateResult:
    success: bool; module_id: str
    from_version: str; to_version: str; error: str | None
```

### `boot` y `boot_all_active_modules`

**`boot(session, hub_id, skip_db_writes=False)`**

Secuencia de arranque para un hub:

1. Restaura ETags del disco (si S3 configurado).
2. Consulta `ModuleStateDB.get_active_modules(session, hub_id=hub_id)`.
3. Para módulos sin versión, intenta resolverla desde el catálogo.
4. Llama `_ensure_module_code` para garantizar que el código está en
   disco (descargando de S3 si es necesario).
5. Ordena topológicamente con `DependencyManager.resolve_load_order`.
6. Carga cada módulo con `_load_from_path`.
7. En modo `DEBUG`, arranca `ModuleWatcher` para hot-reload automático.

El parámetro `skip_db_writes` es una optimización para entornos
multi-worker: el worker que no obtuvo el advisory lock de Postgres todavía
necesita montar las rutas en *su* proceso FastAPI, pero no debe escribir
a la BD para evitar deadlocks.

**`boot_all_active_modules(session) -> int`**

Detecta automáticamente si el modelo tiene `hub_id` (multi-tenant) y en
ese caso itera todos los hubs con módulos activos. Usa un advisory lock
de Postgres por hub para serializar los DB writes en entornos
multi-worker.

```python
# Advisory lock: BLAKE2b del hub_id → signed int64
key = _hub_id_to_advisory_key(str(hub_id))
result = await session.execute(
    text("SELECT pg_try_advisory_xact_lock(:key)"),
    {"key": key},
)
acquired = bool(result.first()[0])
```

El hash BLAKE2b en lugar de `hash()` es deliberado: `hash()` está
salteado por proceso en Python 3.3+ y daría claves distintas en cada
worker.

### `install` — pipeline de fases

```python
async def install(
    self,
    session: ISession,
    hub_id: UUID | None,
    module_id: str,
    version: str | None = None,
    checksum: str = "",
    source: str | None = None,
    auto_install_deps: bool = False,
    installed_by: UUID | None = None,
) -> InstallResult
```

Internamente usa un `HotMountPipeline` con 6 fases:

| Fase | Método privado | Efecto | Rollback |
|------|---------------|--------|----------|
| `DOWNLOADING` | `_phase_download` | Descarga el código a `MODULES_DIR` | `shutil.rmtree(target_path)` |
| `VALIDATING` | `_phase_validate` | Valida manifest; renombra si `MODULE_ID` ≠ clave catálogo | Log (la BD ya no se deshace) |
| `VALIDATING` | `_phase_check_deps` | Comprueba dependencias en BD | No-op |
| `MIGRATING` | `_phase_migrate` | Crea fila en BD (`status='installing'`); corre Alembic `upgrade` | Alembic `downgrade`; borra fila BD |
| `IMPORTING` | `_phase_on_install` | Llama `lifecycle.on_install` | No-op (ver nota) |
| `MOUNTING` | `_phase_mount` | `loader.load_module`; refresca templates Jinja2 | `loader.unload_module` |
| `STACK_REBUILD` | `_phase_activate` | `lifecycle.on_activate`; `state.activate` (DB a `active`) | No-op (MIGRATING lo deshace) |

La resolución de la fuente de descarga en `_phase_download` sigue este
orden:
1. `source` es una URL → `MarketplaceClient.download`.
2. `source` es un `.zip` local → `MarketplaceClient._extract_zip`.
3. El módulo ya existe en `MODULES_DIR` → no-op.
4. `MODULE_MARKETPLACE_URL` configurado → `MarketplaceClient.resolve +
   download`.
5. `S3ModuleSource` configurado → `S3ModuleSource.download`.
6. Si ninguno aplica → `RuntimeError`.

### `activate` — re-activar un módulo desactivado

```python
async def activate(self, session, hub_id, module_id) -> ActivateResult
```

No usa pipeline (es una operación más lineal). Pasos:

1. Verifica que el módulo esté en estado `disabled|installed|error`.
2. Garantiza que el código está en disco (lo descarga de S3 si falta).
3. Valida el manifest.
4. Comprueba dependencias.
5. `loader.load_module`.
6. Refresca templates.
7. `lifecycle.on_activate`.
8. `state.activate` (DB a `active`).
9. Emite `module.activated`.

Si algo falla, intenta `loader.unload_module` si el módulo ya se había
cargado, y llama `state.set_error`.

### `deactivate`

```python
async def deactivate(self, session, hub_id, module_id, cascade=False) -> DeactivateResult
```

1. Verifica que el módulo esté `active`.
2. Rechaza módulos `is_system`.
3. Comprueba dependientes activos con `DependencyManager.check_can_deactivate`.
4. Si `cascade=False` y hay dependientes → devuelve error con la lista
   (el UI puede mostrar el diálogo de confirmación).
5. Si `cascade=True` → `DependencyManager.deactivate_cascade` (desactiva
   en orden inverso).
6. `lifecycle.on_deactivate`.
7. `loader.unload_module`.
8. `state.deactivate` (DB a `disabled`).
9. Emite `module.deactivated`.

### `uninstall`

```python
async def uninstall(self, session, hub_id, module_id) -> UninstallResult
```

Más destructivo que deactivate; nunca se hace en cascada:

1. Rechaza `is_system`.
2. Comprueba que **ningún** módulo (cualquier status) dependa de este.
3. Si estaba activo: `lifecycle.on_deactivate` + `loader.unload_module`.
4. `lifecycle.on_uninstall` (si falla → aborta para prevenir pérdida de datos).
5. Revierte migraciones Alembic si las hay.
6. `state.delete` (borra la fila de BD).
7. `S3ModuleSource.clear_cache` si S3 configurado.
8. Refresca templates.
9. Emite `module.uninstalled`.

### `update`

```python
async def update(self, session, hub_id, module_id, new_version, checksum="", source=None) -> UpdateResult
```

Proceso de actualización con rollback parcial:

1. Descarga la nueva versión (misma lógica de resolución que `install`).
2. Valida el nuevo manifest.
3. Si el módulo estaba activo: `lifecycle.on_deactivate` + `loader.unload_module`.
4. Corre Alembic `upgrade` con el nuevo código.
5. `lifecycle.on_upgrade(from_version=..., to_version=...)`.
6. `loader.load_module` con la nueva versión.
7. Si estaba activo: `lifecycle.on_activate`.
8. `state.activate` + actualiza `version` y `checksum_sha256` en BD.
9. Emite `module.updated`.

Si el paso 6 (`load_module`) falla, intenta recargar la versión antigua:

```python
# Rollback parcial en el except:
if was_active and not self.registry.is_loaded(module_id):
    old_path = Path(self.settings.MODULES_DIR) / module_id
    if old_path.exists():
        old_manifest = load_manifest(old_path)
        await self.loader.load_module(module_id, old_path, old_manifest)
        logger.warning("Update rollback: reloaded previous version of %s", module_id)
```

### `hot_reload` (modo desarrollo)

```python
async def hot_reload(self, module_id: str) -> bool
```

Recarga el código Python sin tocar la BD ni S3. Solo para `DEBUG=True`:

1. Valida que el módulo está cargado.
2. Recarga el manifest del disco.
3. Comprueba que las dependencias siguen cargadas.
4. `loader.reload_module` (unload + load).

### Eventos emitidos

| Evento | Cuándo |
|--------|--------|
| `module.installed` | Al completar `install` con éxito |
| `module.activated` | Al completar `activate` |
| `module.deactivated` | Al completar `deactivate` |
| `module.uninstalled` | Al completar `uninstall` |
| `module.updated` | Al completar `update` |
| `module.error` | En cada excepción capturada por `ModuleBoundaryMiddleware` |
| `module.degraded` | Cuando el tracker de `ModuleBoundaryMiddleware` cruza el umbral |

---

## El ciclo de vida completo de un módulo

### Diagrama de estados

```
No instalado
     │
     │ install()
     ▼
  installing ──────────── error (fallo en cualquier fase)
     │                       │
     │ (pipeline completa)   │ activate() (si se corrige el problema)
     ▼                       │
   active ◄──────────────────┘
     │  ▲
     │  │ activate()
     ▼  │
  disabled
     │
     │ uninstall()
     ▼
No instalado

active ──── muchas peticiones fallidas ──► degraded
```

### Flujo `install` paso a paso

```
Usuario / CLI / Marketplace UI
    │
    │ ModuleRuntime.install(session, hub_id, "invoice", version="2.0.0")
    │
    ├─ HotMountPipeline("invoice")
    │
    ├─ DOWNLOADING (_phase_download)
    │   ├─ URL directa → MarketplaceClient.download()
    │   ├─ .zip local → MarketplaceClient._extract_zip()
    │   ├─ ya en disco → no-op
    │   ├─ MODULE_MARKETPLACE_URL → MarketplaceClient.resolve + download
    │   └─ S3 → S3ModuleSource.download()
    │       └─ → /app/modules/invoice/
    │
    ├─ VALIDATING (_phase_validate)
    │   ├─ load_manifest(module_path) → ModuleManifest
    │   └─ si MODULE_ID ≠ clave → rename dir + update HubModuleVersion
    │
    ├─ VALIDATING (_phase_check_deps)
    │   └─ DependencyManager.check_install_deps()
    │       ├─ missing → RuntimeError
    │       ├─ version_mismatch → RuntimeError
    │       └─ inactive + auto_install_deps=False → RuntimeError
    │
    ├─ MIGRATING (_phase_migrate)
    │   ├─ ModuleStateDB.create(status='installing')
    │   └─ ModuleMigrationRunner.upgrade() → Alembic upgrade head
    │
    ├─ IMPORTING (_phase_on_install)
    │   └─ ModuleLifecycleManager.call("on_install") → invoice/lifecycle.py
    │
    ├─ MOUNTING (_phase_mount)
    │   ├─ ModuleLoader.load_module()
    │   │   ├─ ImportManager.import_package("invoice")
    │   │   ├─ Monta /m/invoice/ + /api/v1/m/invoice/
    │   │   ├─ Registra eventos, hooks, slots, servicios, componentes
    │   │   └─ Añade middleware a Starlette stack
    │   └─ _refresh_templates() → Jinja2 rescanning
    │
    ├─ STACK_REBUILD (_phase_activate)
    │   ├─ ModuleLifecycleManager.call("on_activate")
    │   └─ ModuleStateDB.activate(status='active', manifest=...)
    │
    ├─ pipeline.commit()
    │
    └─ bus.emit("module.installed")
```

### Flujo `deactivate` paso a paso

```
ModuleRuntime.deactivate(session, hub_id, "invoice")
    │
    ├─ ModuleStateDB.get_module() → verifica status=='active'
    │
    ├─ DependencyManager.check_can_deactivate()
    │   └─ si hay dependientes activos Y cascade=False → return error
    │
    ├─ [cascade=True] DependencyManager.deactivate_cascade()
    │   └─ desactiva en LIFO: billing → invoice
    │
    ├─ ModuleLifecycleManager.call("on_deactivate")
    │
    ├─ ModuleLoader.unload_module()
    │   ├─ Elimina rutas de app.routes
    │   ├─ bus.unsubscribe_module("invoice")
    │   ├─ hooks.remove_module_hooks("invoice")
    │   ├─ slots.unregister_module("invoice")
    │   ├─ Desmonta componentes, locales, estáticos
    │   ├─ _drop_module_metadata("invoice") → SQLAlchemy cleanup
    │   ├─ ImportManager.purge("invoice") → sys.modules cleanup
    │   ├─ gc.collect() × 2
    │   └─ registry.unregister("invoice")
    │
    ├─ ModuleStateDB.deactivate(status='disabled')
    │
    └─ bus.emit("module.deactivated")
```

### Flujo `update` con rollback de versión

```
ModuleRuntime.update(session, hub_id, "invoice", "2.1.0")
    │
    ├─ Descarga nueva versión → /app/modules/invoice/ (sobrescribe)
    ├─ Valida nuevo manifest
    ├─ lifecycle.on_deactivate + loader.unload_module  (si estaba activo)
    ├─ Alembic upgrade head (migraciones de v2.1.0)
    ├─ lifecycle.on_upgrade(from_version="2.0.0", to_version="2.1.0")
    │
    ├─ loader.load_module  ←── si FALLA aquí:
    │                              intenta recargar v2.0.0 desde disco
    │
    ├─ lifecycle.on_activate
    ├─ state.activate + actualiza version/checksum en BD
    └─ bus.emit("module.updated")
```

---

## Cómo encaja con el resto del framework

### Bootstrap (`create_app`)

En `hotframe/bootstrap.py`, `create_app` crea el `ModuleRuntime` y lo
guarda en `app.state.module_runtime`. Durante el lifespan startup llama
a `runtime.boot_all_active_modules(session)`.

```python
# En create_app (simplificado):
module_runtime = ModuleRuntime(
    app=app,
    settings=settings,
    event_bus=event_bus,
    hooks=hook_registry,
    slots=slot_registry,
    components=component_registry,
)
app.state.module_runtime = module_runtime

@app.lifespan
async def lifespan(_app):
    async with get_db_session() as session:
        await module_runtime.boot_all_active_modules(session)
    yield
    await module_runtime.shutdown()
```

### `ModuleRegistry`

`ModuleRuntime` usa `ModuleRegistry` (de `hotframe/apps/registry.py`)
como registro en memoria de los módulos actualmente cargados.
`registry.is_loaded(module_id)` permite comprobar si un módulo está en
memoria antes de intentar cargarlo o descargarlo.

### `ModuleMigrationRunner`

Accedido como `self.migrations`, ejecuta las migraciones Alembic de cada
módulo de forma aislada. Cada módulo tiene su propio directorio
`migrations/` con su propio `env.py`. El runner convierte una URL
async (`asyncpg`) a sync (`psycopg2`) para que Alembic pueda usarla.

### `MiddlewareStackManager`

Cuando un módulo declara `MIDDLEWARE = "invoice.middleware.InvoiceMiddleware"`
en su manifest, `ModuleLoader` delega en `MiddlewareStackManager` para
añadir/quitar la clase de middleware de la pila de Starlette
*atómicamente*: construye la nueva pila completa y la instala de una vez,
sin momento de inconsistencia.

### `AsyncEventBus`

`ModuleLoader` llama a `bus.unsubscribe_module(module_id)` en el
unload. Esto requiere que el bus implemente un mecanismo de tracking por
módulo: cuando un módulo se suscribe a eventos en su `events.py`,
registra el `module_id` como propietario de cada suscripción. Al
desactivar, todas las suscripciones de ese módulo se eliminan en bloque.

### `SlotRegistry` y `ComponentRegistry`

Análogo al bus: el `SlotRegistry` tiene `unregister_module(module_id)`
y el `ComponentRegistry` tiene `unregister_module(module_id)`. Ambos
eliminan en bloque todos los registros del módulo, garantizando que las
plantillas que renderizan slots o usan componentes del módulo simplemente
no encuentran entradas (comportamiento silencioso, sin excepción).

### Templates Jinja2

`_refresh_templates()` llama a `refresh_template_dirs(templates,
MODULES_DIR)` que rescana `modules/*/templates/` y actualiza los
`search_path` del loader Jinja2. Sin este paso, las plantillas del módulo
recién activado serían invisibles.

---

## Gotchas y decisiones de diseño

### 1. `sys.modules` es exacto, no por prefijo

En versiones anteriores del engine se hacía:
```python
# Malo: borra "invoiceapp" si el módulo se llama "invoice"
for key in list(sys.modules.keys()):
    if key == "invoice" or key.startswith("invoice."):
        del sys.modules[key]
```

`ImportManager` soluciona esto con el snapshot antes/después del import:
sólo borra exactamente las entradas que *ese* import creó.

### 2. `Table 'x' is already defined` — la fuga de SQLAlchemy

El error más frecuente en reinstalaciones. Se produce cuando:
1. Se importa `invoice.models` → SQLAlchemy registra `invoice_item` en
   `Base.metadata`.
2. Se purga `sys.modules["invoice.models"]`, pero el objeto `Table` y el
   mapper siguen en `Base.metadata` y `Base.registry`.
3. Se importa `invoice.models` otra vez → SQLAlchemy encuentra la tabla
   ya registrada.

La solución es la secuencia `_drop_module_metadata` → `_purge_module` →
`_verify_metadata_cleared` con limpieza de emergencia si la verificación
falla.

### 3. Fugas de memoria — tres vectores (ver `test_unload_leaks.py`)

El test `test_install_uninstall_cycle_stable_memory` ejecuta 50 ciclos
y mide el RSS. El presupuesto es 256 KB/ciclo. Los tres vectores conocidos:

**Vector A — HTTP clients**: un módulo que registra un cliente HTTP
nombrado y no lo desregistra en `on_deactivate`. El loader llama
`http_clients.unregister_module(module_id)` como safety net.

**Vector B — Starlette middleware stack**: al reconstruir la pila,
`BaseHTTPMiddleware` crea closures que pueden retener el middleware
antiguo via ciclos de referencia. El loader llama `gc.collect()` justo
después de `stack_manager.remove_and_rebuild`.

**Vector C — SQLAlchemy mappers**: si el dispose no se llama en el orden
correcto, el mapper registry retiene referencias a las clases del módulo
a través de weak sets internos que sólo se liberan con `gc.collect()`.
El loader llama `gc.collect()` al final de `unload_module`.

### 4. Advisory lock de Postgres para boot multi-worker

Con `uvicorn --workers 4`, los cuatro workers ejecutan `boot` en paralelo.
Sin el lock, los cuatro escribirían `UPDATE hub_module SET manifest=...`
concurrentemente para las mismas filas, causando deadlocks y flipping
modules a `error`.

La solución: `pg_try_advisory_xact_lock` con una clave derivada del
`hub_id` via BLAKE2b. El primer worker en obtenerla hace los DB writes.
Los demás montan las rutas en su proceso (necesario: cada worker tiene
su propia app FastAPI en memoria) pero no tocan la BD.

### 5. `MODULE_STATE_MODEL` — modelo swappable

El motor nunca referencia directamente `hotframe.engine.models.Module`
en los queries. Siempre usa `_get_module_model()`. Esto permite que
proyectos multi-tenant reemplacen el modelo con uno que tenga `hub_id`:

```python
# settings.py
MODULE_STATE_MODEL = "myproject.modules.HubModule"
```

```python
# myproject/modules.py
class HubModule(Base):
    __tablename__ = "hub_module"
    hub_id: Mapped[UUID]
    module_id: Mapped[str]
    # ... todos los campos de Module + hub_id
```

### 6. `is_system` — módulos no desinstalables

Módulos del sistema (auth, core, shared) pueden declarar `IS_SYSTEM=True`
en su `ModuleManifest`. `deactivate` y `uninstall` comprueban este campo
y devuelven error antes de hacer nada:

```python
if mod.is_system:
    result.error = f"Cannot deactivate system module '{module_id}'"
    return result
```

### 7. Rollback de `on_install` no existe

El rollback de la fase `IMPORTING` es un no-op deliberado. Si `on_install`
crea datos en BD y luego el pipeline falla en `MOUNTING`, esos datos
quedaron ahí. El rationale: `on_install` debe ser idempotente y su
limpieza es responsabilidad de `on_uninstall`, que corre en el flujo de
uninstall normal. No tiene sentido llamar a `on_uninstall` desde el
rollback de install porque significaría que las migraciones aún no se
han revertido.

### 8. Módulo degradado vs. error

`degraded` significa "sigue montado pero falla demasiado": las rutas
siguen respondiendo (con 503), el módulo sigue en `sys.modules`, pero la
UI avisa y sugiere desactivarlo. `error` significa "no se pudo cargar":
las rutas no existen en absoluto.

`get_active_modules` no devuelve filas `degraded`, de modo que el
próximo reinicio del proceso deja el módulo sin montar hasta que el
operador lo reactiva explícitamente (que resetea el tracker via
`reset_tracker`).

### 9. Hot-reload en desarrollo

`ModuleWatcher` (de `hotframe/dev/autoreload.py`) observa cambios en
`MODULES_DIR` y llama `runtime.hot_reload(module_id)` automáticamente
cuando `DEBUG=True`. El hot-reload **no** toca la BD ni corre
migraciones; sólo recarga el código Python. Cambios en modelos requieren
`hf makemigrations + hf migrate` manualmente.

---

## Tests de referencia

### `test_boundary.py`

Construye una app Starlette mínima con un middleware falso de sesión y
una ruta de módulo que explota. Verifica:

- Respuesta 503 contenida sin afectar `/health`.
- JSON vs HTML según la ruta (`/api/v1/m/...` vs `/m/...`).
- `module.error` y `module.degraded` en el bus.
- `_ModuleErrorTracker.record()` no degrada antes del umbral.
- `reset_tracker` limpia el historial.

### `test_module_metadata_lifecycle.py`

Prueba unitaria de `_register_exported_models` y `_drop_module_metadata`
con modelos SQLAlchemy creados dinámicamente con `type()`. Casos:

- Registrar + drop elimina la tabla de `Base.metadata`.
- Dos ciclos install→unload→install no lanza "Table already defined".
- `_drop_module_metadata` en módulo desconocido es idempotente.
- `_verify_metadata_cleared` detecta tablas residuales pero ignora las
  de otros módulos.

### `test_unload_leaks.py`

Crea un módulo falso en disco con `routes.py` y `models.py` reales
(no mocks), ejecuta 50 ciclos de `load_module`/`unload_module` y mide
el RSS con `psutil` (o `resource.getrusage` si no está). Assertions:

- `fake_leakcheck` no está en `Base.metadata.tables` al final.
- El crecimiento de RSS es `< 256 KB/ciclo`.
