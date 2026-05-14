# 8. Migraciones (`migrations/`)

> Orquestación de Alembic para un proyecto donde cada app y cada módulo
> dinámico lleva sus propias migraciones — sin que ninguna pisotee el
> historial de otra. El runner resuelve el problema de los foreign keys
> cruzados y ejecuta las migraciones de módulos de forma segura en un
> proceso async.

---

## Para qué sirve esta carpeta

En hotframe cada unidad de código (apps estáticas y módulos dinámicos)
tiene su propio `migrations/` con su propio directorio `versions/` y su
propia tabla de versiones Alembic. Esto es lo que permite:

- **Instalar o desinstalar un módulo sin tocar las migraciones del core**
  ni de otras apps.
- **Correr `hf migrate` y que se apliquen todas las pendientes**, en orden,
  sin que colisionen.
- **Que `hf makemigrations <app>` autogenere** la revisión correcta para
  una sola app, sin que Alembic confunda tablas de otros módulos.

Esta carpeta implementa esa orquestación en tres piezas:

| Pieza | Responsabilidad |
|---|---|
| `runner.py` | Ejecutar migraciones de un módulo individual (upgrade / downgrade) |
| `multi_namespace.py` | Coordinar migraciones de todos los namespaces (core + apps) |
| `env_helpers.py` | Helpers para los `env.py` de Alembic: importar modelos que resuelven FKs cruzados |

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`__init__.py`](../src/hotframe/migrations/__init__.py) | Docstring + re-exportaciones conceptuales del paquete |
| [`env_helpers.py`](../src/hotframe/migrations/env_helpers.py) | `import_all_app_models`, `import_module_dependencies` — previenen `NoReferencedTableError` en autogenerate |
| [`multi_namespace.py`](../src/hotframe/migrations/multi_namespace.py) | `MultiNamespaceRunner`, `MigrationNamespace`, `MigrationReport` — coordina core + todas las apps |
| [`runner.py`](../src/hotframe/migrations/runner.py) | `ModuleMigrationRunner` — upgrade/downgrade de un módulo individual, async-safe |

---

## Qué significa "multi-namespace"

Alembic estándar asume que todas las migraciones comparten **una** tabla
`alembic_version`. En hotframe cada namespace tiene la suya:

| Namespace | Tabla de versiones |
|---|---|
| core | `alembic_version` |
| `apps/shared/` | `alembic_shared_version` |
| `apps/auth/` | `alembic_auth_version` |
| `modules/notes/` | `alembic_notes` |
| `modules/shop/` | `alembic_shop` |

El nombre de tabla viene del patrón:

- Core: `"alembic_version"` (convención estándar, sin sufijo).
- Apps: `f"alembic_{app_name}_version"`.
- Módulos: `f"alembic_{module_id}"`.

Esto significa que en la base de datos conviven N tablas de versiones. Cada
una tiene exactamente una fila con el `version_num` actual de su namespace.
`alembic upgrade head` solo mira la tabla de su propio namespace; nunca
afecta la de otro.

---

## `runner.py` — `ModuleMigrationRunner`

[`migrations/runner.py`](../src/hotframe/migrations/runner.py)

Gestiona las migraciones de **un módulo individual**. Lo usa el `ModuleRuntime`
en el ciclo de vida de los módulos: `upgrade` al activar, `downgrade` al
desinstalar.

### `ModuleMigrationRunner`

```python
class ModuleMigrationRunner:
```

No tiene estado de instancia (no recibe parámetros en `__init__`). Todos
los parámetros van en cada llamada. Es un namespace de métodos async y
estáticos.

### `upgrade(module_id, module_path, db_url)`

```python
async def upgrade(
    self,
    module_id: str,
    module_path: Path,
    db_url: str,
) -> None:
```

Ejecuta `alembic upgrade head` para el módulo. El argumento `db_url` debe
ser la URL **síncrona** (sin `+asyncpg` ni `+aiosqlite`) porque Alembic
usa sqlalchemy síncrono internamente.

Algoritmo completo:

1. Comprueba que `module_path / "migrations"` existe. Si no, devuelve sin
   error (los módulos sin modelos no necesitan migraciones).
