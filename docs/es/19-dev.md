# 19. Autoreload en desarrollo (dev/)

> `dev/` es el subpaquete de hotframe que añade capacidades de desarrollo que no tienen cabida en producción. Su pieza central es `ModuleWatcher`: un observador de ficheros que detecta cambios en los módulos dinámicos y los recarga en caliente **sin reiniciar el proceso**. En producción este subpaquete es un no-op completo.

---

## Para qué sirve esta carpeta

Cuando `DEBUG=True`, hotframe puede activar un watcher que monitoriza el directorio `modules/` en busca de cambios. Cada vez que el código de un módulo activo cambia, el watcher llama al callback de hot-reload del `ModuleRuntime`, que desmonta el módulo, limpia `sys.modules` y lo vuelve a montar — todo dentro del mismo proceso uvicorn ya en ejecución.

Esto es distinto del reload de uvicorn (que mata y relanza el proceso entero): el hot-reload de módulos de hotframe es **quirúrgico**, afecta solo al módulo cuyo código cambió y deja los demás intactos.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`dev/__init__.py`](../src/hotframe/dev/__init__.py) | Docstring con la descripción del subpaquete y los exports clave. No importa nada al nivel de módulo — diseño lazy. |
| [`dev/autoreload.py`](../src/hotframe/dev/autoreload.py) | La clase `ModuleWatcher` con el bucle de observación asíncrono. |

---

## `dev/__init__.py` — diseño lazy

El `__init__.py` del subpaquete **no importa** `ModuleWatcher` directamente. Solo documenta el paquete con su docstring. La importación se hace cuando el código la necesita:

```python
from hotframe.dev.autoreload import ModuleWatcher
```

Esto es intencional: si `watchfiles` no está instalado, importar `hotframe.dev` no falla. Solo falla cuando se intenta usar `ModuleWatcher._watch_loop`, que es cuando se necesita realmente.

---

## `ModuleWatcher`

**Ubicación:** [`dev/autoreload.py`](../src/hotframe/dev/autoreload.py)

### Propósito

Observa recursivamente un directorio `modules/` en busca de cambios en archivos `.py`, `.html`, `.json` y `.jinja2`. Cuando detecta un cambio, identifica qué módulo está afectado y llama a un callback con el `module_id`.

### Atributos de clase

```python
WATCH_EXTENSIONS = frozenset({".py", ".html", ".json", ".jinja2"})
```

Solo se procesan cambios en estos tipos de archivo. Cambios en `.pyc`, `.db`, `.lock` u otros se ignoran silenciosamente.

### Estado de instancia

```python
def __init__(self) -> None:
    self._task: asyncio.Task | None = None
    self._stop_event: asyncio.Event = asyncio.Event()
```

- `_task`: la tarea asyncio del bucle de observación. `None` cuando el watcher está parado.
- `_stop_event`: evento asyncio que se activa al llamar a `stop()`. Se pasa a `watchfiles.awatch()` como señal de parada limpia.

### `start(modules_dir, on_change)`

**Firma:**
```python
async def start(
    self,
    modules_dir: Path,
    on_change: Callable[[str], object],
) -> None
```

Arranca el bucle de observación en segundo plano como tarea asyncio.

**Argumentos:**
- `modules_dir`: ruta al directorio raíz de módulos (típicamente `<project>/modules/`).
- `on_change`: callable que recibe el `module_id` del módulo que cambió. Puede ser síncrono o una corrutina — el `_watch_loop` detecta el tipo con `asyncio.iscoroutine()` y hace `await` si es necesario.

**Comportamiento de idempotencia:** si el watcher ya está corriendo, registra un warning y retorna sin iniciar una segunda tarea.

```python
if self._task is not None:
    logger.warning("ModuleWatcher already running")
    return
```

### `stop()`

**Firma:** `async def stop(self) -> None`

Para el watcher de forma limpia:

1. Activa `_stop_event` — `watchfiles.awatch()` lo detecta y termina su generador.
2. Cancela la tarea asyncio `_task`.
3. Espera a que la tarea termine, absorbiendo `CancelledError`.
4. Resetea `_task = None`.

### `is_running` (propiedad)

```python
@property
def is_running(self) -> bool:
    return self._task is not None and not self._task.done()
```

