# 9. El motor de plantillas (templating/)

> El subsistema `templating/` construye, configura y mantiene el entorno Jinja2 que toda la
> aplicación usa para renderizar HTML — con descubrimiento de directorios automático, globals de
> framework, filtros de formato, i18n integrado y el sistema de slots para inyección de UI entre
> módulos.

---

## Para qué sirve esta carpeta

`hotframe.templating` es la capa que separa "tener Jinja2 instalado" de "tener Jinja2 listo para
producción en hotframe". Sus responsabilidades concretas son:

1. **Construir el `Environment` Jinja2** con todos los directorios de plantillas correctamente
   ordenados — globales, apps, módulos y raíces de componentes.
2. **Registrar extensiones de tag**: `{% component %}` y `{% live %}`, más las de la librería
   estándar de Jinja2 (`i18n`, `do`, `loopcontrols`).
3. **Exponer globals y filtros** que los templates dan por sentados: `static`, `url_for`, `icon`,
   `render_slot`, `currency`, `dateformat`, etc.
4. **Inyectar contexto de seguridad** automáticamente en cada `TemplateResponse` (`csrf_token`,
   `csp_nonce`, `csrf_input`).
5. **Gestionar el sistema de slots** — el `SlotRegistry` que permite que módulos terceros
   contribuyan fragmentos de UI a puntos de extensión declarados por otros módulos.
6. **Soportar hot-reload**: cuando un módulo se activa o desactiva, `refresh_template_dirs` vuelve
   a escanear los directorios sin reiniciar.

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`__init__.py`](../src/hotframe/templating/__init__.py) | Docstring de módulo; re-exporta los puntos de entrada públicos |
| [`engine.py`](../src/hotframe/templating/engine.py) | `create_template_engine`, `_HotframeTemplates`, `refresh_template_dirs` |
| [`extensions.py`](../src/hotframe/templating/extensions.py) | `register_extensions` — globals (`static`, `url_for`, `icon`, `stat_card`, `render_slot`) y filtros (`currency`, `dateformat`, `timesince`, `slugify`, …) |
| [`globals.py`](../src/hotframe/templating/globals.py) | `get_global_context` — contexto por-request inyectado por `@view` (user, csrf, csp, módulos de menú) |
| [`slots.py`](../src/hotframe/templating/slots.py) | `SlotEntry`, `SlotRegistry` — registro y resolución de contenidos de slot entre módulos |

---

## `engine.py` — El constructor del entorno Jinja2

### `_collect_template_dirs(modules_dir)`

Función privada que construye la lista ordenada de directorios que el `FileSystemLoader` de Jinja2
consultará. El orden importa: una plantilla encontrada primero "gana".

```
Orden de búsqueda:
1.  CWD/templates/                     ← globales del proyecto
2.  apps/*/templates/                  ← cada app estática, ordenadas alfabéticamente
3.  modules/*/templates/               ← cada módulo activo, ordenados alfabéticamente
4.  hotframe/components/               ← raíz de componentes built-in del framework
5.  apps/                              ← raíz para resolver <app>/components/<name>/template.html
6.  modules_dir/                       ← raíz para resolver <module_id>/components/<name>/template.html
```

Los directorios 4-6 son las "raíces de componentes". La discovery de componentes registra rutas
como `shared/components/badge/template.html`; para que Jinja2 las resuelva, necesita tener `apps/`
en su search path.

### `create_template_engine(modules_dir=None) -> Jinja2Templates`

El único constructor público. Crea el `Environment` de Jinja2 con:

```python
env = Environment(
    loader=FileSystemLoader(template_dirs),
    autoescape=select_autoescape(["html", "xml"]),
    extensions=[
        "jinja2.ext.i18n",       # {% trans %}, _(), ngettext()
        "jinja2.ext.do",         # {% do expression %}
        "jinja2.ext.loopcontrols",  # {% break %}, {% continue %}
        ComponentExtension,      # {% component 'name' key=val %}...{% endcomponent %}
        LiveExtension,           # {% live 'name' prop=val %}
    ],
)
```