2. Construye `version_table = f"alembic_{module_id}"`.
3. Llama a `_build_config(module_id, module_path, db_url, version_table)`.
4. Añade `module_path.parent` a `sys.path` si no está — para que el `env.py`
   del módulo pueda importar sus modelos como `{module_id}.models`.
5. Define `_run_upgrade()` que crea un `create_engine(db_url, poolclass=NullPool)`,
   inyecta el engine en `config.attributes["connection"]` y llama a
   `command.upgrade(config, "head")`.
6. Llama `await asyncio.to_thread(_run_upgrade)` — las APIs de Alembic son
   síncronas y bloqueantes; `asyncio.to_thread` las ejecuta en el
   threadpool sin bloquear el event loop.

El uso de `NullPool` en el engine creado para migraciones garantiza que
las conexiones se abren y cierran por operación — el engine de migraciones
no compite con el pool async de la aplicación.

### `downgrade(module_id, module_path, db_url)`

```python
async def downgrade(
    self,
    module_id: str,
    module_path: Path,
    db_url: str,
) -> None:
```

Ejecuta `alembic downgrade base` — revierte **todas** las migraciones del
módulo. Se usa durante `hf modules uninstall <name>` para limpiar el
esquema antes de eliminar el código.

Más simple que `upgrade`: construye el config y llama
`await asyncio.to_thread(command.downgrade, config, "base")`. No necesita
manipular `sys.path` (ya está de la activación previa).

### `has_migrations(module_path)`

```python
def has_migrations(self, module_path: Path) -> bool:
```

Comprueba si el módulo tiene un directorio `migrations/versions/` con al
menos un archivo `.py`. Lo usa el runtime para decidir si llamar a `upgrade`
o no, evitando el overhead de construir un config de Alembic innecesariamente.

### `_build_config(module_id, module_path, db_url, version_table)` (estático)

```python
@staticmethod
def _build_config(
    module_id: str,
    module_path: Path,
    db_url: str,
    version_table: str,
) -> Config:
```

Construye el `alembic.Config` para el módulo:

1. Si existe `migrations/alembic.ini`, lo usa como base.
2. Si no, crea un `Config()` vacío.
3. Sobreescribe/define:
   - `script_location` → `module_path / "migrations"`
   - `sqlalchemy.url` → `db_url`
   - `version_table` → como main_option Y como `config.attributes["version_table"]`
4. También guarda en `config.attributes`:
   - `module_id`
   - `module_path` (como string)

Los `config.attributes` son el canal de comunicación entre el runner y el
`env.py` del módulo. El `env.py` los lee para saber qué tabla de versiones
usar y cuál es el `module_path`.

### `get_sync_db_url(async_url)` (estático)

```python
@staticmethod
def get_sync_db_url(async_url: str) -> str:
```

Convierte una URL async a su equivalente síncrona:

- `postgresql+asyncpg://...` → `postgresql://...`
- `sqlite+aiosqlite://...` → `sqlite://...`

El `ModuleRuntime` llama a este helper antes de invocar `upgrade` o
`downgrade`, porque `settings.DATABASE_URL` siempre lleva el driver async.

---

## `multi_namespace.py` — coordinación de namespaces

[`migrations/multi_namespace.py`](../src/hotframe/migrations/multi_namespace.py)

Implementa la orquestación de migraciones de todas las apps (core +
apps estáticas). Los módulos dinámicos usan `ModuleMigrationRunner`
directamente desde el runtime; este runner se usa desde el CLI `hf migrate`.

### `MigrationNamespace`

```python
@dataclass
class MigrationNamespace:
    name: str           # "core", "accounts", "shared"
    script_location: Path
    version_table: str

    @classmethod
    def core(cls, root: Path) -> MigrationNamespace: ...

    @classmethod
    def for_app(cls, apps_root: Path, app_name: str) -> MigrationNamespace: ...
```

Objeto de datos que describe un namespace. Los dos constructores de clase
generan las convenciones correctas:

- `MigrationNamespace.core(root)` → name=`"core"`, script=`root/migrations`,
  version_table=`"alembic_version"`.
- `MigrationNamespace.for_app(apps_root, "auth")` → name=`"auth"`,
  script=`apps_root/auth/migrations`, version_table=`"alembic_auth_version"`.

### `MigrationReport`

