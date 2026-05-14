# 3. Apps estáticas (`apps/`)

> `apps/` define el contrato declarativo de las apps y módulos: qué son, cómo se describen a sí mismos, cómo se registran en memoria y cómo exponen servicios con permisos. Es la columna vertebral conceptual sobre la que el engine y el discovery construyen el resto.

---

## Para qué sirve esta carpeta

Cuando el GUIDE habla de "Apps vs módulos: la pieza clave", se refiere a las abstracciones que viven aquí. `apps/` proporciona:

1. **`AppConfig` y `ModuleConfig`** (`config.py`) — las clases base que todo `app.py` y `module.py` de usuario debe subclasificar. Son la declaración de identidad de una app o módulo.
2. **`ModuleManifest`** (`config.py`) — el esquema Pydantic estricto que valida los atributos sueltos de un `module.py` heredado (el contrato legacy).
3. **`AppRegistry` y `ModuleRegistry`** (`registry.py`) — los registros en memoria que el engine usa como fuente de verdad de qué está cargado en el proceso actual.
4. **`ModuleService` y el decorador `@action`** (`service_facade.py`) — la capa de servicios con permisos declarativos, helpers de respuesta y el registro global `SERVICE_REGISTRY`.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`__init__.py`](../src/hotframe/apps/__init__.py) | Re-exporta todos los símbolos públicos del paquete. |
| [`config.py`](../src/hotframe/apps/config.py) | Define `AppConfig`, `ModuleConfig`, `ModuleManifest`, `MenuConfig`, `NavigationItem`, `load_manifest()`, `manifest_to_dict()`. |
| [`registry.py`](../src/hotframe/apps/registry.py) | Define `RegisteredModule`, `ModuleRegistry` (legacy) y `AppRegistry` (nuevo contrato). |
| [`service_facade.py`](../src/hotframe/apps/service_facade.py) | Define `ModuleService`, `@action`, `ActionMeta`, `ActionEntry`, `ServiceEntry`, `SERVICE_REGISTRY`, `register_services()`, `unregister_module_services()`, `generate_module_context()`. |

---

## `config.py` — AppConfig, ModuleConfig y ModuleManifest

### Sub-modelos de navegación y menú

#### `MenuConfig`

```python
class MenuConfig(BaseModel):
    label: str
    icon: str = "cube-outline"
    order: int = 50
```

Configura la entrada del módulo en el sidebar de navegación. `order` controla la posición: menor número aparece antes. Si un módulo no define `MENU`, no aparece en el sidebar.

#### `NavigationItem`

```python
class NavigationItem(BaseModel):
    label: str
    icon: str
    id: str
    view: str = ""
```

Una pestaña dentro de la barra de navegación interna de un módulo. `id` es el identificador de la sección; `view` es la ruta o nombre de vista a cargar.

---

### `ModuleManifest`

```python
class ModuleManifest(BaseModel):
    MODULE_ID: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    MODULE_NAME: str
    MODULE_VERSION: str = Field(pattern=r"^\d+\.\d+\.\d+")
    MODULE_ICON: str = "cube-outline"
    MODULE_DESCRIPTION: str = ""
    MODULE_AUTHOR: str = ""
    HAS_MODELS: bool = False
    MENU: MenuConfig | None = None
    NAVIGATION: list[NavigationItem] = []
    PERMISSIONS: list[str] = []
    ROLE_PERMISSIONS: dict[str, list[str | tuple]] = {}
    DEPENDENCIES: list[str] = []
    MIDDLEWARE: str | None = None
    SCHEDULED_TASKS: list[dict] = []
    PRICING: dict | None = None
```

El contrato **legacy** para `module.py`. En vez de subclasificar `ModuleConfig`, los módulos más viejos declaran constantes en mayúsculas directamente en `module.py`. `load_manifest()` las extrae y `ModuleManifest` las valida.

Restricciones de validación importantes:
- `MODULE_ID` debe coincidir con `^[a-z][a-z0-9_]*$` — solo minúsculas, dígitos y guiones bajos, empezando por letra.
- `MODULE_VERSION` debe ser semver: `^\d+\.\d+\.\d+`.
- Si `MODULE_ID` o `MODULE_VERSION` fallan, el módulo queda en estado `error` y no puede cargarse.

#### Validador `normalize_permissions`

```python
@field_validator("PERMISSIONS", mode="before")
@classmethod
def normalize_permissions(cls, v: Any) -> list[str]:
    result = []
    for item in v:
        if isinstance(item, (tuple, list)):
            result.append(str(item[0]))
        else:
            result.append(str(item))
    return result
```