Después de construir el `Environment`:

1. Llama a `install_component_context_tracker(env)` para parchear `env.context_class` y publicar
   el `Context` activo en un `ContextVar` (necesario para que `{% component %}` acceda al framework
   slice dentro de un `CallBlock`).
2. Llama a `register_extensions(env)` para instalar globals y filtros.
3. Llama a `register_component_globals(env)` para instalar la función `render_component`.
4. Instala `live_assets` como global (la función que emite el `<script>` de `live.js`).
5. Instala las traducciones gettext via `env.install_gettext_translations(get_translations())`.

Devuelve una instancia de `_HotframeTemplates` (no el `Environment` crudo), lista para usar como
`app.state.templates` en FastAPI.

### `_HotframeTemplates` — Subclase de `Jinja2Templates`

```python
class _HotframeTemplates(Jinja2Templates):
    def TemplateResponse(self, request, name, context=None, **kwargs):
        ...
```

Sobreescribe `TemplateResponse` para inyectar automáticamente en `context`:

| Variable | Fuente |
|---|---|
| `request` | El request actual |
| `csrf_token` | `request.state.csrf_token` |
| `csrf_input` | Lambda que devuelve el `<input hidden>` con el token |
| `csp_nonce` | `request.state.csp_nonce` |

Esto garantiza que cualquier plantilla renderizada via `templates.TemplateResponse(...)` tenga
siempre estas variables disponibles, sin que el código de vista tenga que recordarlo.

### `refresh_template_dirs(templates, modules_dir)`

```python
def refresh_template_dirs(templates: Jinja2Templates, modules_dir: Path) -> None:
    template_dirs = _collect_template_dirs(modules_dir)
    templates.env.loader = FileSystemLoader(template_dirs)
```

Reemplaza el `loader` del entorno ya existente. No recrea el `Environment` — todas las extensiones
y globals registradas previamente se conservan. El `ModuleRuntime` llama a esta función después de
activar o desactivar un módulo para que las plantillas del módulo aparezcan (o desaparezcan) sin
reiniciar el proceso.

---

## `extensions.py` — Globals y filtros

### `register_extensions(env)`

Punto de entrada único. Registra en el `Environment` todas las funciones globales y filtros que los
templates de hotframe dan por garantizados.

#### Globals registrados

| Nombre en template | Función Python | Qué hace |
|---|---|---|
| `static` | `static_url(path)` | Devuelve `/static/{path}` |
| `url_for` | `url_for_helper(name, **kwargs)` | Genera URLs a módulos: `url_for('notes:index')` → `/m/notes/index/` |
| `icon` | `render_icon(name, size, css_class, **attrs)` | Genera markup Iconify (`<span class="iconify" data-icon="...">`) |
| `render_slot` | `render_slot_helper(slot_name, **context)` | Placeholder; la implementación real se pasa desde el bootstrap |
| `currency` | `currency_filter(value, currency_code, language)` | Formatea moneda via Babel si disponible |
| `ngettext` | Del middleware i18n | Plural gettext |
| `get_current_language` | Del middleware i18n | Idioma activo del request |
| `csrf_input` | Lambda vacía | Sobrescrita por `_HotframeTemplates` con el token real |
| `stat_card` | `stat_card_helper(value, label, icon, color)` | Genera el HTML de un tile de dashboard |

#### Filtros registrados

| Filtro | Firma | Comportamiento |
|---|---|---|
| `currency` | `value \| currency` | Igual que el global `currency` |
| `dateformat` | `value \| dateformat('d/m/Y H:i')` | Formato PHP-style de fecha/hora |
| `timeformat` | `value \| timeformat('H:i')` | Solo hora, tokens PHP: `H`, `G`, `h`, `g`, `i`, `s`, `a` |
| `timesince` | `value \| timesince` | "2 hours", "3 days", etc. desde `value` hasta ahora |
| `truncatewords` | `value \| truncatewords(10)` | Corta a N palabras añadiendo `…` |
| `slugify` | `value \| slugify` | Normaliza unicode, minúsculas, reemplaza espacios por `-` |