```python
@dataclass
class MigrationReport:
    namespace: str
    applied: bool = False
    skipped: bool = False
    reason: str | None = None
    error: str | None = None
```

Resultado de aplicar las migraciones de un namespace. `applied=True`
significa que Alembic completó sin excepción. `error` contiene el mensaje
de la excepción si falló. El campo `skipped` está previsto para cuando el
namespace no tiene pendientes (actualmente no se usa — Alembic lo gestiona
internamente).

### `MultiNamespaceRunner`

```python
class MultiNamespaceRunner:
    def __init__(self, db_url: str, project_root: Path) -> None:
        self.db_url = db_url
        self.project_root = project_root
```

#### `discover_namespaces()`

```python
def discover_namespaces(self) -> list[MigrationNamespace]:
```

Descubre namespaces existentes en el filesystem:

1. Siempre añade el namespace `core` (`{project_root}/migrations/`).
2. Recorre `{project_root}/apps/*/` en orden alfabético.
3. Para cada subdirectorio, comprueba que existan tanto
   `migrations/env.py` como `migrations/versions/`. Solo si ambos existen
   incluye el namespace (apps sin migraciones, o apps recién creadas sin
   `versions/`, se ignoran).

El orden alfabético garantiza reproducibilidad: dos ejecuciones de
`discover_namespaces` devuelven siempre la misma lista.

#### `build_alembic_config(ns)`

```python
def build_alembic_config(self, ns: MigrationNamespace) -> AlembicConfig:
```

Construye un `AlembicConfig` para el namespace:

```python
cfg = AlembicConfig()
cfg.set_main_option("script_location", str(ns.script_location))
cfg.set_main_option("sqlalchemy.url", self.db_url)
cfg.attributes["version_table"] = ns.version_table
cfg.attributes["namespace_name"] = ns.name
```

`cfg.attributes` es el canal hacia `env.py` — el env.py del namespace lee
`context.config.attributes["version_table"]` para configurar Alembic con
la tabla correcta.

#### `upgrade(namespace=None, revision="head")`

```python
def upgrade(
    self, namespace: str | None = None, revision: str = "head"
) -> list[MigrationReport]:
```

Si `namespace` es `None`, actualiza todos. Si se especifica, filtra solo
ese namespace. Para cada namespace descubierto:

1. Construye el config con `build_alembic_config`.
2. Llama `alembic_command.upgrade(cfg, revision)`.
3. Añade `MigrationReport(namespace=ns.name, applied=True)` si tiene éxito,
   o `MigrationReport(namespace=ns.name, error=str(e))` si falla.

**Importante**: el loop continúa aunque un namespace falle. Recibes un
report por namespace y puedes inspeccionar cuáles fallaron. El CLI `hf migrate`
imprime los reports y sale con código de error si alguno tiene `error`.

#### `current(namespace=None)`

```python
def current(self, namespace: str | None = None) -> dict[str, str | None]:
```

Devuelve el `version_num` actual de cada namespace consultando directamente
las tablas en la DB. No usa el mecanismo de Alembic sino una query
`SELECT version_num FROM {version_table} LIMIT 1` para evitar inicializar
el entorno de Alembic solo para consultar.

Crea un **engine síncrono** temporal a partir de `self.db_url` limpiando
los prefijos async (`+asyncpg`, `+aiosqlite`). Si la tabla no existe
(namespace nunca migrado), devuelve `None` para ese namespace.

---

## `env_helpers.py` — resolviendo FKs cruzados en autogenerate

[`migrations/env_helpers.py`](../src/hotframe/migrations/env_helpers.py)

Este módulo no se ejecuta en runtime — solo durante `hf makemigrations`.
Resuelve el problema de que Alembic autogenerate necesita tener **todas
las tablas referenciadas** en `Base.metadata` para poder inspeccionar FKs
cruzados.

### El problema

Cada `env.py` de módulo importa solo sus propios modelos:

```python
# modules/shop/migrations/env.py
from shop.models import Order, OrderLine
```

Si `Order` tiene un FK a `users` (tabla de `apps/auth`), y `users` no
está en `Base.metadata` al correr autogenerate, Alembic lanza
`NoReferencedTableError`.

### `import_all_app_models(project_root=None)`

```python
def import_all_app_models(project_root: Path | None = None) -> list[str]:
```

