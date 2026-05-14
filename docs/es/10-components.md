# 10. Componentes stateless (components/)

> El subsistema `components/` implementa la unidad de UI reutilizable más simple de hotframe:
> un directorio con un `template.html` y, opcionalmente, una clase Pydantic de props, un router
> FastAPI y assets estáticos propios — sin estado de servidor, sin WebSocket.

---

## Para qué sirve esta carpeta

Un componente stateless es la respuesta a "¿cómo reutilizo este trozo de HTML sin copiar-pegar
ni romper el encapsulamiento?". En hotframe, la respuesta es: crea un directorio en
`apps/<app>/components/<nombre>/` o `modules/<id>/components/<nombre>/`, pon un `template.html`,
y ya puedes invocarlo desde cualquier plantilla con `{{ render_component('nombre', prop=val) }}` o
`{% component 'nombre' prop=val %}...{% endcomponent %}`.

La carpeta `components/` del framework gestiona todo el ciclo de vida de esas definiciones:

1. **Descubrimiento** (`discovery.py`) — escanea el filesystem y construye `ComponentEntry`.
2. **Registro** (`registry.py`) — catálogo en RAM indexado por nombre, con limpieza por módulo.
3. **Render aislado** (`rendering.py`) — valida props, construye un contexto aislado (sin filtración
   de variables del padre), renderiza el template.
4. **Extensión Jinja2** (`jinja_ext.py`) — el tag `{% component %}` con soporte de body.
5. **Montaje de routers y assets** (`mounting.py`) — conecta los routers y directorios `static/`
   de cada componente al `FastAPI` app.
6. **Clase base** (`base.py`) — `Component`, el modelo Pydantic que declaras en `component.py`.
7. **Entry** (`entry.py`) — `ComponentEntry`, el descriptor en RAM de un componente registrado.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`__init__.py`](../src/hotframe/components/__init__.py) | Re-exporta la API pública del subsistema |
| [`base.py`](../src/hotframe/components/base.py) | Clase `Component` — base Pydantic para props tipadas |
| [`entry.py`](../src/hotframe/components/entry.py) | Dataclass `ComponentEntry` — descriptor en RAM |
| [`discovery.py`](../src/hotframe/components/discovery.py) | Escaneo de filesystem y construcción de entries |
| [`registry.py`](../src/hotframe/components/registry.py) | `ComponentRegistry` — catálogo en memoria |
| [`rendering.py`](../src/hotframe/components/rendering.py) | `render_component` global Jinja2 + `_render_entry` |
| [`jinja_ext.py`](../src/hotframe/components/jinja_ext.py) | `ComponentExtension` — tag `{% component %}` |
| [`mounting.py`](../src/hotframe/components/mounting.py) | Mount y unmount de routers y assets estáticos |

---

## `base.py` — La clase `Component`

```python
class Component(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    def context(self) -> dict:
        return {}
```

`Component` es un `pydantic.BaseModel` con una única extensión: el método `context()`.

### Cómo se usa

```python
# apps/shared/components/media_picker/component.py
from hotframe.components import Component

class MediaPicker(Component):
    path: str
    multiple: bool = False
    accept: str = "image/*"

    def context(self) -> dict:
        return {"accept_list": self.accept.split(",")}
```

Cuando el framework renderiza este componente:

1. Instancia `MediaPicker(**props_from_caller)` — Pydantic valida y convierte.
2. Llama a `instance.model_dump()` para obtener los props como dict.
3. Llama a `instance.context()` y fusiona el resultado.
4. Renderiza `template.html` con ese dict combinado más el framework slice.

### `context()` — para valores derivados

El método `context()` es síncrono por diseño (el entorno Jinja2 de hotframe es síncrono). Su
propósito es exponer valores calculados a partir de los props que no quieres calcular en el
template. Si necesitas datos de la base de datos, cárgalos en el endpoint o en la vista que invoca
el componente, y pásalos como prop.

### Diferencia con `LiveComponent`

