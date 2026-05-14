# 2. Auto-descubrimiento por convención (`discovery/`)

> `discovery/` es el escáner de filesystem del framework: dado un directorio, detecta qué archivos existen, los importa en orden determinista y devuelve una descripción estructurada de lo que encontró, sin montar nada. La orquestación (montaje de rutas, registro en el AppRegistry) la hace la capa superior.

---

## Para qué sirve esta carpeta

Cuando hotframe arranca, necesita saber qué apps y módulos existen en disco, qué archivos tienen, y cuáles de esos archivos importar. `discovery/` resuelve exactamente eso con dos piezas:

1. **`conventions.py`** — la tabla de verdad que define qué ficheros son "convencionales" y qué rol cumple cada uno.
2. **`scanner.py`** — el motor que recorre el directorio aplicando esas convenciones, importa los módulos Python correspondientes y devuelve una lista de `DiscoveryResult`.

La separación es importante: si las convenciones estuvieran hardcodeadas dentro del scanner, serían invisibles y difíciles de testear. Al tenerlas como datos en `conventions.py`, son documentables, reemplazables y pueden inspeccionarse en runtime.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`__init__.py`](../src/hotframe/discovery/__init__.py) | Docstring-only. El paquete no re-exporta nada; los consumers importan directamente desde `scanner`. |
| [`conventions.py`](../src/hotframe/discovery/conventions.py) | Define `Kind`, `Convention` y `APP_CONVENTIONS`: la tabla que mapea nombre de archivo → rol semántico. |
| [`scanner.py`](../src/hotframe/discovery/scanner.py) | Implementa `scan()`, `_scan_subdir()` y `find_entry_config()`. Produce `DiscoveryResult` por cada subdirectorio encontrado. |

---

## `conventions.py` — La tabla de verdad

### Enum `Kind`

```python
class Kind(str, Enum):
    ENTRY_POINT = "entry_point"
    MODELS      = "models"
    ROUTES      = "routes"
    API         = "api"
    SCHEMAS     = "schemas"
    SERVICES    = "services"
    REPOSITORY  = "repository"
    SIGNALS     = "signals"
    MIGRATIONS  = "migrations"
    TEMPLATES   = "templates"
    STATIC      = "static"
    LOCALES     = "locales"
    TESTS       = "tests"
    MANAGEMENT  = "management"
```

Cada valor representa un rol semántico dentro de una app o módulo. El scanner asigna un `Kind` a cada artefacto que detecta; las capas superiores consultan el `Kind` para saber qué hacer con él (montar rutas, registrar señales, etc.).

`Kind` hereda de `str` para que sea serializable directamente a JSON y comparable con literales string cuando conviene.

---

### Dataclass `Convention`

```python
@dataclass(frozen=True, slots=True)
class Convention:
    filename_or_dir: str          # e.g. "models.py" o "templates"
    kind: Kind
    is_directory: bool = False
    optional: bool = True         # si False, su ausencia es un error
    required_exports: tuple[str, ...] = ()
```

`required_exports` implementa semántica **al-menos-uno-de**: si el campo no está vacío, el módulo importado debe exponer al menos uno de los nombres listados. Esto permite que una misma convención acepte varias formas históricas:

- `routes.py` acepta tanto `urlpatterns` (estilo Django) como `router` (estilo FastAPI).
- `api.py` acepta tanto `router` como `api_router` (alias legacy).

Si ningún nombre está presente, el scanner lanza `DiscoveryError`.

---

### Tupla `APP_CONVENTIONS`

La tabla completa, en orden de declaración (solo afecta al logging, no a la lógica):

| `filename_or_dir` | `kind` | `is_directory` | `required_exports` |
|---|---|---|---|
| `app.py` | `ENTRY_POINT` | No | — |
| `module.py` | `ENTRY_POINT` | No | — |
| `models.py` | `MODELS` | No | — |
| `routes.py` | `ROUTES` | No | `("urlpatterns", "router")` |
| `api.py` | `API` | No | `("router", "api_router")` |
| `schemas.py` | `SCHEMAS` | No | — |
| `services.py` | `SERVICES` | No | — |
| `repository.py` | `REPOSITORY` | No | — |
| `signals.py` | `SIGNALS` | No | — |
| `migrations` | `MIGRATIONS` | Sí | — |
| `templates` | `TEMPLATES` | Sí | — |
| `static` | `STATIC` | Sí | — |
| `locales` | `LOCALES` | Sí | — |
| `tests` | `TESTS` | Sí | — |
| `management` | `MANAGEMENT` | Sí | — |

Todos los items son `optional=True`. No hay ningún archivo obligatorio salvo la restricción XOR sobre `app.py` / `module.py` que el scanner implementa explícitamente.