Recorre `{project_root}/apps/*/models.py` en orden alfabético e importa
cada uno. El efecto secundario deseado es que cada `Model = Table(...)` de
SQLAlchemy se registre en `Base.metadata`.

Precauciones:

- Añade `project_root` a `sys.path` si no está (para que `apps.auth.models`
  sea importable).
- Es idempotente: la segunda llamada con el mismo `project_root` es un no-op
  (Python cachea los módulos importados).
- Errores en cualquier `models.py` se loguean como warning y el loop
  continúa — una app rota no impide migrar otras. Si el error era
  necesario para resolver un FK, Alembic fallará luego con un mensaje
  claro.

**Uso típico en `env.py` de un módulo**:

```python
# modules/shop/migrations/env.py
from hotframe.migrations.env_helpers import import_all_app_models, import_module_dependencies

import_all_app_models()                    # registra tablas de apps/
import_module_dependencies("shop")         # registra tablas de módulos dep.

from shop import models as _  # registra tablas del propio módulo
```

### `import_module_dependencies(module_id, project_root=None)`

```python
def import_module_dependencies(
    module_id: str,
    project_root: Path | None = None,
) -> list[str]:
```

Importa los `models.py` de todas las dependencias **transitivas** del módulo,
sin importar el módulo propio (su `env.py` ya lo hace).

Algoritmo de la caminata:

1. Lee el `module.py` del módulo especificado para obtener `DEPENDENCIES`.
2. Para cada dependencia, lee recursivamente su `module.py` y sus deps.
3. Cuando no hay más nodos sin visitar, importa `{dep_id}.models` de cada
   dependencia encontrada.

El grafo se recorre con un set `visited` para evitar ciclos. Los módulos que
no están en el árbol de dependencias **no se importan** — esto es deliberado
para evitar efectos secundarios de módulos no relacionados (decoradores,
registros de señales, etc.).

### `_read_dependencies(manifest)` (privado)

```python
def _read_dependencies(manifest: Path) -> list[str]:
```

Parsea `DEPENDENCIES` del `module.py` usando `ast.parse` **sin importar el
archivo**. Busca una asignación como:

```python
DEPENDENCIES = ["auth", "catalog"]
# o con anotación:
DEPENDENCIES: list[str] = ["auth", "catalog"]
```

Y extrae los strings de la lista. Si el archivo tiene un error de sintaxis
o el campo no existe, devuelve `[]`. Esta decisión de usar AST en lugar de
`importlib` evita ejecutar código del módulo (que podría tener imports que
fallen en el contexto de la migración).

---

## Estructura de un módulo con migraciones

```
modules/notes/
└── migrations/
    ├── __init__.py
    ├── alembic.ini        ← (opcional) sobreescribe defaults
    ├── env.py             ← configuración de Alembic para este módulo
    └── versions/
        ├── 001_initial.py
        └── 002_add_tags.py
```

El `env.py` mínimo para un módulo:

```python
# modules/notes/migrations/env.py
from alembic import context
from hotframe.migrations.env_helpers import import_all_app_models

import_all_app_models()  # resuelve FKs hacia apps/

from notes import models as _  # registra tablas de este módulo

config = context.config
version_table = (
    config.attributes.get("version_table")
    or config.get_main_option("version_table")
    or f"alembic_notes"
)

# Configuración offline / online según el modo de Alembic
def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        version_table=version_table,
        target_metadata=_.Base.metadata,
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            version_table=version_table,
            target_metadata=_.Base.metadata,
        )
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

El truco clave: `config.attributes.get("connection")` — el `ModuleMigrationRunner`
inyecta el engine en `config.attributes["connection"]` antes de llamar
`command.upgrade`. El `env.py` solo tiene que leerlo.

---

## Ciclo de vida completo de las migraciones de un módulo

```
hf modules install notes
  └── ModuleRuntime.install()
        └── [copia archivos, registra en DB]

hf modules activate notes
  └── ModuleRuntime.activate()
        ├── importa el paquete Python del módulo
        ├── monta rutas en /m/notes/
        └── ModuleMigrationRunner.upgrade(
                module_id="notes",
                module_path=Path(".../modules/notes"),
                db_url="postgresql://..."   # síncrona
            )
            └── asyncio.to_thread(_run_upgrade)
                  └── alembic upgrade head (en threadpool)
                      → crea tabla notes, aplica revisiones pendientes