`Component` y `LiveComponent` son jerarquías independientes — `LiveComponent` no hereda de
`Component`. Esta separación es deliberada: los LiveComponents tienen su propio ciclo de vida
(Pydantic + state + lifecycle hooks + WebSocket) que es incompatible con el modelo sin estado de
`Component`. El descubrimiento maneja ambas clases base.

---

## `entry.py` — `ComponentEntry`

```python
@dataclass(slots=True)
class ComponentEntry:
    name: str
    template: str
    has_endpoint: bool = False
    render_fn: Callable[..., dict[str, Any]] | None = None
    extra_router: APIRouter | None = None
    module_id: str | None = None
    static_dir: str | None = None
    props_cls: type | None = None
    is_live: bool = False
```

Este dataclass es el "pasaporte" de un componente dentro del framework. Lo que llevan sus campos:

| Campo | Qué es |
|---|---|
| `name` | El identificador único: `"badge"`, `"data_table"`, etc. |
| `template` | Ruta Jinja2 relativa al loader: `"shared/components/badge/template.html"` |
| `has_endpoint` | `True` si `routes.py` declaró un `router` |
| `render_fn` | Callable `(**props) -> dict` construido por discovery; `None` si el componente es template-only |
| `extra_router` | El `APIRouter` de `routes.py`, listo para montar |
| `module_id` | Módulo dueño; `None` para componentes de apps estáticas |
| `static_dir` | Ruta absoluta al directorio `static/` del componente, o `None` |
| `props_cls` | La clase `Component` o `LiveComponent` declarada en `component.py` |
| `is_live` | `True` cuando `props_cls` es subclase de `LiveComponent` |

`is_live` permite al runtime live distinguir rápidamente entre componentes stateless y stateful
sin hacer `issubclass` en el hot path de cada evento WebSocket.

---

## `discovery.py` — Descubrimiento de componentes

El motor de discovery escanea el filesystem, importa `component.py` y `routes.py` si existen, y
construye una lista de `ComponentEntry`. Es completamente síncrono y solo tiene efectos secundarios
de importación (`sys.modules`) y de llamada a `registry.register`.

### `_load_module_from_file(py_path, module_name)`

```python
def _load_module_from_file(py_path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, py_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
```

Importa un archivo `.py` cualquiera por ruta de filesystem, lo registra en `sys.modules` con un
nombre sintético y lo devuelve. Esto es necesario porque los componentes viven fuera del árbol de
paquetes normal de Python (están en `apps/` o `modules/`, no en el paquete importado).

El `module_name` usa prefijos como `_hotframe_components.{module_id}.{name}.component` para evitar
colisiones en `sys.modules` entre componentes de módulos distintos.

### `_find_component_class(module) -> type | None`

```python
def _find_component_class(module) -> type | None:
    bases = (Component, LiveComponent)
    for _attr_name, attr in inspect.getmembers(module, inspect.isclass):
        if attr in bases:
            continue
        if issubclass(attr, bases) and attr.__module__ == module.__name__:
            return attr
    # Fallback: acepta incluso si fue re-exportada
    ...
```

Busca la primera subclase de `Component` o `LiveComponent` en el módulo importado. La
condición `attr.__module__ == module.__name__` prioriza clases definidas en ese archivo (no
importadas). Si no hay ninguna con esa condición, el fallback acepta cualquier subclase, para
soportar el patrón de re-exportar la clase desde otro módulo.

### `_build_render_fn(props_cls) -> Callable | None`

Construye la función de render para el componente según el tipo de `props_cls`:

- `props_cls is None` (template-only) → devuelve `None`. Los kwargs del caller pasan directamente
  como contexto.
- `issubclass(props_cls, LiveComponent)` → devuelve `None`. Los LiveComponents tienen su propio
  path de render en `hotframe.live.diff`.
- `issubclass(props_cls, Component)` → devuelve la función closure `render_fn(**props) -> dict`.

La función `render_fn` resultante:

```python
def render_fn(**props) -> dict:
    instance = props_cls(**props)       # validación Pydantic
    context = instance.model_dump()     # props validadas como dict
    extra = instance.context()          # valores derivados
    if extra:
        context.update(extra)
    return context
```

### `discover_components(root, *, module_id, template_search_prefix, import_prefix)`

La función principal. Itera `root` en orden alfabético, salta directorios ocultos y sin
`template.html`, y para cada componente válido:

1. Busca y carga `component.py` → `props_cls` + detecta `is_live`.
2. Busca y carga `routes.py` → `extra_router`.
3. Detecta si existe `static/` → `static_dir`.
4. Calcula la ruta del template: `{template_search_prefix}/{name}/template.html`.
5. Construye `ComponentEntry` con todos los campos.

Devuelve la lista de entries, sin tocar el registry — es pura construcción de datos.

### Helpers de discovery escopada

```python
discover_module_components(registry, module_dir, module_id) -> int
discover_app_components(registry, apps_dir, app_name) -> int
discover_apps_components(registry, apps_dir) -> int
```

Estos tres helpers son los puntos de entrada que el bootstrap real usa:

- `discover_apps_components` escanea todas las apps en `apps/` de una vez durante el arranque.
- `discover_module_components` escanea un módulo concreto cuando se activa (llamado por
  `ModuleRuntime`).

La diferencia en los `template_search_prefix` es importante:
- Apps: `"shared/components"` → template se referencia como `shared/components/badge/template.html`
- Módulos: `"loyalty/components"` → template se referencia como `loyalty/components/widget/template.html`

El `FileSystemLoader` de Jinja2 tiene `apps/` y `modules_dir/` en su search path, por lo que
estos prefijos resuelven correctamente.

---

## `registry.py` — `ComponentRegistry`

```python
class ComponentRegistry:
    def __init__(self) -> None:
        self._components: dict[str, ComponentEntry] = {}
```

Un dict plano, indexado por nombre de componente. Almacenado en `app.state.components`.

### API completa

| Método | Firma | Descripción |
|---|---|---|
| `register` | `(entry, *, module_id=None)` | Registra una entry; si `module_id` se pasa, lo escribe en `entry.module_id`. Colisión de nombre → warning + overwrite |
| `unregister` | `(name)` | Elimina por nombre; no-op si no existe |
| `unregister_module` | `(module_id)` | Elimina todas las entries del módulo |
| `get` | `(name) -> ComponentEntry \| None` | Lookup por nombre |
| `has` | `(name) -> bool` | Comprobación de existencia |
| `list_components` | `() -> list[ComponentEntry]` | Lista en orden de inserción |
| `clear` | `()` | Vacía todo (tests) |
| `__len__` | — | Número de componentes registrados |
| `__contains__` | — | `"badge" in registry` |

### Comportamiento ante colisiones

Cuando dos entidades registran un componente con el mismo nombre, el registry logea:

```
Component name collision: 'badge' is being overwritten
(previous module=shared, new module=premium_ui)
```

Y sobreescribe con la nueva entry. Esto es deliberado: en desarrollo, cuando un módulo se
recarga, sus definiciones actualizadas deben reemplazar a las anteriores sin reiniciar.

### Limpieza en módulo unload

```python
def unregister_module(self, module_id: str) -> None:
    to_remove = [
        name for name, entry in self._components.items()
        if entry.module_id == module_id
    ]
    for name in to_remove:
        del self._components[name]
```

El `ModuleRuntime` llama a `unregister_module` al desactivar un módulo. A partir de ese momento,
cualquier `render_component('widget_del_modulo')` encontrará `None` en el registry y devolverá
`Markup("")` (con un warning en el log).

---

## `rendering.py` — El render aislado

Este módulo implementa el corazón del render de componentes stateless: el contexto aislado y la
función `render_component` que se expone como global Jinja2.

### El "framework slice" — contexto aislado

```python
_FRAMEWORK_CONTEXT_KEYS = (
    "request",
    "csrf_token",
    "csp_nonce",
    "user",
    "current_path",
)
```