#### `render_icon` en detalle

```python
def render_icon(name: str, size: int | None = None, css_class: str = "", **attrs: str) -> Markup:
```

Soporta namespaces de icono:

```jinja
{{ icon('ion:heart') }}
{{ icon('material:account', size=24, css_class='header-icon') }}
{{ icon('hero:check', aria_label='done') }}
```

El mapa `_NAMESPACE_MAP` traduce prefijos cortos a prefijos Iconify:

```python
_NAMESPACE_MAP = {
    "ion": "ion", "material": "mdi", "hero": "heroicons",
    "tabler": "tabler", "lucide": "lucide", "fa": "fa-solid",
}
```

Los `kwargs` adicionales se convierten en atributos HTML con guiones (`aria_label` → `aria-label`).

#### `url_for_helper` en detalle

```python
def url_for_helper(name: str, **kwargs: str) -> str:
```

El nombre puede usar `:` o `.` como separador. `url_for('notes:detail', pk=42)` genera
`/m/notes/detail/42/`. Sin separador devuelve `/{name}`. Esta convención sigue la de FastAPI pero
adaptada al prefijo `/m/<module_id>/` de los módulos dinámicos.

---

## `globals.py` — Contexto global por-request

### `get_global_context(request) -> dict`

Coroutine async que construye el contexto base inyectado antes de cada render de vista (`@view`).
No actúa directamente sobre el `Environment` — es el decorador `@view` quien llama a esta función
y fusiona el resultado con el contexto que devuelve la función de vista.

Variables que produce:

| Clave | Tipo | Origen |
|---|---|---|
| `request` | `Request` | El request HTTP actual |
| `csp_nonce` | `str` | `request.state.csp_nonce` |
| `csp_trusted_types` | `bool` | `settings.CSP_TRUSTED_TYPES` |
| `csrf_token` | `str` | `request.state.csrf_token` |
| `csrf_input` | callable | Lambda que devuelve el `<input hidden>` |
| `debug` | `bool` | `app.state.debug` |
| `current_path` | `str` | `request.url.path` |
| `user` | model o `None` | Usuario autenticado; cargado de sesión si no está ya en `request.state` |
| `is_authenticated` | `bool` | `True` si hay usuario activo |
| `module_menu_items` | `list` | Ítems de menú de módulos activos (via `module_registry.get_menu_items()`) |

#### Hook de contexto personalizable

Si `settings.GLOBAL_CONTEXT_HOOK` está definido (dotted path a una función async), se invoca
después de rellenar el contexto base. La función recibe `request` y debe devolver un `dict`. Su
resultado se fusiona en el contexto con `context.update(extra)`.

```python
# settings.py
GLOBAL_CONTEXT_HOOK = "apps.shared.context.add_branding_context"

# apps/shared/context.py
async def add_branding_context(request):
    return {"app_name": "Acme", "logo_url": "/static/logo.svg"}
```

#### `_load_user_from_session`

Función privada que carga el usuario de la base de datos si no estaba ya en `request.state`. Usa
`get_session_user_id(request)` para leer el `user_id` de la cookie de sesión, luego ejecuta una
`SELECT` con `is_active=True`. Escribe el usuario de vuelta a `request.state.current_user` como
caché para el resto del ciclo de vida del request.

---

## `slots.py` — Sistema de slots

El sistema de slots es el mecanismo de extensión de UI de hotframe. Permite que un módulo inyecte
contenido (fragmentos de template) en puntos de extensión definidos por otros módulos, sin que
haya ningún `import` directo entre ellos.

### `SlotEntry`