Acepta tanto strings simples `"codename"` como tuplas `("codename", "descripción")`. La descripción se descarta aquí: solo se conserva el código de permiso.

---

### `load_manifest(module_path: Path) -> ModuleManifest`

```python
def load_manifest(module_path: Path) -> ModuleManifest:
```

Importa `module.py` desde `module_path` usando `importlib.util.spec_from_file_location` con un nombre temporal (`_manifest_loader_<nombre>`), ejecuta el módulo, extrae los atributos que coinciden con los campos de `ModuleManifest`, y construye la instancia validada. El módulo temporal se elimina de `sys.modules` antes de retornar para evitar colisiones.

Este mecanismo permite leer el manifiesto de un módulo **sin que ese módulo quede cargado en el proceso**. Es la misma estrategia que usa el engine para inspeccionar un módulo antes de decidir si instalarlo.

```python
try:
    manifest = load_manifest(Path("modules/invoice"))
    print(manifest.MODULE_NAME, manifest.MODULE_VERSION)
except FileNotFoundError:
    print("No hay module.py")
except ValidationError as e:
    print("Manifiesto inválido:", e)
```

---

### `manifest_to_dict(manifest: ModuleManifest) -> dict`

```python
def manifest_to_dict(manifest: ModuleManifest) -> dict[str, Any]:
```

Serializa el manifiesto usando claves cortas y legibles en vez de los nombres en mayúsculas del Pydantic model. El mapeo es:

| Clave Pydantic | Clave en dict |
|---|---|
| `MODULE_ID` | `module_id` |
| `MODULE_NAME` | `name` |
| `MODULE_VERSION` | `version` |
| `MODULE_ICON` | `icon` |
| `MODULE_DESCRIPTION` | `description` |
| `MODULE_AUTHOR` | `author` |
| `HAS_MODELS` | `has_models` |
| `MENU` | `menu` |
| `NAVIGATION` | `navigation` |
| `PERMISSIONS` | `permissions` |
| `ROLE_PERMISSIONS` | `role_permissions` |
| `DEPENDENCIES` | `dependencies` |
| `MIDDLEWARE` | `middleware` |
| `SCHEDULED_TASKS` | `scheduled_tasks` |
| `PRICING` | `pricing` |

El resultado se almacena en la columna `manifest` de la tabla `hub_module` (JSON), lo que permite que templates y APIs accedan a `manifest.name` en vez de `manifest.MODULE_NAME`.

---

### `AppConfig`

```python
class AppConfig:
    name: str = ""
    verbose_name: str = ""
    mount_prefix: str = ""        # vacío → f"/{name}/"
    media_path: str = ""          # vacío → usa app name
    version: str = "0.1.0"
    depends: list[str] = []
    permissions: list[tuple[str, str]] = []
    role_permissions: dict[str, list[str]] = {}
    menu: dict | None = None
    navigation: list[dict] = []
    is_builtin: bool = False
    _abstract: bool = False
```

La clase base que todo `apps/<nombre>/app.py` debe subclasificar. Al contrario del contrato de constantes de `ModuleManifest`, aquí el contrato es orientado a objetos: atributos de clase y un método `ready()`.

#### `__init_subclass__`

```python
def __init_subclass__(cls, **kwargs) -> None:
    super().__init_subclass__(**kwargs)
    if "_abstract" not in cls.__dict__:
        cls._abstract = False
    if cls._abstract:
        return
    if not cls.name:
        raise ValueError(f"{cls.__name__}: AppConfig subclass must define 'name'")
```

Se ejecuta en tiempo de definición de la clase. Si `_abstract` no está en el `__dict__` de la subclase (es decir, no fue declarado explícitamente), se resetea a `False`. Esto impide que el flag `_abstract=True` de `ModuleConfig` se herede por accidente a una subclase concreta del módulo del usuario. Después, si `_abstract=False` y `name` está vacío, lanza `ValueError` en tiempo de importación — falla rápido y claro.

#### `async def ready(self) -> None`

```python
async def ready(self) -> None:
    return None
```

Hook que se llama una vez después de que todas las apps están cargadas y sus routers montados. El uso típico es importar el módulo de señales para registrar los decoradores `@receiver`. La implementación base es un no-op; las subclases la sobreescriben cuando necesitan. Puede ser `async def` o `def` — el bootstrap lo detecta con `inspect.iscoroutinefunction`.

Ejemplo:

```python
class SharedConfig(AppConfig):
    name = "shared"
    verbose_name = "Shared"
    is_builtin = True

    async def ready(self) -> None:
        import apps.shared.signals  # registra receivers al importar
```

---

### `ModuleConfig`

```python
class ModuleConfig(AppConfig):
    _abstract: bool = True

    requires_restart: bool = False
    is_system: bool = False
    has_views: bool = True
    has_api: bool = True
    media_path: str = ""
    s3_key: str | None = None
    sha256: str | None = None
```

Hereda de `AppConfig` y añade los atributos específicos de los módulos dinámicos. El flag `_abstract=True` es deliberado: `ModuleConfig` no debe tener `name` (los módulos son abstractos hasta que el usuario los subclasifica con un `name` concreto).

Atributos adicionales:

| Atributo | Tipo | Default | Significado |
|---|---|---|---|
| `requires_restart` | `bool` | `False` | Si `True`, los cambios en el módulo no pueden aplicarse en caliente y requieren reiniciar el proceso. |
| `is_system` | `bool` | `False` | Si `True`, el módulo no puede desinstalarse desde la UI (es un módulo del sistema). |
| `has_views` | `bool` | `True` | El módulo tiene rutas HTML (monta `routes.py`). |
| `has_api` | `bool` | `True` | El módulo tiene rutas REST (monta `api.py`). |
| `s3_key` | `str \| None` | `None` | Clave S3 explícita. Si vacío, se calcula a partir de `name + version`. |
| `sha256` | `str \| None` | `None` | Hash SHA256 explícito del paquete para verificación de integridad. |

#### Hooks de ciclo de vida

```python
async def install(self, ctx: Any) -> None: ...
async def uninstall(self, ctx: Any) -> None: ...
async def activate(self, ctx: Any) -> None: ...
async def deactivate(self, ctx: Any) -> None: ...
```

Todos son no-ops en la base. El engine los llama en los momentos correspondientes del ciclo de vida. `ctx` es el contexto de la operación (sesión de DB, settings, etc.).

- `install` — seed de datos iniciales en la primera instalación.
- `uninstall` — limpieza idempotente al desinstalar.
- `activate` — setup que necesita ejecutarse cada vez que el módulo se activa (por ejemplo, registrar tareas planificadas).
- `deactivate` — limpieza de estado cuando el módulo se desactiva pero no se desinstala.

Un `ModuleConfig` completo en un proyecto real:

```python
# modules/notes/module.py
from hotframe.apps import ModuleConfig

class NotesModule(ModuleConfig):
    name = "notes"
    verbose_name = "Notes"
    version = "1.0.0"
    has_views = True
    has_api = True
    is_system = False
    requires_restart = False
    menu = {"label": "Notes", "icon": "document-text-outline", "order": 30}

    async def install(self, ctx):
        await ctx.db.execute("INSERT INTO ...")  # seed data

    async def ready(self):
        import modules.notes.signals
```

---

## `registry.py` — Los registros en memoria

### `RegisteredModule`

```python
@dataclass(slots=True)
class RegisteredModule:
    module_id: str
    manifest: ModuleManifest
    router: APIRouter | None = None
    api_router: APIRouter | None = None
    middleware: Any | None = None
    path: Path = field(default_factory=Path)
    loaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
```

Snapshot del estado de un módulo dinámico cargado en el proceso. Usa `slots=True` para reducir el overhead de memoria cuando hay muchos módulos. `loaded_at` se usa para diagnóstico y métricas.

---

### `ModuleRegistry` (contrato legacy)

```python
class ModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, RegisteredModule] = {}
        self._version: int = 0
```

Registro de módulos dinámicos cargados con el pipeline legacy (`ModuleManifest`). No es persistente: se reconstruye en cada arranque frío. Thread-safety: acceso desde un único event loop asyncio, por lo que un dict plano es suficiente.

#### Métodos de mutación

##### `register(...) -> RegisteredModule`

```python
def register(
    self,
    module_id: str,
    manifest: ModuleManifest,
    router: APIRouter | None,
    api_router: APIRouter | None,
    middleware: Any | None,
    path: Path,
) -> RegisteredModule:
```

Crea un `RegisteredModule`, lo almacena en `_modules[module_id]` e incrementa `_version`. Loguea el registro con nombre y versión.

##### `unregister(module_id: str) -> None`

Elimina el módulo del diccionario e incrementa `_version`. Silencioso si el módulo no estaba registrado.

#### Métodos de consulta

