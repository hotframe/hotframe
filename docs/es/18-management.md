# 18. El CLI (management/)

> `management/` es la capa de herramientas de línea de comandos de hotframe. Expone el ejecutable `hf` (y su alias `hotframe`) con el que un desarrollador interactúa con el proyecto durante todo su ciclo de vida: desde el andamiaje inicial hasta las migraciones de producción, pasando por el REPL interactivo y el ciclo de vida completo de los módulos dinámicos.

---

## Para qué sirve esta carpeta

`management/` encapsula toda la lógica de CLI. Su objetivo es que el desarrollador **nunca tenga que escribir scripts ad-hoc**: el mismo binario `hf` sirve para crear proyectos, generar andamiaje, migrar la base de datos, instalar/activar/desactivar módulos y abrir una consola de depuración. La filosofía es la misma que en Django o Rails: **convención sobre configuración**, y **un único punto de entrada** para administrar el proyecto.

La carpeta contiene exactamente dos archivos:

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`management/__init__.py`](../src/hotframe/management/__init__.py) | Módulo vacío con docstring de uso. Marca el paquete. |
| [`management/cli.py`](../src/hotframe/management/cli.py) | La totalidad del CLI (~1 640 líneas). Define cada subcomando `hf`, el grupo `hf modules`, y las funciones auxiliares de andamiaje y migraciones. |

---

## Arquitectura general del CLI