```python
@dataclass(slots=True)
class SlotEntry:
    template: str
    priority: int = 10
    module_id: str | None = None
    context_fn: Callable | None = None
    condition_fn: Callable | None = None
```

Cada entrada representa una contribución a un slot concreto. Los atributos clave:

- **`template`**: Ruta Jinja2 del fragmento a renderizar (ej. `"loyalty/partials/widget.html"`).
- **`priority`**: Entero — las entradas con menor número se renderizan primero (default 10).
- **`module_id`**: Permite el cleanup automático cuando el módulo se desactiva.
- **`condition_fn`**: Callable síncrono o async que devuelve `bool`. Si devuelve `False`, la
  entrada se omite silenciosamente. Recibe `request` y el `**extra_context` del call site.
- **`context_fn`**: Callable síncrono o async que devuelve un `dict`. Se fusiona con el contexto
  antes de renderizar el template del slot. Útil para cargar datos adicionales (ej. el saldo de
  puntos de un cliente desde la DB).

### `SlotRegistry`

Singleton de la app, almacenado en `app.state.slots`. Internamente mantiene:

```python
self._slots: dict[str, list[SlotEntry]] = {}
```

#### `register(slot_name, template, *, priority, module_id, context_fn, condition_fn)`

```python
slots.register(
    "dashboard_widgets",
    template="loyalty/partials/widget.html",
    priority=5,
    module_id="loyalty",
    condition_fn=lambda request, **kw: request.user.has_loyalty_card,
)
```

Añade una `SlotEntry` a la lista del slot. Si el slot no existía, se crea con `setdefault`.
No hay validación de nombres de slot — cualquier cadena es un nombre válido.

#### `get_entries(slot_name, request=None, **extra_context) -> list[tuple[SlotEntry, dict]]`

Método async. Proceso interno:

1. Recupera las entradas del slot y las ordena por `priority`.
2. Para cada entrada, evalúa `condition_fn` (si existe). Si devuelve `False` o lanza excepción, la
   entrada se salta.
3. Evalúa `context_fn` (si existe). El resultado se fusiona sobre `extra_context`.
4. Devuelve lista de tuplas `(entry, context_dict)`.

Las excepciones en `condition_fn` y `context_fn` se capturan y loguean (sin crash), manteniendo
el slot resiliente ante errores en módulos de terceros.

#### `unregister_module(module_id)`

```python
def unregister_module(self, module_id: str) -> None:
```

Elimina todas las `SlotEntry` cuyo `module_id` coincide. Después limpia los slots que han quedado
vacíos. El `ModuleRuntime` llama a esta función al desactivar un módulo — la UI deja de mostrar
los fragmentos del módulo sin ningún cambio en el código del módulo anfitrión.

#### Métodos auxiliares

| Método | Descripción |
|---|---|
| `has_content(slot_name)` | `True` si hay al menos una entrada para ese slot |
| `list_slots()` | `dict[str, int]` — nombre → número de entradas; para diagnóstico |
| `clear()` | Vacía todo; uso en tests |

#### Ejemplo completo de uso

En el template de la app anfitriona:

```jinja
{# apps/shared/templates/shared/dashboard.html #}
{% set widget_entries = slot_entries('dashboard_widgets') %}
{% for entry, ctx in widget_entries %}
  {% include entry.template with context %}
{% endfor %}
```

En el módulo contribuyente:

```python
# modules/loyalty/slots.py
def register_slots(slots, module_id):
    slots.register(
        slot="dashboard_widgets",
        template="loyalty/partials/points_widget.html",
        priority=5,
        module_id=module_id,
        context_fn=_load_points_summary,
        condition_fn=_user_has_card,
    )

async def _load_points_summary(request, **kw):
    return {"points": await PointsRepo.total_for(request.state.current_user.id)}

async def _user_has_card(request, **kw):
    return await LoyaltyRepo.has_card(request.state.current_user.id)
```

---

## Cómo encaja con el resto del framework