| Método | Descripción |
|---|---|
| `get(module_id)` | Devuelve `RegisteredModule` o `None`. |
| `get_all()` | Copia defensiva del dict. |
| `is_loaded(module_id)` | Booleano. |
| `get_loaded_module_ids()` | Lista de IDs actualmente cargados. |

#### Datos derivados

##### `get_menu_items() -> list[dict]`

```python
def get_menu_items(self) -> list[dict]:
    items = [
        {"module_id": ..., "label": ..., "icon": ..., "order": ...}
        for entry in self._modules.values()
        if entry.manifest.MENU is not None
    ]
    items.sort(key=lambda m: (m["order"], m["label"]))
    return items
```

Solo incluye módulos que declaran `MENU`. Ordena primero por `order` (ascendente) y luego alfabéticamente por `label` para desempates deterministas.

##### `get_navigation(module_id) -> list[dict]`

Devuelve los `NavigationItem` del módulo como lista de dicts. Vacío si el módulo no existe.

##### `get_module_middleware() -> list[Any]`

Devuelve todos los middleware de todos los módulos cargados. Alias: `get_all_middleware` (compatibilidad con `module_middleware.py`).

##### `get_permissions(module_id) -> list[str]`

Permisos de un módulo concreto.

##### `get_all_permissions() -> list[str]`

Todos los permisos de todos los módulos, en una lista plana.

#### Versionado del registro

```python
@property
def version(self) -> int:
    return self._version
```

Contador monótonamente creciente. Se incrementa en cada `register` / `unregister`. Los consumidores (template loaders, caché del OpenAPI, caché de menús) almacenan el último `version` que vieron y comparan contra él para decidir si reconstruir.

---

### `AppRegistry` (nuevo contrato, Fase 3+)

```python
class AppRegistry:
    def __init__(self) -> None:
        self._apps: dict[str, AppConfig] = {}
        self._lock: asyncio.Lock | None = None
```

El registro nuevo, pensado para coexistir con `ModuleRegistry` durante la migración y eventualmente reemplazarlo. A diferencia de `ModuleRegistry`, almacena instancias de `AppConfig` (no manifests + routers por separado), lo que permite acceder a todos los atributos de la config directamente.

**Thread-safety mejorada:** usa un `asyncio.Lock` lazy (se crea solo si se usa). El lock es lazy porque en la mayoría de los arranques solo hay un evento de registro, y crear el lock innecesariamente tiene coste.

#### `async register(config: AppConfig) -> None`

```python
async def register(self, config: AppConfig) -> None:
    async with self._get_lock():
        if config.name in self._apps:
            raise ValueError(f"App {config.name!r} already registered")
        self._apps[config.name] = config
```

Lanza `ValueError` si el nombre ya existe. Protegido con lock para ser seguro en entornos con concurrencia async.

#### `async unregister(name: str) -> AppConfig | None`

Elimina y devuelve la config. Devuelve `None` si no estaba registrada.

#### `get(name: str) -> AppConfig | None`

Lectura sin async (el camino frecuente). O(1).

#### `all() -> list[AppConfig]`

Snapshot de todas las configs registradas.

#### `by_kind(*, builtin: bool | None = None) -> list[AppConfig]`

```python
def by_kind(self, *, builtin: bool | None = None) -> list[AppConfig]:
    items = self._apps.values()
    if builtin is None:
        return list(items)
    return [c for c in items if c.is_builtin is builtin]
```

Filtra por tipo:
- `builtin=True` → apps core del proyecto (viven en `apps/`, tienen `is_builtin=True`).
- `builtin=False` → módulos dinámicos (instalados desde el marketplace, `is_builtin=False`).
- `builtin=None` → todos.

Soporta `in` y `len()` directamente (`__contains__`, `__len__`).

---

## `service_facade.py` — Servicios con permisos

### El decorador `@action`

```python
@dataclass(frozen=True, slots=True)
class ActionMeta:
    permission: str
    mutates: bool = False
    description: str = ""

def action(*, permission: str, mutates: bool = False, description: str = "") -> Any:
    def decorator(fn: Any) -> Any:
        fn._action_meta = ActionMeta(
            permission=permission, mutates=mutates, description=description
        )
        return fn
    return decorator
```

Decora métodos de `ModuleService` marcándolos como acciones invocables externamente. El atributo `_action_meta` es leído por `register_services()` para construir el `SERVICE_REGISTRY`. El parámetro `mutates=True` indica que la acción modifica datos (útil para logs de auditoría y para que la UI muestre advertencias de confirmación).