Cuando se renderiza un componente, el nuevo contexto solo recibe:
1. Los props validados del componente.
2. Las 5 claves del framework slice extraídas del contexto padre.

**Nada más.** Las variables locales del template que llamó al componente no se heredan. Este
aislamiento es una decisión de diseño explícita: evita bugs donde un componente de uso general
funciona en una página pero silenciosamente depende de una variable que otra página no define.

```python
def _framework_slice(ctx: Context) -> dict:
    return {key: ctx.get(key) for key in _FRAMEWORK_CONTEXT_KEYS if key in ctx}
```

### `_render_entry(env, ctx, entry, props, body=None) -> Markup`

La función central, no pública. Secuencia de operaciones:

1. Si `entry.render_fn` no es `None`: llama `render_fn(**props)`, capturando
   `ValidationError` (props inválidas) y `TypeError` (kwargs inesperados). En ambos casos
   devuelve un comentario HTML con el error en lugar de explotar.
2. Si `entry.render_fn` es `None` (template-only): usa los `props` crudos como contexto.
3. Fusiona el framework slice sobre el contexto.
4. Si se pasó `body` (desde el tag `{% component %}`), añade `context["body"] = Markup(body)`.
5. Carga el template con `env.get_template(entry.template)`.
6. Devuelve `Markup(template.render(**context))`.

Todos los errores devuelven `Markup("")` o un comentario HTML — los templates nunca lanzan
excepciones al usuario por un fallo en un componente.

### `render_component` — El global Jinja2

```python
@pass_context
def render_component(ctx: Context, __component_name__: str, /, **props) -> Markup:
```

Decorado con `@pass_context` de Jinja2 para recibir el contexto de renderizado activo.
El nombre del componente es un parámetro posicional-only (`/`) para que `name=...` pueda ser
un prop sin colisionar con el dispatch.

Flujo:
1. Obtiene el `ComponentRegistry` de `ctx.environment.globals["_hotframe_components"]`.
2. Busca la entry por nombre.
3. Delega a `_render_entry`.

### `register_component_globals(env)`

```python
def register_component_globals(env: Environment) -> None:
    env.globals["render_component"] = render_component
```

Registra el global en el entorno. Llamado por `create_template_engine`.

---

## `jinja_ext.py` — El tag `{% component %}`

La extensión Jinja2 que habilita la sintaxis de bloque con body:

```jinja
{% component 'modal' title='Confirm' %}
  <p>Are you sure?</p>
{% endcomponent %}
```

### El problema del `CallBlock` y el `ContextVar`

Jinja2 implementa los tags con body mediante `CallBlock`. El problema es que `CallBlock` no
recibe el `Context` de renderizado — lo llama el intérprete en un contexto separado. Para que el
componente tenga acceso al framework slice (request, csrf_token…), el módulo parchea la clase de
contexto del entorno.

```python
_current_ctx: contextvars.ContextVar = contextvars.ContextVar(
    "hotframe_component_render_ctx", default=None
)
```

### `install_component_context_tracker(env)`

```python
def install_component_context_tracker(env: Environment) -> None:
    original_context_class = env.context_class
    if getattr(original_context_class, "_hotframe_patched", False):
        return

    class _TrackingContext(original_context_class):
        _hotframe_patched = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            _current_ctx.set(self)

    env.context_class = _TrackingContext
```

Cada vez que Jinja2 crea un nuevo `Context` (al iniciar el render de un template), el constructor
de `_TrackingContext` publica ese contexto en el `ContextVar`. La función
`_current_render_context()` lo lee cuando el `CallBlock` necesita el framework slice. El check
`_hotframe_patched` hace la operación idempotente.

### `ComponentExtension`

```python
class ComponentExtension(Extension):
    tags = {"component"}

    def parse(self, parser: Parser):
        ...
        body = parser.parse_statements(("name:endcomponent",), drop_needle=True)
        call = self.call_method("_render_component", [name_expr], kwargs)
        return nodes.CallBlock(call, [], [], body).set_lineno(lineno)
```