Útil para health checks o lógica de guardas.

### `_watch_loop(modules_dir, on_change)` — implementación interna

**Firma:**
```python
async def _watch_loop(
    self,
    modules_dir: Path,
    on_change: Callable[[str], object],
) -> None
```

Este es el corazón del watcher. Su estructura:

```python
try:
    from watchfiles import awatch
except ImportError:
    logger.warning("watchfiles not installed — hot-reload disabled. ...")
    return
```

Si `watchfiles` no está instalado, el watcher se deshabilita silenciosamente con un warning en el log. **No lanza excepción** — esto es deliberado para que el framework no falle en entornos mínimos sin `watchfiles`.

El bucle principal:

```python
debounce_ms = 300
recently_reloaded: dict[str, float] = {}

async for changes in awatch(
    modules_dir,
    stop_event=self._stop_event,
    debounce=debounce_ms,
    recursive=True,
):
    ...
```

`watchfiles.awatch()` es un generador asíncrono que usa mecanismos nativos del OS (FSEvents en macOS, inotify en Linux, ReadDirectoryChangesW en Windows) para notificar cambios. El parámetro `debounce=300` hace que `awatch` acumule cambios durante 300ms antes de emitirlos como un batch — evita múltiples reloads cuando un editor guarda varios ficheros a la vez.

### Deduplicación y debounce doble

Dentro del bucle, hotframe aplica su propia capa de deduplicación **encima** del debounce de watchfiles:

```python
changed_modules: set[str] = set()

for _change_type, changed_path in changes:
    path = Path(changed_path)
    if path.suffix not in self.WATCH_EXTENSIONS:
        continue
    module_id = self._extract_module_id(modules_dir, path)
    if module_id:
        changed_modules.add(module_id)

now = asyncio.get_event_loop().time()
for module_id in changed_modules:
    last = recently_reloaded.get(module_id, 0)
    if now - last < 1.0:  # debounce: skip si recargado hace menos de 1s
        continue
    recently_reloaded[module_id] = now
    ...
    result = on_change(module_id)
    if asyncio.iscoroutine(result):
        await result
```

Hay dos niveles de debounce:

1. **watchfiles (300ms):** acumula los cambios del OS en un batch antes de emitirlos.
2. **`recently_reloaded` (1s):** hotframe evita recargar el mismo módulo más de una vez por segundo. Esto protege contra editores que hacen múltiples escrituras (backup file, swap file, escritura final).

Los errores en el callback `on_change` se capturan con `logger.exception` y no abortan el bucle — el watcher sigue observando cambios.

### `_extract_module_id(modules_dir, changed_path)` — método estático

**Firma:**
```python
@staticmethod
def _extract_module_id(modules_dir: Path, changed_path: Path) -> str | None
```

Extrae el `module_id` a partir de la ruta absoluta del fichero cambiado:

```
modules_dir  = /home/user/myapp/modules
changed_path = /home/user/myapp/modules/inventory/routes.py
  → relative = inventory/routes.py
  → parts[0] = "inventory"
  → return "inventory"
```

Si `changed_path` no está bajo `modules_dir` (p.ej. un evento espurio), captura `ValueError` y retorna `None`.

---

## Cómo integrar `ModuleWatcher` en el bootstrap

El watcher no se arranca automáticamente. El bootstrap de hotframe lo integra durante el lifespan, condicionado a `DEBUG`:

```python
# Patrón de uso en hotframe.bootstrap (lifespan)
if settings.DEBUG:
    from hotframe.dev.autoreload import ModuleWatcher
    watcher = ModuleWatcher()
    await watcher.start(
        modules_dir=Path(settings.MODULES_DIR),
        on_change=module_runtime.hot_reload,  # método del ModuleRuntime
    )
```

En el shutdown del lifespan:

```python
if settings.DEBUG and watcher.is_running:
    await watcher.stop()
```

El `on_change` típico es `module_runtime.hot_reload`, que es una corrutina: desmonta el módulo con `deactivate`, limpia `sys.modules`, y lo vuelve a activar con `activate`.

---

## Ciclo completo de una edición de fichero