---

### `ModuleService`

```python
class ModuleService:
    module_id: str = ""

    def __init__(self, db: ISession, hub_id: UUID) -> None:
        self.db = db
        self.hub_id = hub_id
```

Clase base para los servicios de módulo. Recibe la sesión de base de datos y el `hub_id` (identificador del tenant en arquitecturas multi-tenant) en el constructor.

#### Métodos de acceso a datos

##### `q(model: type) -> IQueryBuilder`

```python
def q(self, model: type) -> IQueryBuilder:
    return HubQuery(model, self.db, self.hub_id)
```

Devuelve un `HubQuery` filtrado por `hub_id`. Todas las consultas hechas a través de `self.q()` quedan automáticamente circunscritas al tenant actual.

##### `repo(model, *, search_fields=None, default_order="created_at") -> IRepository`

```python
def repo(
    self,
    model: type,
    *,
    search_fields: list[str] | None = None,
    default_order: str = "created_at",
) -> IRepository[Any]:
    return BaseRepository(model, self.db, self.hub_id, ...)
```

Devuelve un `BaseRepository` hub-scoped. El tipo de retorno es `IRepository[Any]` (el protocolo), no `BaseRepository`, de forma que el servicio no se acopla a la implementación concreta.

#### Helpers de respuesta

##### `success(**fields) -> dict`

```python
@staticmethod
def success(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}
```

Construye una respuesta de éxito con forma consistente. Siempre incluye `"ok": True`. Uso:

```python
return self.success(id=str(todo.id), created=True)
# → {"ok": True, "id": "abc", "created": True}
```

##### `error(message, *, code="", **fields) -> dict`

```python
@staticmethod
def error(message: str, *, code: str = "", **fields: Any) -> dict[str, Any]:
    body = {"ok": False, "error": message}
    if code:
        body["code"] = code
    body.update(fields)
    return body
```

Construye una respuesta de error. `code` es un identificador de máquina para que los clientes puedan ramificar sin parsear el mensaje humano.

#### Helpers de parseo

| Método | Qué hace |
|---|---|
| `parse_uuid(value)` | `str \| UUID \| None` → `UUID \| None`. Devuelve `None` para vacíos, lanza `ValueError` para strings malformados. |
| `parse_date(value, *, fmt="%Y-%m-%d")` | String ISO → `date`. Vacío → `None`. |
| `parse_decimal(value)` | String → `Decimal`. Vacío → `None`. |

#### Helpers de lookup

##### `async get_or_none(model, id_value) -> Any`

Busca por clave primaria UUID. Devuelve la fila o `None` sin tocar la DB para valores vacíos.

##### `async get_or_error(model, id_value, *, not_found_message, code) -> tuple`

```python
async def get_or_error(
    self,
    model: type,
    id_value: str | UUID | None,
    *,
    not_found_message: str = "Not found",
    code: str = "not_found",
) -> tuple[Any, dict[str, Any] | None]:
```

Patrón idiomático para handlers:

```python
todo, err = await self.get_or_error(Todo, todo_id)
if err:
    return err
# todo es el objeto, garantizado no-None
```

Devuelve `(row, None)` en éxito o `(None, error_dict)` en fallo.

#### `atomic()`

```python
def atomic(self) -> Any:
    from hotframe.orm.transactions import atomic as _atomic
    return _atomic(self.db)
```

Atajo para transacciones explícitas:

```python
async with self.atomic():
    await self.repo(Invoice).create(...)
    await self.repo(Line).create(...)
```

#### `serialize(obj) / serialize_list(items)`

Delegan a las funciones `serialize` y `serialize_list` de `hotframe.repository.base`. Convierten objetos ORM a dicts planos JSON-serializables.

---

### El registro global `SERVICE_REGISTRY`

```python
SERVICE_REGISTRY: dict[str, dict[str, ServiceEntry]] = {}
```

Estructura: `{module_id: {ClassName: ServiceEntry}}`.

```python
@dataclass
class ServiceEntry:
    cls: type[ModuleService]
    description: str
    actions: dict[str, ActionEntry] = field(default_factory=dict)

@dataclass
class ActionEntry:
    method_name: str
    permission: str
    mutates: bool
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
```

---

### `register_services(module_id: str) -> int`

```python
def register_services(module_id: str) -> int:
```

Importa `<module_id>.services`, itera sus atributos buscando subclases de `ModuleService`, y por cada una recoge todos los métodos con `_action_meta`. Construye el `SERVICE_REGISTRY` y retorna el número de servicios registrados.