hf modules uninstall notes
  └── ModuleRuntime.uninstall()
        └── ModuleMigrationRunner.downgrade(
                module_id="notes",
                module_path=...,
                db_url=...
            )
            └── alembic downgrade base
                → elimina todas las tablas del módulo
```

---

## Cómo encaja con el resto del framework

### CLI `hf migrate`

El comando `hf migrate` usa `MultiNamespaceRunner` para correr las
migraciones de core + todas las apps. Las migraciones de módulos no las
corre este comando — cada módulo las corre automáticamente al activarse
con `ModuleMigrationRunner`.

```bash
hf migrate              # corre core + apps, upgrade head
hf migrate --namespace auth   # solo el namespace auth
```

### CLI `hf makemigrations <app>`

Configura Alembic para el namespace de la app, llama `import_all_app_models()`
y `import_module_dependencies()` (si es un módulo), y lanza
`alembic revision --autogenerate`.

### `ModuleRuntime` (engine/module_runtime.py)

Importa `ModuleMigrationRunner` y lo invoca en los hooks de ciclo de vida:

- `activate` → `runner.upgrade(module_id, module_path, sync_url)`
- `uninstall` → `runner.downgrade(module_id, module_path, sync_url)`

La URL síncrona la obtiene con `ModuleMigrationRunner.get_sync_db_url(settings.DATABASE_URL)`.

### SQLAlchemy engine (async)

Las migraciones usan sqlalchemy síncrono (`create_engine`, no
`create_async_engine`). Esto es correcto — Alembic no soporta async.
El engine de migraciones usa `NullPool` para no interferir con el pool
async de la aplicación.

---

## Gotchas y decisiones de diseño

### 1. `asyncio.to_thread` para Alembic

Alembic es síncrono y bloqueante. Llamarlo directamente en un handler
async bloquearía el event loop. `asyncio.to_thread` lo ejecuta en el
threadpool del sistema, de forma que el loop puede continuar procesando
otras peticiones mientras las migraciones corren. El coste es que la
migración bloquea un thread del pool durante su ejecución.

### 2. El módulo puede tener `alembic.ini` propio o no

`_build_config` detecta si existe `migrations/alembic.ini`. Si existe, lo
usa como base (permite sobreescribir logging, etc.). Si no existe, crea un
`Config()` vacío y define todo programáticamente. Los valores que el runner
necesita (url, script_location, version_table) siempre se sobreescriben
después, así que un `alembic.ini` existente no puede interferir con ellos.

### 3. `DEPENDENCIES` se parsea con AST, nunca se importa

Importar `module.py` para leer sus dependencias tendría efectos secundarios
(ejecutaría decoradores, registraría señales, cargaría modelos). El uso de
`ast.parse` es un tradeoff: solo funciona con literales simples en
`DEPENDENCIES = [...]`, pero eso es suficiente — la convención del framework
es que `DEPENDENCIES` sea siempre una lista de strings literales.

### 4. Cada módulo tiene su propia tabla de versiones, no un campo en la tabla

La alternativa (una sola tabla `alembic_version` con una columna adicional
`namespace`) haría más fácil la consulta pero requeriría parches a Alembic.
La solución de tablas separadas es puro Alembic estándar — cada namespace es
una instancia de Alembic completamente independiente.

### 5. `MultiNamespaceRunner` es síncrono, `ModuleMigrationRunner` es async

El `MultiNamespaceRunner` se usa desde el CLI (`hf migrate`), que corre
en un proceso síncrono fuera del event loop. Por eso sus métodos son `def`,
no `async def`. El `ModuleMigrationRunner` necesita ser async porque lo
invoca el `ModuleRuntime` desde dentro del event loop de la aplicación.

### 6. `downgrade base` borra todo el historial del módulo

Al desinstalar, `ModuleMigrationRunner.downgrade` ejecuta `alembic downgrade base`,
que aplica todos los downgrade scripts en orden inverso. Si el módulo no
implementó los downgrade scripts correctamente, la desinstalación puede
fallar o dejar la DB en estado inconsistente. La convención del framework
es que los módulos siempre implementen los pasos de downgrade.