1. El desarrollador guarda `modules/inventory/routes.py` en su editor.
2. El OS emite un evento de cambio a través de FSEvents/inotify.
3. `watchfiles.awatch()` acumula el evento durante 300ms y lo emite como `{(ChangeType.modified, "/path/to/inventory/routes.py")}`.
4. `_watch_loop` recibe el batch, extrae `module_id = "inventory"`.
5. Comprueba que `.py` está en `WATCH_EXTENSIONS` — sí.
6. Comprueba que no se recargó hace menos de 1s — OK.
7. Llama a `on_change("inventory")` — en la práctica `module_runtime.hot_reload("inventory")`.
8. `hot_reload` desmonta el módulo: limpia rutas de FastAPI, limpia `sys.modules["modules.inventory"]` y submódulos, libera memoria.
9. `hot_reload` vuelve a activar el módulo: importa el paquete fresco, monta rutas, registra eventos/hooks/slots, ejecuta `ready()`.
10. La próxima petición HTTP al módulo usa el código nuevo.

Todo esto ocurre **en el mismo proceso uvicorn** — sin reinicio, sin perder el estado de las otras sesiones WebSocket ni de los otros módulos.

---

## Dependencias opcionales

| Dependencia | Uso | Instalación |
|---|---|---|
| `watchfiles` | Motor de observación de ficheros (FSEvents/inotify) | `pip install watchfiles` |

`watchfiles` es una dependencia **opcional**. Si no está instalada, el watcher simplemente no funciona (warning en el log) pero el resto del framework es completamente operativo.

---

## Cómo encaja con el resto del framework

| Componente | Relación con `dev/` |
|---|---|
| `hotframe.engine.module_runtime.ModuleRuntime` | Su método `hot_reload(module_id)` es el callback natural para `on_change` |
| `hotframe.bootstrap.create_app` | Arranca y para `ModuleWatcher` durante el lifespan si `DEBUG=True` |
| `hotframe.config.settings.HotframeSettings` | `DEBUG` y `MODULES_DIR` determinan si el watcher se activa y qué directorio observa |
| `management/cli.py` → `runserver` | Arranca uvicorn con `reload=True` — esto es **distinto** de `ModuleWatcher`: uvicorn reload reinicia el proceso completo, `ModuleWatcher` solo recarga módulos individuales |

---

## Gotchas y decisiones de diseño

**1. Dos mecanismos de reload en desarrollo.**
`hf runserver` arranca uvicorn con `reload=True`, que reinicia el proceso completo cuando cambia cualquier fichero Python de la app. `ModuleWatcher` solo recarga el módulo afectado dentro del proceso en ejecución. En la práctica conviven: uvicorn recarga apps estáticas (código en `apps/`) y el watcher recarga módulos dinámicos (código en `modules/`). Ambos están activos en `DEBUG=True`.

**2. El callback puede ser síncrono o asíncrono.**
`_watch_loop` detecta si el resultado de `on_change(module_id)` es una corrutina con `asyncio.iscoroutine(result)` y hace `await` si es necesario. Esto permite pasar callbacks síncronos simples (para testing o extensiones) sin necesidad de envolverlos.

**3. Errores en hot-reload no abortan el watcher.**
Si `hot_reload` lanza una excepción (p.ej. error de sintaxis en el módulo recién editado), `logger.exception` la registra y el bucle continúa. El módulo queda desactivado (el `deactivate` ya se ejecutó) y el desarrollador verá el error en el log. Al corregir el fichero y guardarlo, se dispara otro evento y el reload se reintenta.

**4. `recently_reloaded` no se limpia.**
El diccionario `recently_reloaded` crece indefinidamente con los IDs de módulos que se han recargado. En proyectos con muchos módulos y sesiones de desarrollo largas, esto no tiene impacto práctico (son cadenas cortas), pero es una observación de diseño: en v1.0 no hay GC de ese diccionario.

**5. Solo observa `modules/`, no `apps/`.**
Los cambios en `apps/` los gestiona el reload de uvicorn (que sí observa todo el directorio del proyecto). El `ModuleWatcher` es específico para módulos dinámicos.

**6. `WATCH_EXTENSIONS` incluye `.jinja2`.**
Las plantillas también disparan hot-reload del módulo. Cuando un desarrollador edita una plantilla de un módulo, el módulo se recarga. Esto es necesario porque el motor Jinja2 puede cachear las plantillas; reactivar el módulo limpia esas cachés.