El nombre de permiso completo se construye como `f"{module_id}.{meta.permission}"`, de forma que `"view"` en el módulo `"notes"` se convierte en `"notes.view"`.

Si `<module_id>.services` no existe, devuelve `0` silenciosamente (el módulo simplemente no tiene servicios).

Llamada por el engine durante la activación del módulo.

---

### `unregister_module_services(module_id: str) -> int`

Elimina las entradas del módulo del `SERVICE_REGISTRY`. Llamada durante la desactivación.

---

### `generate_module_context(module_id: str) -> str`

```python
def generate_module_context(module_id: str) -> str:
```

Serializa el `SERVICE_REGISTRY` de un módulo a un string en formato Markdown-like apto para ser inyectado como contexto en un LLM. Formato de salida:

```
### TodoService
Gestiona los TODOs del usuario

- **list_todos**() → Lista todos los TODOs | READ
- **create_todo**(text: string, done?: boolean = False) → Crea un TODO | WRITE
```

Este método es la interfaz entre el registry de servicios y el asistente IA integrado en hotframe.

---

### `generate_all_contexts() -> dict[str, str]`

Aplica `generate_module_context` a todos los módulos registrados y devuelve un dict `{module_id: context_string}`.

---

## Cómo encaja con el resto del framework

**`bootstrap.py`** llama a `_auto_discover_apps(app)` que busca `app.py` en cada subdirectorio de `apps/`. Si encuentra una subclase de `AppConfig`, instancia la config y llama a `ready()`. Las rutas se montan directamente (sin pasar por `AppRegistry` en ese path — la integración completa con `AppRegistry` es parte de la migración en curso hacia el nuevo contrato).

**`discovery/scanner.py`** usa `find_entry_config()` para extraer la clase `AppConfig` o `ModuleConfig` de un `DiscoveryResult`. El scanner no registra nada; solo descubre.

**`engine/module_runtime.py`** usa `ModuleRegistry` como fuente de verdad de módulos activos. Cuando activa un módulo, llama a `registry.register(...)` con el manifiesto y los routers; cuando lo desactiva, llama a `registry.unregister(...)`. También llama a `register_services(module_id)` durante la activación y `unregister_module_services(module_id)` durante la desactivación.

**El middleware manager** llama a `registry.get_module_middleware()` para construir el stack de middleware dinámico.

**El template loader** compara `registry.version` con su último valor visto para invalidar la caché de templates cuando se monta o desmonta un módulo.

---

## Gotchas y decisiones de diseño

**Dos registros coexisten durante la migración.** `ModuleRegistry` (legacy, basado en `ModuleManifest`) y `AppRegistry` (nuevo, basado en `AppConfig`) conviven en el paquete. El comentario en el código los llama "Fase 3+" y "Fase 4+ los unificará". Si ves código que usa uno u otro, es por el momento del ciclo de vida del framework en que fue escrito.

**`AppConfig.name` es obligatorio en tiempo de definición de clase.** `__init_subclass__` lanza `ValueError` si `name` está vacío. Esto significa que el error falla al importar el módulo, no cuando el engine intenta registrarlo. El fallo es inmediato y el mensaje es claro.

**`ModuleConfig._abstract=True` se resetea en las subclases.** Este es el mecanismo más sutil del paquete. `__init_subclass__` resetea `_abstract=False` en cada subclase a menos que la subclase declare explícitamente `_abstract=True`. Esto impide que `ModuleConfig` herede su flag a subclases concretas. Sin este mecanismo, `class NotesModule(ModuleConfig): name = "notes"` estaría marcada como abstracta y `__init_subclass__` no validaría `name`.

**`SERVICE_REGISTRY` es un módulo-global mutable.** Esto es una decisión pragmática: los servicios se registran al activar módulos y se desregistran al desactivarlos, lo que hace necesario un store global. En un entorno multi-proceso, este store no se comparte entre workers.

**`ModuleService` siempre recibe `hub_id`.** Incluso en aplicaciones que no son multi-tenant, el parámetro existe. En ese caso, `hub_id` puede ser un UUID fijo o ignorarse. La razón es que el framework está diseñado para ser multi-tenant desde el principio, y cambiar la firma de `__init__` después sería un breaking change.

**No hay `INSTALLED_APPS`.** Las apps se descubren automáticamente escaneando `apps/`. Si quieres controlar qué se monta, usa `EXTRA_ROUTERS` en settings para añadir routers externos, o no pongas el directorio en `apps/`. No hay lista negra.