El CLI está construido sobre **[Typer](https://typer.tiangolo.com/)**, que a su vez envuelve Click. La app raíz se declara así:

```python
app = typer.Typer(
    name="hotframe",
    help="Hotframe — Modular Python web framework CLI.",
    no_args_is_help=True,
)
```

El flag `no_args_is_help=True` hace que `hf` sin argumentos muestre la ayuda, igual que `hf --help`. El grupo de subcomandos `hf modules` se crea con un segundo `Typer` que se adjunta al principal:

```python
modules_app = typer.Typer(help="Module lifecycle management.")
app.add_typer(modules_app, name="modules")
```

En `pyproject.toml` de hotframe se registra el entry point:

```toml
[project.scripts]
hf = "hotframe.management.cli:app"
hotframe = "hotframe.management.cli:app"
```

Con `pip install hotframe` ambos comandos quedan disponibles en el `PATH`.

---

## Función auxiliar: `_load_project_settings()`

**Firma:** `_load_project_settings() -> HotframeSettings`

Esta es la primera función que casi todos los subcomandos llaman. Su cometido es localizar el archivo `settings.py` del proyecto (no el de hotframe) e inyectarlo en el contexto global del framework.

```python
def _load_project_settings():
    import sys
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        import importlib
        mod = importlib.import_module("settings")
        project_settings = getattr(mod, "settings", None)
        if project_settings is not None:
            from hotframe.config.settings import set_settings
            set_settings(project_settings)
            return project_settings
    except ImportError:
        pass

    from hotframe.config.settings import get_settings
    return get_settings()
```

**Lo que hace, paso a paso:**

1. Añade el directorio de trabajo actual a `sys.path` si no estaba, para que `import settings` pueda encontrar el `settings.py` del proyecto.
2. Importa dinámicamente el módulo `settings` con `importlib.import_module`.
3. Si el módulo expone un atributo `settings` (el objeto `HotframeSettings`), lo registra globalmente con `set_settings()` y lo devuelve.
4. Si no hay `settings.py` en el directorio actual (p. ej., el desarrollador está fuera del proyecto), cae en el `except ImportError` y devuelve la configuración por defecto de hotframe.

**Gotcha importante:** `_load_project_settings()` depende del directorio de trabajo actual (`Path.cwd()`). Los subcomandos que tocan la base de datos deben ejecutarse **desde la raíz del proyecto**, o de lo contrario usarán la configuración por defecto de hotframe (con SQLite en memoria), lo que puede resultar confuso.

---

## `hf startproject`

**Firma:** `startproject(name: str) -> None`

Crea la estructura completa de un nuevo proyecto hotframe.

### Uso

```bash
hf startproject myapp         # crea directorio myapp/
hf startproject .             # crea en el directorio actual (debe estar vacío)
```

### Flags

Ninguno. El único argumento posicional es el nombre del proyecto.

### Caso especial: `name == "."`

Si el nombre es `.`, hotframe crea el proyecto en el directorio actual. Antes de hacerlo comprueba que el directorio esté "suficientemente vacío" — se permiten `.venv`, `pyproject.toml`, `uv.lock`, `.git`, `.gitignore`, `__pycache__` y `.python-version`. Si hay cualquier otro archivo o carpeta, el comando falla con un error claro:

```
Error: directory is not empty. Found: src, README.md
```

### Archivos que genera

| Ruta | Contenido |
|---|---|
| `main.py` | `create_app(settings)` — entrypoint para uvicorn |
| `asgi.py` | Re-exporta `app` desde `main` con comentario `# uvicorn asgi:app` |
| `settings.py` | Clase `Settings(HotframeSettings)` con todos los grupos de configuración comentados, lista para descomentar |
| `manage.py` | Delega a `hotframe.management.cli:app` — útil para proyectos que prefieren `python manage.py` |
| `.env` | `DATABASE_URL=sqlite+aiosqlite:///./app.db`, `SECRET_KEY`, `DEBUG=true` |
| `.gitignore` | Estándar Python + `.env`, `*.db` |
| `pyproject.toml` | Solo si no existe. Deps mínimas: `hotframe`. Devdeps: `pytest`, `pytest-asyncio`, `ruff`. Configura `asyncio_mode = "auto"` en pytest |
| `apps/__init__.py` | Vacío |
| `apps/shared/__init__.py` | Vacío |
| `apps/shared/app.py` | `SharedConfig(AppConfig)` |
| `apps/shared/routes.py` | Ruta `GET /` que sirve `shared/index.html` o HTML básico de fallback |
| `apps/shared/templates/shared/base.html` | Plantilla base completa con soporte para CSP nonce, Trusted Types, Iconify CDN, `live_assets()`, toast container con JS inline |
| `apps/shared/templates/shared/index.html` | Página de bienvenida que extiende `base.html` |
| `apps/shared/templates/errors/404.html` | Extiende `base.html` |
| `apps/shared/templates/errors/500.html` | Extiende `base.html` |
| `apps/shared/components/alert/template.html` | Componente de ejemplo: `{% component 'alert' type='warning' %}` |
| `apps/shared/components/badge/template.html` | Componente de ejemplo: `{{ render_component('badge', text='New') }}` |
| `modules/` | Carpeta vacía para módulos dinámicos |
| `tests/__init__.py` | Vacío |
| `tests/conftest.py` | Fixtures `app` y `db` listos para usar con `hotframe.testing` |

**Decisión de diseño:** hotframe genera `base.html` con el toast container y el JS inline desde el principio, porque casi toda aplicación real necesita notificaciones y es más útil tenerlo listo que tener que buscarlo en la documentación.

---

## `hf startapp`

**Firma:** `startapp(name: str) -> None`

Crea una nueva **app estática** dentro de `apps/`.

### Uso

```bash
hf startapp accounts
hf startapp billing
```

### Archivos que genera en `apps/<name>/`

| Ruta | Contenido |
|---|---|
| `__init__.py` | Vacío |
| `app.py` | `{Name}Config(AppConfig)` con `name`, `verbose_name` y `ready()` vacío |
| `models.py` | Importa `Base`, comentario para definir modelos |
| `routes.py` | `router = APIRouter(prefix="/{name}", tags=["{name}"])` |
| `api.py` | `api_router = APIRouter(prefix="/api/v1/{name}", tags=["{name}"])` |
| `templates/{name}/pages/` | Carpeta vacía |
| `templates/{name}/partials/` | Carpeta vacía |
| `migrations/versions/` | Carpeta vacía |
| `migrations/env.py` | Generado por `_generate_env_py(name)` |
| `migrations/script.py.mako` | Generado por `_generate_script_mako()` |
| `tests/__init__.py` | Vacío |

**Diferencia con `startmodule`:** las apps no tienen `module.py` (no son módulos dinámicos), no tienen `has_views`/`has_api` flags, y sus rutas se montan al arrancar el proceso, no en runtime.

---

## `hf startmodule`

**Firma:**
```python
startmodule(
    name: str,
    api_only: bool = typer.Option(False, "--api-only", ...),
    system: bool  = typer.Option(False, "--system", ...),
) -> None
```

Crea un nuevo **módulo dinámico** dentro de `modules/`.

### Uso

```bash
hf startmodule blog                  # vistas + API (default)
hf startmodule payments --api-only   # solo API, sin vistas HTML
hf startmodule audit --system        # módulo de sistema (is_system=True)
```

### Lógica de flags

```python
has_views = not api_only and not system
has_api   = not system
```

Un módulo `--system` se asume sin vistas ni API propias (solo infraestructura del framework, no desinstalable por el usuario). Un módulo `--api-only` tiene API pero no plantillas ni `routes.py`.

### Archivos que genera en `modules/<name>/`

| Ruta | Condición | Contenido |
|---|---|---|
| `__init__.py` | siempre | Vacío |
| `module.py` | siempre | Clase `{Name}Module(ModuleConfig)` con `name`, `verbose_name`, `version`, `is_system`, `has_views`, `has_api`, `requires_restart`, `dependencies`, `ready()`, `install()`, `uninstall()` |
| `models.py` | siempre | Importa `Base` |
| `routes.py` | si `has_views` | Router con `GET /m/{name}/` que renderiza `{name}/pages/index.html` |
| `templates/{name}/pages/index.html` | si `has_views` | Extiende `shared/base.html`, muestra nombre del módulo |
| `templates/{name}/partials/` | si `has_views` | Carpeta vacía |
| `api.py` | si `has_api` | `api_router` con `GET /api/v1/{name}/` que devuelve `{"module": name, "items": []}` |
| `migrations/versions/` | siempre | Carpeta vacía |
| `migrations/env.py` | siempre | Generado por `_generate_env_py(name)` |
| `migrations/script.py.mako` | siempre | Generado por `_generate_script_mako()` |
| `tests/__init__.py` | siempre | Vacío |

La salida del comando indica el modo creado:

```
Created module 'modules/blog/' (views + API)
Created module 'modules/payments/' (API)
Created module 'modules/audit/' (system)
```

---

## `hf runserver`

**Firma:**
```python
runserver(
    host: str  = "0.0.0.0",
    port: int  = 8000,
    reload: bool = True,
) -> None
```

Arranca uvicorn apuntando a `main:app` con recarga automática habilitada por defecto.

### Uso

```bash
hf runserver
hf runserver --host 127.0.0.1 --port 9000
hf runserver --no-reload
```

### Implementación

```python
import uvicorn

cwd = str(Path.cwd())
if cwd not in sys.path:
    sys.path.insert(0, cwd)

uvicorn.run("main:app", host=host, port=port, reload=reload)
```

Añade el cwd a `sys.path` antes de llamar a uvicorn para que el `import main` funcione sin necesidad de instalar el proyecto como paquete. El reload se basa en el propio mecanismo de uvicorn (inotify/FSEvents), **no** en el `ModuleWatcher` de `dev/autoreload.py` (que es distinto y opera a nivel de módulos dinámicos).

**Gotcha:** En producción, no usar `hf runserver`. Usar directamente `uvicorn asgi:app --workers N` o gunicorn con worker class uvicorn.

---

## `hf migrate`

**Firma:**
```python
migrate(
    name: str = typer.Argument(None, help="App o módulo. Omitir para migrar todo.")
) -> None
```

Ejecuta las migraciones Alembic pendientes de todas las apps y módulos (o de uno concreto).

### Uso

```bash
hf migrate                 # todas las apps/ + modules/
hf migrate accounts        # solo apps/accounts/
hf migrate sales           # solo modules/sales/
```

### Flujo interno

1. Carga settings con `_load_project_settings()`.
2. Instancia `ModuleMigrationRunner` (de `hotframe.migrations.runner`).
3. Obtiene la URL de BD síncrona con `runner.get_sync_db_url(settings.DATABASE_URL)` — convierte `+asyncpg` o `+aiosqlite` a sync.
4. Si `name` está especificado, busca en `apps/<name>` y luego en `modules/<name>`.
5. Si `name` no está especificado:
   - Recoge todas las apps con carpeta `migrations/` en `apps/`.
   - Recoge todos los módulos con `migrations/` en `modules/` y los **ordena topológicamente** con `_topo_sort_modules()`.
6. Para cada target, si `runner.has_migrations(path)` es verdadero, llama a `await runner.upgrade(mid, mpath, db_url)`.

### Ordenación topológica de módulos

La función `_topo_sort_modules(module_targets)` resuelve el problema de las foreign keys entre módulos. Si el módulo `commissions` tiene una FK a la tabla del módulo `services`, sus migraciones deben ejecutarse **después** de las de `services`. El algoritmo de Kahn detecta ciclos y aborta con un mensaje claro.

`_extract_module_dependencies(module_path)` lee el `module.py` del módulo **sin importarlo** (usa `ast.parse`) para evitar efectos secundarios. Extrae la lista `DEPENDENCIES` (como literal, soporta tanto `DEPENDENCIES = [...]` como la forma anotada `DEPENDENCIES: list[str] = [...]`).

### Tabla de versiones por app/módulo

Cada app/módulo usa su propia tabla Alembic: `alembic_<name>` (p.ej. `alembic_accounts`, `alembic_sales`). Esto permite que las migraciones de distintos módulos sean completamente independientes y no colisionen en la misma tabla `alembic_version`.

---

## `hf makemigrations`

**Firma:**
```python
makemigrations(
    name: str = typer.Argument(..., help="App o módulo"),
    message: str = typer.Option("auto", "-m", "--message", help="Mensaje"),
) -> None
```

Genera una nueva revisión Alembic con autodetección de cambios en los modelos.

### Uso

```bash
hf makemigrations accounts
hf makemigrations accounts -m "add email field"
hf makemigrations sales -m "initial"
```

### Flujo interno

1. Busca el directorio en `apps/<name>` o `modules/<name>`.
2. Crea `migrations/`, `migrations/versions/`, `migrations/env.py` y `migrations/script.py.mako` si no existen.
3. Construye un `alembic.config.Config` en memoria (sin archivo `alembic.ini`):
   - `script_location` → la carpeta `migrations/` del app/módulo.
   - `sqlalchemy.url` → URL de BD síncrona (strip de `+asyncpg`/`+aiosqlite`).
   - `version_table` → `alembic_<name>`.
4. Llama a `command.revision(config, message=message, autogenerate=True)` dentro de `asyncio.to_thread` para no bloquear el event loop.

### Función auxiliar: `_generate_env_py(name: str) -> str`

Genera el texto completo del `env.py` de Alembic. Lo más relevante:

- Añade `parents[3]` al `sys.path` para que el `env.py` pueda importar los modelos del proyecto desde cualquier profundidad.
- Importa `Base` de `hotframe.models.base`.
- Intenta importar los modelos del app o módulo con `importlib`, probando primero `apps.<name>.models` y luego `modules.<name>.models`.
- Soporta **modo online con conexión inyectada** (para `ModuleMigrationRunner`): `config.attributes.get("connection")`. Si la conexión viene inyectada, la usa directamente en lugar de crear un engine nuevo.
- Usa `render_as_batch=True` para compatibilidad con SQLite (que no permite `ALTER TABLE` directo).

### Función auxiliar: `_generate_script_mako() -> str`

Genera el template Mako estándar para archivos de migración Alembic. Contiene los bloques `upgrade()` y `downgrade()` con variables de Alembic (`${up_revision}`, `${down_revision}`, etc.).

---

## `hf shell`

**Firma:**
```python
shell(
    no_startup: bool = typer.Option(False, "--no-startup", ...),
    settings_path: str = typer.Option("", "--settings", ...),
    plain: bool = typer.Option(False, "--plain", ...),
) -> None
```

Abre un REPL interactivo con la aplicación hotframe completamente inicializada.

### Uso

```bash
hf shell                              # IPython si disponible, con DB y registries
hf shell --plain                      # código.interact() en lugar de IPython
hf shell --no-startup                 # sin lifespan (sin DB, sin registries)
hf shell --settings myproject.settings  # settings explícitos
```

### Variables pre-inyectadas en el namespace

| Variable | Tipo | Descripción |
|---|---|---|
| `app` | `FastAPI` | La aplicación FastAPI completamente construida |
| `settings` | `HotframeSettings` | La instancia de settings del proyecto |
| `db` | `AsyncSession` | Sesión de base de datos abierta |
| `events` | `AsyncEventBus` | El bus de eventos de la app |
| `hooks` | `HookRegistry` | El registro de hooks |
| `slots` | `SlotRegistry` | El registro de slots |
| `runtime` | `ModuleRuntime` | El runtime de módulos dinámicos |
| `SlotEntry` | clase | Para crear entradas de slot manualmente |

Con `--no-startup` solo se inyectan `app` y `settings`.

### Flujo interno

1. `_resolve_shell_settings(settings_path)` carga el objeto settings (desde dotted path o auto-descubrimiento).
2. `create_app(settings_obj)` construye la app FastAPI.
3. Si no hay `--no-startup`, ejecuta el lifespan completo con `fastapi_app.router.lifespan_context(fastapi_app).__aenter__()`, que inicializa el engine de BD, las registries, el ModuleRuntime, etc.
4. Abre una sesión de BD con `get_session_factory()`.
5. Llama a `_launch_repl(namespace, version, plain, loop)`.

### REPL con IPython

Si IPython está instalado y `--plain` no está activo:

```python
from IPython.terminal.embed import InteractiveShellEmbed
shell_instance = InteractiveShellEmbed(banner1=banner, user_ns=namespace)
shell_instance.run_line_magic("autoawait", "asyncio")
shell_instance()
```

`%autoawait asyncio` permite usar `await` directamente en el prompt de IPython:

```python
In [1]: users = await db.execute(select(User))
```

### REPL con `code.interact()`

Si IPython no está disponible, se inyecta una función `run(coro)` extra:

```python
def run(coro):
    return loop.run_until_complete(coro)
```

Uso en el REPL:

```python
>>> users = run(db.execute(select(User)))
```

### Banner de inicio

```
Hotframe 1.0.0 shell (IPython)
Variables: app, settings, db, events, hooks, slots, runtime, SlotEntry
Tip: await works directly (autoawait asyncio).
```

### Limpieza garantizada

El bloque `finally` cierra la sesión de BD y ejecuta el lifespan `__aexit__` aunque el REPL se cierre con Ctrl+D, error, o señal.

---

## `hf modules list`

**Firma:** `modules_list() -> None`

Imprime una tabla con todos los módulos encontrados en `modules/`.

### Uso

```bash
hf modules list
```

### Salida

```
Module               Status       Version    Views  API
------------------------------------------------------------
blog                 available    1.0.0      yes    yes
payments             available    2.1.0      no     yes
audit                available (system) 1.0.0  no   no
```

### Implementación

Itera `modules/` buscando carpetas con `module.py`. Para cada una, importa dinámicamente el módulo y busca la clase cuyo atributo `name` coincide con el nombre del directorio. Extrae `version`, `has_views`, `has_api` e `is_system`. Si la importación falla (módulo con error de sintaxis, dependencias no satisfechas), muestra el módulo con campos vacíos sin abortar.

**Gotcha:** `modules list` importa el `module.py` (no usa AST como `_extract_module_dependencies`), así que tiene efectos secundarios de importación. Si el módulo tiene código de nivel de módulo que falla, la columna de estado simplemente aparece vacía.

**Limitación en v1.0:** `modules list` no consulta la base de datos, por lo que no puede mostrar el estado real (instalado/activo/inactivo). Solo detecta qué módulos existen en el sistema de ficheros y muestra `"available"` para todos.

---

## `hf modules install`

**Firma:** `modules_install(source: str) -> None`

Instala un módulo desde una fuente (nombre de módulo local, `.zip`, URL o marketplace).

### Uso

```bash
hf modules install shop
hf modules install /tmp/shop-1.2.0.zip
hf modules install https://marketplace.hotframe.dev/shop-1.2.0.zip
```

### Flujo interno

```python
async def _install():
    settings = _load_project_settings()
    runtime = ModuleRuntime(
        app=None, settings=settings,
        event_bus=AsyncEventBus(), hooks=HookRegistry(), slots=SlotRegistry()
    )
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # crea tablas si no existen
    async with factory() as session:
        result = await runtime.install(session, hub_id=None, module_id=source, source=source)
        ...
```

Crea un `ModuleRuntime` "headless" (sin `FastAPI` app, `app=None`) porque en el CLI no hay servidor en ejecución. Crea las tablas de metadatos de hotframe antes de intentar la instalación. El resultado es un objeto con `result.success`, `result.module_id` y `result.version`.

---

## `hf modules update`

**Firma:** `modules_update(source: str) -> None`

Actualiza un módulo a una nueva versión. Delega en `runtime.update(session, hub_id=None, module_id=source, new_version=None, source=source)`. La lógica de backup y rollback está en `ModuleRuntime`, no en el CLI.

### Uso

```bash
hf modules update shop
hf modules update /tmp/shop-2.0.0.zip
```

---

## `hf modules activate`

**Firma:** `modules_activate(name: str) -> None`

Activa un módulo que estaba desactivado. Llama a `runtime.activate(session, hub_id=None, module_id=name)`.

**Nota:** En el CLI (fuera del servidor), activar significa actualizar el estado en la base de datos. Las rutas no se montan en ningún proceso hasta que el servidor arranque y ejecute su lifespan.

### Uso

```bash
hf modules activate shop
```

---

## `hf modules deactivate`

**Firma:** `modules_deactivate(name: str) -> None`

Desactiva un módulo sin borrar sus datos. Llama a `runtime.deactivate(session, hub_id=None, module_id=name)`.

### Uso

```bash
hf modules deactivate shop
```

---

## `hf modules uninstall`

**Firma:**
```python
modules_uninstall(
    name: str,
    keep_data: bool = typer.Option(False, "--keep-data", help="Conservar tablas"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Omitir confirmación"),
) -> None
```

Desinstala un módulo. Por defecto pide confirmación interactiva.

### Uso

```bash
hf modules uninstall shop
hf modules uninstall shop --keep-data
hf modules uninstall shop -y                 # sin confirmación (CI/CD)
hf modules uninstall shop --keep-data -y
```

### Confirmación interactiva

```
Uninstall module 'shop'? (including database tables) [y/N]:
```

Con `--keep-data` el mensaje cambia a `(keeping data)`. Si el usuario responde "no", imprime `Cancelled.` y sale con código 0.

Delega en `runtime.uninstall(session, hub_id=None, module_id=name)`.

---

## `hf version`

**Firma:** `version() -> None`

Imprime la versión instalada de hotframe.

### Uso

```bash
hf version
# → hotframe 1.0.0
```

Importa `__version__` desde el paquete `hotframe`.

---

## Funciones auxiliares de generación de código

### `_generate_env_py(name: str) -> str`

Genera el contenido completo del `env.py` de Alembic para un app o módulo. Características:

- Añade `parents[3]` (raíz del proyecto) a `sys.path`.
- Importa `hotframe.models.base.Base`.
- Importa los modelos del app/módulo con `importlib`, con fallback silencioso si el módulo no se puede importar.
- Soporta modo offline (`context.is_offline_mode()`).
- Soporta conexión inyectada (`config.attributes.get("connection")`).
- Usa `render_as_batch=True` para compatibilidad con SQLite.
- Usa `compare_type=True` para detectar cambios de tipo en columnas.

### `_generate_script_mako() -> str`

Genera el template Mako estándar de Alembic. Contiene el encabezado con `Revision ID`, `Revises` y `Create Date`, y los bloques `upgrade()` y `downgrade()`.

### `_topo_sort_modules(module_targets: list[tuple[str, Path]]) -> list[tuple[str, Path]]`

Ordena los módulos con el algoritmo de Kahn para respetar las dependencias declaradas en `module.py`. Ignora dependencias que apuntan a módulos no presentes en la lista (opcionales, eliminados o referenciando apps). Detecta ciclos y aborta.

### `_extract_module_dependencies(module_path: Path) -> list[str]`

Lee `module.py` sin importarlo (usando `ast.parse`) y extrae la lista `DEPENDENCIES`. Si el archivo no existe, tiene error de sintaxis, o no tiene la variable `DEPENDENCIES`, devuelve `[]`.

---

## Cómo encaja con el resto del framework

| Qué hace el CLI | Con qué componente interactúa |
|---|---|
| `migrate` / `makemigrations` | `hotframe.migrations.runner.ModuleMigrationRunner` |
| `modules install/activate/deactivate/uninstall/update` | `hotframe.engine.module_runtime.ModuleRuntime` |
| `shell` | `hotframe.bootstrap.create_app`, `hotframe.config.database.get_session_factory`, `hotframe.templating.slots.SlotEntry` |
| `runserver` | `uvicorn` directamente, apuntando a `main:app` |
| `startproject` / `startapp` / `startmodule` | Sistema de ficheros + templates en línea; crea archivos que el bootstrap (`create_app`) descubrirá al arrancar |
| `_load_project_settings()` | `hotframe.config.settings.set_settings` / `get_settings` |

---

## Gotchas y decisiones de diseño

**1. `ModuleRuntime` headless para comandos de módulos.**
Los comandos `modules install/activate/deactivate/uninstall/update` instancian `ModuleRuntime(app=None, ...)`. Esto significa que el runtime no puede montar rutas (no hay app FastAPI), pero sí puede modificar la base de datos y el sistema de ficheros. Las rutas se montarán cuando el servidor arranque su lifespan.

**2. URL síncrona en migraciones.**
Alembic no soporta drivers asíncronos. `makemigrations` y `migrate` convierten la URL async (`+asyncpg`, `+aiosqlite`) a sync quitando el sufijo. Esto requiere que los drivers síncronos estén instalados (`psycopg2`, `aiosqlite` en su modo sync).

**3. `startproject .` borra campos preexistentes.**
La lista de archivos permitidos es fija. Si tienes un archivo no listado (p.ej. `README.md`), el comando falla. Esto es deliberado: evita sobreescribir proyectos existentes por error.

**4. Sin `INSTALLED_APPS` en `settings.py` generado.**
`startproject` genera `settings.py` sin `INSTALLED_APPS`. Las apps se descubren automáticamente escaneando `apps/`. Solo necesitas `INSTALLED_APPS` si quieres restringir el descubrimiento.

**5. El `pyproject.toml` no se sobreescribe.**
Si `pyproject.toml` ya existe (por haber creado el proyecto con `uv init`), `startproject` lo respeta y no lo sobreescribe. Esto permite usar `hf startproject .` en proyectos `uv` sin perder las dependencias declaradas.

**6. `hf shell` ejecuta el lifespan completo.**
A diferencia del shell de Django (que solo importa el código), `hf shell` ejecuta el startup completo del servidor: inicializa el engine de BD, las registries y el ModuleRuntime. Esto hace que el shell sea más fiel a producción pero más lento de arrancar.

**7. Dos debounces distintos.**
`runserver` usa el debounce de uvicorn para recargar el proceso. `dev/autoreload.py` usa un debounce de 300ms con `watchfiles` para hacer hot-reload de módulos individuales. Son mecanismos independientes que operan a distintos niveles de granularidad.