El parser extrae el nombre del componente, recopila los keyword arguments hasta el `block_end`,
parsea el body hasta `{% endcomponent %}` y emite un `CallBlock`. El `CallBlock` es el nodo AST
de Jinja2 que ejecuta `caller()` para obtener el HTML del body renderizado.

```python
def _render_component(self, __component_name__, /, *, caller=None, **props) -> Markup:
    env = self.environment
    registry = env.globals.get("_hotframe_components")
    ...
    body = caller() if caller is not None else ""
    ctx = _current_render_context()
    return _render_entry(env, ctx, entry, props, body=str(body))
```

El body queda disponible como `{{ body }}` dentro del template del componente (se inyecta en el
contexto como `Markup(body)`).

#### Limitación: palabras reservadas de Python como props

Jinja2 parsea los kwargs del tag como identificadores Python. `class` es una palabra reservada,
por lo que no puede usarse como nombre de prop directamente:

```jinja
{# MALO: error de sintaxis #}
{% component 'button' class='btn-primary' %}

{# BIEN: usa un dict de atributos #}
{% component 'button' attrs={'class': 'btn-primary', 'id': 'submit'} %}
```

Esta limitación está documentada en el docstring del módulo.

---

## `mounting.py` — Montaje de routers y assets

### El problema del hot-reload y FastAPI

FastAPI/Starlette no tienen API para eliminar rutas en runtime. El sistema de componentes resuelve
esto con la misma técnica que usa `ModuleLoader`: mutación directa de `app.router.routes`.

```python
routes[:] = [
    route for route in routes
    if not _matches_component_subtree(_route_path(route), prefix, prefix_slash)
]
```

La lista se filtra in-place, eliminando los `Mount` entries que corresponden al componente.
Después se invalida el schema OpenAPI (`app.openapi_schema = None`) para que se regenere en la
próxima petición.

### Prefijos reservados

Todos los componentes usan el espacio `/_ components/<nombre>/`:

- Router: `/_components/{name}/`
- Static: `/_components/{name}/static/`

Este prefijo con guión bajo evita colisiones con rutas de módulos (`/m/`) y rutas de la app.

### API de montaje

#### Para el arranque inicial (todas las apps)

```python
mount_component_routers(app, registry) -> int
mount_component_static(app, registry) -> int
```

Itera todas las entries del registry y monta las que tienen `extra_router` o `static_dir`.

#### Para módulos individuales (hot-mount)

```python
mount_component_routers_for_module(app, registry, module_id) -> int
mount_component_static_for_module(app, registry, module_id) -> int
```

Filtra por `entry.module_id == module_id`. Llamado por `ModuleRuntime` al activar un módulo.

#### Para desmontaje

```python
unmount_component_router(app, name) -> bool
unmount_component_routers_for_module(app, module_id) -> int
unmount_component_static(app, name) -> bool
unmount_component_static_for_module(app, module_id) -> int
```

Las variantes `_for_module` leen la propiedad `module_id` de las entries del registry para
identificar qué paths eliminar. Por eso es obligatorio llamarlas **antes** de
`registry.unregister_module()` — si el registry ya no tiene las entries, no sabe qué paths
limpiar.

### `_mount_single_static`

```python
def _mount_single_static(app: FastAPI, name: str, static_dir: str) -> bool:
```

Monta un `StaticFiles` en `/_components/{name}/static`. Antes de montar, verifica:
1. Que el directorio exista en disco.
2. Que no haya un mount con el mismo path ya registrado (evita duplicados en recargas).

---

## Cómo encaja con el resto del framework