---

### Función auxiliar `conventions_by_kind()`

```python
def conventions_by_kind() -> dict[Kind, tuple[Convention, ...]]:
```

Agrupa la tabla `APP_CONVENTIONS` por `Kind`. Útil cuando una capa superior quiere consultar "¿qué convenciones corresponden a `Kind.TEMPLATES`?" sin iterar la tabla completa.

---

## `scanner.py` — El motor de descubrimiento

### Clase `DiscoveryError`

```python
class DiscoveryError(Exception):
```

Se lanza cuando un directorio viola las convenciones: tiene tanto `app.py` como `module.py`, o un archivo con `required_exports` no exporta ninguno de los nombres esperados. Es una excepción de programación (la estructura del proyecto es incorrecta), no de runtime.

---

### Dataclass `FileArtifact`

```python
@dataclass(slots=True)
class FileArtifact:
    convention: Convention
    path: Path
    imported_module: ModuleType | None = None
```

Representa un archivo o directorio detectado dentro de una app. `imported_module` se rellena solo cuando `import_side_effects=True` y la importación tiene éxito. Si falla, el error queda en `DiscoveryResult.errors`.

---

### Dataclass `DiscoveryResult`

```python
@dataclass(slots=True)
class DiscoveryResult:
    name: str            # e.g. "accounts"
    root_path: Path      # e.g. /path/to/apps/accounts
    package_name: str    # e.g. "apps.accounts"
    entry_point: FileArtifact | None = None
    artifacts: list[FileArtifact] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
```

El `entry_point` (`app.py` o `module.py`) se almacena separado de los `artifacts` porque tiene semántica especial: es el que contiene el `AppConfig` o `ModuleConfig`. El resto de artefactos (modelos, rutas, señales...) van en la lista `artifacts`.

#### Método `find(kind: Kind) -> FileArtifact | None`

```python
def find(self, kind: Kind) -> FileArtifact | None:
    if kind == Kind.ENTRY_POINT:
        return self.entry_point
    for a in self.artifacts:
        if a.convention.kind == kind:
            return a
    return None
```

Atajo para consultar si un artefacto de un `Kind` concreto está presente. Por ejemplo, para saber si una app tiene migraciones:

```python
result = scan(apps_dir, package_prefix="apps")[0]
if result.find(Kind.MIGRATIONS):
    print("tiene migrations/")
```

#### Propiedad `has_entry_point`

```python
@property
def has_entry_point(self) -> bool:
    return self.entry_point is not None
```

---

### Constante `_SKIP_DIRS`

```python
_SKIP_DIRS: frozenset[str] = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".git",
})
```

Directorios que siempre se ignoran durante el escaneo. Además, cualquier directorio cuyo nombre empiece por `.` también se salta.

---

### Función `scan(root, *, package_prefix, import_side_effects=True) -> list[DiscoveryResult]`

```python
def scan(
    root: Path,
    *,
    package_prefix: str,
    import_side_effects: bool = True,
) -> list[DiscoveryResult]:
```

El punto de entrada principal. Itera los subdirectorios de `root` en orden alfabético (para garantizar determinismo) y llama a `_scan_subdir` por cada uno.

**Parámetros:**

- `root` — directorio raíz a escanear. Debe existir y ser un directorio; si no, lanza `DiscoveryError`.
- `package_prefix` — prefijo Python para construir los nombres de importación. Si `root` es `apps/` y `package_prefix="apps"`, los módulos se importarán como `apps.<nombre_app>.<archivo>`.
- `import_side_effects` — si `False`, el scanner solo recoge paths sin hacer ningún `import_module`. Útil en tests unitarios donde no quieres ejecutar código de usuario.

**Retorno:** lista de `DiscoveryResult`, uno por cada subdirectorio apto. Los subdirectorios en `_SKIP_DIRS` o que empiezan por `.` se omiten silenciosamente.

Ejemplo de uso (tomado de la guía del engine):

```python
from pathlib import Path
from hotframe.discovery.scanner import scan, Kind

results = scan(Path("apps"), package_prefix="apps")
for result in results:
    if result.errors:
        print(f"[{result.name}] errores: {result.errors}")
    routes = result.find(Kind.ROUTES)
    if routes and routes.imported_module:
        router = getattr(routes.imported_module, "router", None)
        # montar router en FastAPI...
```

---

### Función interna `_scan_subdir`

```python
def _scan_subdir(
    subdir: Path,
    *,
    package_prefix: str,
    import_side_effects: bool,
) -> DiscoveryResult:
```

Escanea un único subdirectorio. El flujo es:

1. Construye `package_name = f"{package_prefix}.{name}"`.
2. Verifica la restricción XOR: `app.py` y `module.py` no pueden coexistir. Si ambos existen, lanza `DiscoveryError` inmediatamente.
3. Itera `APP_CONVENTIONS` en orden:
   - Para convenciones de **directorio** (`is_directory=True`): si el directorio existe, crea un `FileArtifact` y lo añade a `artifacts`. No hay importación.
   - Para convenciones de **archivo**: si el archivo existe, intenta importarlo con `importlib.import_module(f"{package_name}.{stem}")`. Si falla, añade el error a `result.errors` (no lanza excepción, para no detener el arranque por un módulo roto). Si tiene `required_exports`, verifica que al menos uno esté presente; si no, lanza `DiscoveryError`.
4. Si el artefacto es de `Kind.ENTRY_POINT`, lo asigna a `result.entry_point`; si no, lo añade a `result.artifacts`.

Un punto sutil: los errores de importación se acumulan en `result.errors` pero no interrumpen el escaneo. La capa orquestadora (el engine) decide cómo tratar esos errores: puede loguear y continuar, o poner el módulo en estado `error`.

---

### Función `find_entry_config(result: DiscoveryResult) -> type`

```python
def find_entry_config(result: DiscoveryResult) -> Any:
```

Una vez que `scan()` ha importado el `entry_point` (el `app.py` o `module.py`), esta función extrae la clase `AppConfig` o `ModuleConfig` declarada en él.

La lógica:
1. Verifica que `result.entry_point` existe y que su módulo fue importado.
2. Importa `hotframe.apps.config` de forma diferida (para no crear una dependencia estática hacia una capa superior — ver "Decisiones de diseño").
3. Itera los miembros del módulo buscando clases que:
   - Estén definidas en ese módulo (no importadas de otro lado).
   - Hereden de `AppConfig`.
   - No sean `AppConfig` ni `ModuleConfig` ellas mismas (solo subclases concretas).
4. Si encuentra exactamente una candidata, la devuelve. Si encuentra cero o más de una, lanza `DiscoveryError`.

Esta función es la que permite al engine instanciar el config sin saber de antemano qué clase usa el proyecto:

```python
config_cls = find_entry_config(result)
config = config_cls()
await registry.register(config)
```

---

## Cómo encaja con el resto del framework

**Durante el bootstrap de apps estáticas**, `bootstrap.py` llama a `_auto_discover_apps(app)` que hace el mismo trabajo que `scan()` pero de forma más directa con `importlib` (el scanner formal es el que usa el engine para módulos dinámicos). Ambas rutas respetan las mismas convenciones definidas en `APP_CONVENTIONS`.

**Durante la activación de un módulo dinámico**, `engine/module_runtime.py` llama a `scan()` pasando `modules/` como `root` y `package_prefix="modules"`. El `DiscoveryResult` resultante le dice qué artefactos montar (rutas, señales, componentes, estáticos) y `find_entry_config()` le da la clase de config para registrar en el `AppRegistry`.

**En tests**, `import_side_effects=False` permite verificar que la estructura de directorios es correcta sin ejecutar ningún import. Esto hace los tests de estructura instantáneos.

---

## Gotchas y decisiones de diseño

**El scanner es una capa media deliberadamente.**
El comentario de `scanner.py` lo explicita: "este módulo NO debe importar `hotframe.apps` estáticamente porque `hotframe.apps` vive en una capa superior". Por eso `find_entry_config()` usa `importlib.import_module("hotframe.apps.config")` dentro del cuerpo de la función en vez de un import de nivel de módulo. Romper esa restricción crearía una dependencia circular.

**Los errores de import no detienen el arranque.**
`_scan_subdir` captura `ImportError` y `Exception` y los acumula en `result.errors` en vez de propagar la excepción. Esto es intencional: un módulo roto no debe impedir que el resto de la aplicación arranque. La capa orquestadora decide la política de error.

**La restricción XOR `app.py` / `module.py` es estricta.**
Si un directorio tiene ambos archivos, `DiscoveryError` se lanza inmediatamente. Esto previene ambigüedad sobre qué tipo de entidad es el directorio.

**El orden de escaneo es alfabético.**
`sorted(root.iterdir(), key=lambda p: p.name)` garantiza que el orden de descubrimiento es determinista en cualquier sistema de ficheros. Importante para que el orden de montaje de rutas sea predecible y repetible entre reinicios.

**`required_exports` es semántica al-menos-uno-de, no todos.**
Un `routes.py` que tenga tanto `urlpatterns` como `router` es válido (aunque inusual). Solo falla si no tiene ninguno. Esto da espacio de maniobra durante la transición entre estilos de API.