```
create_app()
  └─► create_template_engine()          ← templating/engine.py
        ├─ register_extensions(env)     ← templating/extensions.py
        ├─ register_component_globals() ← components/rendering.py
        ├─ install_component_context_tracker() ← components/jinja_ext.py
        └─ env.globals["live_assets"]   ← live/assets.py

@view decorator
  └─► get_global_context(request)       ← templating/globals.py
        └─ merges into TemplateResponse context

ModuleRuntime.activate(module_id)
  └─► refresh_template_dirs()           ← templating/engine.py
  └─► SlotRegistry.register(...)        ← templating/slots.py

ModuleRuntime.deactivate(module_id)
  └─► refresh_template_dirs()
  └─► SlotRegistry.unregister_module()
```

- **`live/`** usa exactamente el mismo `Environment` creado aquí. La extensión `LiveExtension`
  y el global `live_assets` se instalan en `create_template_engine`.
- **`components/`** registra `render_component` en el mismo `env.globals` y usa el
  `context_class` parcheado para acceder al framework slice.
- **`engine/module_runtime.py`** llama a `refresh_template_dirs` en cada activación/desactivación
  de módulo.
- **`middleware/csrf.py`** y **`middleware/csp.py`** escriben `csrf_token` y `csp_nonce` en
  `request.state`; `_HotframeTemplates.TemplateResponse` los lee y los inyecta en el contexto.

---

## Gotchas y decisiones de diseño

**1. Un solo `Environment`, muchos directorios.**
En lugar de crear un `Environment` por módulo, hotframe usa uno solo con todos los directorios en
el `FileSystemLoader`. Esto maximiza la reutilización del cache de templates compilados de Jinja2.
La contra es que `refresh_template_dirs` tiene que reconstruir el loader entero, lo que invalida
el cache parcialmente. En la práctica, esto solo ocurre al activar/desactivar módulos (evento
raro en producción).

**2. Orden de directorios como mecanismo de override.**
La app del proyecto puede sobreescribir templates de módulos poniendo el mismo nombre de archivo
en `CWD/templates/`. No hay sistema de "herencia de app" explícito — el orden de búsqueda hace
de override implícito. Documenta bien qué templates son "sobreescribibles" en tu módulo.

**3. `render_slot_helper` es un placeholder.**
La función registrada en `extensions.py` como `render_slot` devuelve un comentario HTML. La
implementación real que renderiza los fragmentos de la `SlotRegistry` se inyecta desde el
bootstrap. Esto significa que si ves `<!-- slot:nombre -->` en la salida HTML, hay un problema
en la inicialización.

**4. `context_fn` y `condition_fn` pueden ser sync o async.**
El `SlotRegistry.get_entries` usa `inspect.iscoroutinefunction` para decidir si hace `await`.
Esto es conveniente pero tiene una limitación: lambdas y funciones parciales no son detectadas
como coroutines aunque el callable subyacente lo sea. Usa siempre `async def` explícito.

**5. Colisiones de nombre en `url_for`.**
`url_for_helper` no consulta el router FastAPI — genera URLs por convención
(`/m/<module_id>/<view_id>/`). Si un módulo usa nombres de vista que no siguen esta convención,
la URL generada será incorrecta. Los módulos montados vía `ModuleRuntime` sí siguen la convención.

**6. `csrf_input` se registra como lambda vacía en `register_extensions`.**
Esto es un default seguro: si alguien usa `{{ csrf_input() }}` en un template renderizado fuera
del ciclo normal (ej. en tests sin request), no explota. `_HotframeTemplates` sobreescribe este
valor con la lambda real en cada `TemplateResponse`.

**7. Babel es opcional.**
`currency_filter` intenta importar `babel.numbers.format_currency`. Si Babel no está instalado,
cae a un formato `f"{value:.2f} {currency_code}"`. No es un `ImportError` silenciado de forma
opaca — hay un `try/except (ImportError, Exception)` explícito.