```
Arranque (create_app)
  ├── discover_apps_components(registry, apps_dir)    ← discovery.py
  ├── mount_component_routers(app, registry)          ← mounting.py
  ├── mount_component_static(app, registry)           ← mounting.py
  └── create_template_engine()
        ├── ComponentExtension registrada en el env   ← jinja_ext.py
        └── register_component_globals(env)           ← rendering.py

ModuleRuntime.activate(module_id)
  ├── discover_module_components(registry, mod_dir, module_id)
  ├── mount_component_routers_for_module(app, registry, module_id)
  └── mount_component_static_for_module(app, registry, module_id)

ModuleRuntime.deactivate(module_id)
  ├── unmount_component_routers_for_module(app, module_id)  ← ANTES del unregister
  ├── unmount_component_static_for_module(app, module_id)   ← ANTES del unregister
  └── registry.unregister_module(module_id)

render_component / {% component %} (en cada request)
  ├── ComponentRegistry.get(name)      ← registry.py
  └── _render_entry(env, ctx, entry)   ← rendering.py
```

- **`templating/engine.py`** instala `ComponentExtension` y llama a
  `register_component_globals` — el motor de templates y los componentes comparten el mismo
  `Environment`.
- **`live/`** usa el mismo `ComponentRegistry` para hacer lookup de `LiveComponent` al recibir un
  `attach` por WebSocket. El campo `entry.is_live` permite esa distinción rápida.
- **`engine/module_runtime.py`** es el único cliente de las funciones `discover_module_components`
  y `mount_component_*_for_module` — coordina todo el ciclo de activación/desactivación.

---

## Gotchas y decisiones de diseño

**1. Orden obligatorio en el desmontaje.**
`unmount_component_routers_for_module` lee los nombres de componentes del registry para construir
los prefijos a eliminar. Si llamas a `registry.unregister_module` primero, la información se
pierde y las rutas quedan huérfanas en `app.router.routes`. El `ModuleRuntime` respeta este orden.

**2. `is_live` en el entry para evitar `issubclass` en el hot path.**
Cada evento WebSocket necesita saber si el componente del `cid` es live o stateless. Guardar
`is_live=True/False` en el `ComponentEntry` al momento del discovery evita hacer `issubclass(cls,
LiveComponent)` en cada mensaje. Es una micro-optimización que también simplifica el código del
runtime live.

**3. El `ContextVar` para el framework slice en `{% component %}`.**
La alternativa habría sido pasar el contexto como argumento al `CallBlock`, pero la API de Jinja2
no lo permite directamente. El `ContextVar` es thread-safe y coroutine-safe (asyncio usa
contextos `contextvars` por tarea). La única limitación: si se renderiza un template fuera de un
request (ej. en un script de CLI), `_current_ctx.get()` devuelve `None` y se usa `_EmptyCtx`,
por lo que el framework slice estará vacío.

**4. Los componentes de `LiveComponent` no tienen `render_fn`.**
`_build_render_fn` devuelve `None` para subclases de `LiveComponent`. Si alguien invoca
`render_component('todo_list', user_id=1)` desde un template (sin pasar por `{% live %}`), el
render caerá al path de template-only (kwargs crudos como contexto) y logueará un warning. No
explota, pero el resultado no tendrá el estado inicial.

**5. Nombres de componentes globales.**
El `ComponentRegistry` tiene un único namespace plano. Si el módulo `billing` y el módulo
`crm` definen ambos un componente llamado `invoice_badge`, el que se active después sobreescribe
al primero (con warning). La convención recomendada es prefixar: `billing_invoice_badge` y
`crm_invoice_badge`.

**6. El `template_search_prefix` y los dos niveles de loader.**
La discovery establece que las rutas de template son
`<app_name>/components/<name>/template.html`. El loader de Jinja2 tiene `apps/` en su search
path. Así `apps/shared/components/badge/template.html` se resuelve como
`shared/components/badge/template.html`. Este doble nivel (la raíz del loader + el prefijo del
template) es lo que permite tener componentes de distintos orígenes sin colisiones de nombre de
archivo.

**7. `model_config = {"arbitrary_types_allowed": True}` en `Component`.**
Permite usar tipos no-Pydantic en los props (ej. objetos SQLAlchemy, `Request`). Sin esto,
declarar `request: Request` como prop causaría un error de validación. Habilitar esta opción es
razonable para un modelo que raramente se serializa a JSON.
