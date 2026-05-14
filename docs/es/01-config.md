# 1. Configuración: settings, paths y base de datos (`config/`)

> `config/` es la capa más baja del framework: resuelve toda la configuración antes de que cualquier otra pieza arranque. Si entiendes este paquete, entiendes las palancas maestras de hotframe.

---

## Para qué sirve esta carpeta

`config/` centraliza tres responsabilidades ortogonales:

1. **Settings** — leer, validar y exponer la configuración de la aplicación (`settings.py`).
2. **Base de datos** — crear y gestionar el ciclo de vida del motor async SQLAlchemy (`database.py`).
3. **Paths** — resolver las rutas de sistema de ficheros efímeras que usa el framework (`paths.py`).

Estas tres piezas son singletons: se crean una sola vez por proceso y se reutilizan en todo el framework. `bootstrap.py` las consume en los primeros pasos de arranque (pasos 4b y 1 del lifespan, respectivamente).

---

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| [`__init__.py`](../src/hotframe/config/__init__.py) | Docstring-only; declara los exports públicos y sirve como índice legible del paquete. |
| [`settings.py`](../src/hotframe/config/settings.py) | Define `HotframeSettings` (Pydantic Settings), `get_settings()`, `set_settings()` y `reset_settings()`. |
| [`database.py`](../src/hotframe/config/database.py) | Proporciona el `AsyncEngine`, la `async_sessionmaker` y la dependencia FastAPI `get_db()`. |
| [`paths.py`](../src/hotframe/config/paths.py) | Define `DataPaths`: rutas efímeras bajo `/tmp/` para media, módulos, reportes y caché. |

---

## `settings.py` — HotframeSettings en profundidad

### Clase `HotframeSettings`

```python
class HotframeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
```

`HotframeSettings` hereda de `pydantic_settings.BaseSettings`. El usuario crea una subclase en `settings.py` de su proyecto y pasa la instancia a `create_app(settings)`. Eso permite añadir campos propios sin tocar el framework.

#### Precedencia de fuentes de configuración

Pydantic Settings resuelve los valores en este orden (mayor precedencia primero):

1. Argumentos pasados al constructor (`Settings(DATABASE_URL="...")`)
2. Variables de entorno del proceso
3. Archivo `.env` en el directorio de trabajo
4. Valores por defecto declarados en la clase

La opción `extra="ignore"` hace que variables de entorno desconocidas no provoquen error. `case_sensitive=False` permite mezclar mayúsculas y minúsculas sin problema.

---

### Grupos de settings

#### Core / aplicación

| Campo | Tipo | Default | Qué controla |
|---|---|---|---|
| `APP_TITLE` | `str` | `"Hotframe App"` | Título en Swagger y en la app FastAPI. |
| `DEBUG` | `bool` | `True` | Activa Swagger UI (`/api/docs`), redoc, logs verbosos. |
| `SECRET_KEY` | `str` | token aleatorio en cada arranque | Firma sesiones y tokens CSRF. **Debe fijarse en producción.** |
| `SECRETS_KEY` | `str \| None` | `None` | Clave Fernet (32 bytes en base64-url) para cifrar secretos en reposo. Obligatoria cuando `DEPLOYMENT_MODE != "local"`. |
| `DEPLOYMENT_MODE` | `"local" \| "web"` | `"local"` | `"web"` más `DEBUG=False` activa el modo producción. |
| `LOG_LEVEL` | `str` | `"INFO"` | Validado contra `{DEBUG, INFO, WARNING, ERROR, CRITICAL}`. |
| `LOG_FORMAT` | `"console" \| "json"` | `"console"` | `"json"` en producción o cuando se detecta pytest. |
| `EXTRA_ROUTERS` | `list[str]` | `[]` | Dotted paths a routers adicionales que no viven en `apps/`. |

#### Base de datos

| Campo | Tipo | Default | Qué controla |
|---|---|---|---|
| `DATABASE_URL` | `str` | `"sqlite+aiosqlite:///./app.db"` | URL de conexión async. |
| `DB_POOL_SIZE` | `int` | `10` | Conexiones permanentes del pool (ignorado en SQLite). |
| `DB_MAX_OVERFLOW` | `int` | `20` | Conexiones adicionales por encima del pool. |
| `DB_POOL_RECYCLE` | `int` | `3600` | Segundos antes de reciclar una conexión. |
| `DB_POOL_TIMEOUT` | `int` | `30` | Segundos máximos esperando una conexión libre. |
| `DB_ECHO` | `bool` | `False` | Loguea todo el SQL generado por SQLAlchemy. |
| `DB_DISABLE_PREPARED_STATEMENTS` | `bool` | `False` | Desactiva el caché de prepared statements de asyncpg. Necesario con RDS Proxy, PgBouncer o Supavisor. |
| `MAX_REQUEST_BODY` | `int` | `10 * 1024 * 1024` | Límite de body de request en bytes. |

#### Módulos

| Campo | Tipo | Default | Qué controla |
|---|---|---|---|
| `MODULES_DIR` | `Path` | `Path("./modules")` | Directorio local de módulos dinámicos. Se resuelve a ruta absoluta. |
| `MODULES_CACHE_DIR` | `Path` | `Path("/tmp/hotframe-modules")` | Caché efímera de módulos. Se resuelve a ruta absoluta. |
| `MODULE_SOURCE` | `str` | `"filesystem"` | Origen de los módulos: `"filesystem"`, `"s3"` o `"http"`. |
| `MODULE_MARKETPLACE_URL` | `str` | `""` | URL del marketplace de plugins. |
| `S3_MODULES_BUCKET` | `str` | `""` | Bucket S3 cuando `MODULE_SOURCE = "s3"`. |
| `AWS_REGION` | `str` | `"us-east-1"` | Región AWS para el cliente S3. |
| `MODULE_STATE_MODEL` | `str` | `""` | Dotted path al modelo SQLAlchemy de estado de módulos. Si está vacío, usa el modelo interno de hotframe. |

#### Ficheros estáticos y media

| Campo | Tipo | Default | Qué controla |
|---|---|---|---|
| `STATIC_ROOT` | `Path` | `Path("./static")` | Directorio de estáticos del proyecto. |
| `STATIC_URL` | `str` | `"/static/"` | URL base para servir estáticos. |
| `MEDIA_ROOT` | `Path` | `Path("./media")` | Directorio de media (solo dev local). |
| `MEDIA_STORAGE` | `str` | `"local"` | `"local"` o `"s3"`. |
| `MEDIA_S3_BUCKET` | `str` | `""` | Bucket S3 para media en producción. |
| `MEDIA_URL` | `str` | `"/media/"` | URL base para media. |

#### Seguridad y CSP

| Campo | Tipo | Default | Qué controla |
|---|---|---|---|
| `CSP_ENFORCE` | `bool` | `False` | Activa el header `Content-Security-Policy`. |
| `CSP_TRUSTED_TYPES` | `bool` | `False` | Activa `require-trusted-types-for 'script'`. Desactivado porque `live.js` + `morphdom` usan patrones incompatibles con Trusted Types. |
| `CSP_ALLOWED_SOURCES` | `dict[str, list[str]]` | `{"script": [], "style": [], ...}` | Orígenes permitidos por tipo de recurso. |
| `CSRF_EXEMPT_PREFIXES` | `list[str]` | `["/api/", "/health", "/static/"]` | Prefijos exentos de validación CSRF. |

#### Autenticación

| Campo | Tipo | Default | Qué controla |
|---|---|---|---|
| `AUTH_USER_MODEL` | `str` | `""` | Dotted path al modelo de usuario. Ej.: `"apps.accounts.models.User"`. |
| `AUTH_LOGIN_URL` | `str` | `"/login"` | URL a la que se redirige en 401. |
| `AUTH_UNAUTHORIZED_URL` | `str` | `"/unauthorized"` | URL para 403. |
| `PERMISSION_RESOLVER` | `str` | `""` | Dotted path a un callable async `(request, user_id) -> list[str]`. Resuelve los permisos del usuario para el decorador `@view`. |

#### Rate limiting

| Campo | Tipo | Default | Qué controla |
|---|---|---|---|
| `RATE_LIMIT_API` | `int` | `120` | Requests por minuto para rutas `/api/`. |
| `RATE_LIMIT_AUTH` | `int` | `60` | Requests por minuto para rutas de autenticación. |
| `RATE_LIMIT_AUTH_PREFIXES` | `list[str]` | `[]` | Prefijos que aplican el rate limit de auth. |

#### Sesiones

| Campo | Tipo | Default | Qué controla |
|---|---|---|---|
| `SESSION_COOKIE_NAME` | `str` | `"session"` | Nombre de la cookie de sesión. |
| `SESSION_MAX_AGE` | `int` | `86400 * 30` | TTL de la sesión en segundos (30 días). |

#### CORS

| Campo | Tipo | Default | Qué controla |
|---|---|---|---|
| `CORS_ORIGINS` | `list[str]` | `[]` | Si está vacío, CORS está desactivado. |
| `CORS_METHODS` | `list[str]` | todos los verbos HTTP | Métodos permitidos. |
| `CORS_HEADERS` | `list[str]` | `["*"]` | Headers permitidos. |
| `CORS_CREDENTIALS` | `bool` | `True` | Permite cookies cross-origin. |

#### Middleware

```python
MIDDLEWARE: list[str] = [
    "hotframe.middleware.timeout.TimeoutMiddleware",
    "hotframe.middleware.error_pages.ErrorPageMiddleware",
    "hotframe.middleware.body_limit.BodyLimitMiddleware",
    "asgi_correlation_id.CorrelationIdMiddleware",
    "hotframe.middleware.observability.RequestObservabilityMiddleware",
    "hotframe.middleware.rate_limit.APIRateLimitMiddleware",
    "hotframe.engine.boundary.ModuleBoundaryMiddleware",
    "hotframe.middleware.module_middleware.ModuleMiddlewareManager",
    "hotframe.auth.csrf.CSRFMiddleware",
    "hotframe.middleware.language.LanguageMiddleware",
    "hotframe.middleware.csp.CSPMiddleware",
    "hotframe.middleware.session_safe.RobustSessionMiddleware",
]
```

La lista define el stack de middleware en orden de ejecución (el primero es el más externo). Puedes sobreescribirla en tu subclase, pero en la práctica raramente necesitas hacerlo: el default cubre CSRF, CSP, sesiones, rate limit, observabilidad y gestión de módulos. El `ModuleBoundaryMiddleware` está deliberadamente fuera del `ModuleMiddlewareManager` para poder capturar excepciones lanzadas por el propio middleware de módulos.

#### Otros

| Campo | Tipo | Default | Qué controla |
|---|---|---|---|
| `LANGUAGE` | `str` | `"en"` | Idioma por defecto. |
| `CURRENCY` | `str` | `"USD"` | Moneda por defecto. |
| `OTEL_SERVICE_NAME` | `str` | `"hotframe"` | Nombre del servicio en OpenTelemetry. |
| `PROXY_FIX_ENABLED` | `bool` | `False` | Activa el middleware de proxy reverso. |
| `GLOBAL_CONTEXT_HOOK` | `str` | `""` | Dotted path a un callable async `(request) -> dict` inyectado en cada render de plantilla. |
| `HTTP_CLIENT_EVENTS` | `bool` | `False` | Emite eventos del bus (`http.request.*`) por cada llamada HTTP cliente. |
| `HTTP_INTERCEPTOR_PATHS` | `list[str]` | `[]` | Paths de ficheros .py con interceptores HTTP descubiertos al arrancar. |

---

### Validadores

#### `_normalize_log_level`

```python
@field_validator("LOG_LEVEL")
@classmethod
def _normalize_log_level(cls, v: str) -> str:
    v = v.upper()
    valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if v not in valid:
        raise ValueError(f"LOG_LEVEL must be one of {valid}, got {v!r}")
    return v
```

Convierte a mayúsculas y valida contra el conjunto de niveles válidos de Python logging.

#### `_resolve_path`

```python
@field_validator("MODULES_DIR", "MODULES_CACHE_DIR", mode="before")
@classmethod
def _resolve_path(cls, v: str | Path) -> Path:
    return Path(v).resolve()
```

Convierte `MODULES_DIR` y `MODULES_CACHE_DIR` a rutas absolutas en tiempo de validación, eliminando dependencias del directorio de trabajo actual.

#### `_validate_secrets_key`

```python
@model_validator(mode="after")
def _validate_secrets_key(self) -> HotframeSettings:
    if self.DEPLOYMENT_MODE != "local":
        if not self.SECRETS_KEY:
            raise ValueError("SECRETS_KEY is required in non-local deployments. ...")
    if self.SECRETS_KEY:
        decoded = base64.urlsafe_b64decode(self.SECRETS_KEY)
        if len(decoded) != 32:
            raise ValueError(...)
    return self
```

Garantiza que en cualquier deployment que no sea `"local"` exista una `SECRETS_KEY` válida (exactamente 32 bytes decodificados en base64-url, que es el formato de una clave Fernet).

---

### Propiedades computadas

#### `is_sqlite`

```python
@property
def is_sqlite(self) -> bool:
    return self.DATABASE_URL.startswith("sqlite")
```

Usada por `database.py` para omitir las opciones de pool (SQLite no las soporta) y por tests para detectar el entorno de CI.

#### `is_production`

```python
@property
def is_production(self) -> bool:
    return self.DEPLOYMENT_MODE == "web" and not self.DEBUG
```

Determina si el formato de log se fuerza a JSON y si se desactiva la documentación Swagger.

---

### Funciones del módulo

#### `get_settings() -> HotframeSettings`

Singleton lazy. La primera llamada instancia `HotframeSettings()` leyendo el entorno; las sucesivas devuelven la misma instancia. Es la función que usa todo el framework internamente.

#### `set_settings(settings: HotframeSettings) -> None`

Llamada por `create_app(settings)` para inyectar la instancia que el proyecto construyó. Esto permite que el proyecto sobreescriba el singleton antes de que cualquier pieza del framework lo lea.

#### `reset_settings() -> None`

Pone el singleton a `None`. Solo para tests: fuerza que la próxima llamada a `get_settings()` reconstruya la instancia desde el entorno actual.

---

## `database.py` — Motor async y sesiones

### Variables de módulo

```python
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
```

Singletons a nivel de módulo. Se inicializan en la primera llamada y se destruyen en `dispose_engine()`.

---

### `get_engine() -> AsyncEngine`

```python
def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict = {"echo": settings.DB_ECHO}
        if not settings.is_sqlite:
            kwargs.update(
                pool_size=settings.DB_POOL_SIZE,
                max_overflow=settings.DB_MAX_OVERFLOW,
                pool_recycle=settings.DB_POOL_RECYCLE,
                pool_pre_ping=True,
                pool_timeout=settings.DB_POOL_TIMEOUT,
            )
            if settings.DB_DISABLE_PREPARED_STATEMENTS and "asyncpg" in settings.DATABASE_URL:
                kwargs["connect_args"] = {
                    "prepared_statement_cache_size": 0,
                    "statement_cache_size": 0,
                }
        else:
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_async_engine(settings.DATABASE_URL, **kwargs)
    return _engine
```

Puntos clave:
- `pool_pre_ping=True` — antes de entregar una conexión del pool, ejecuta un `SELECT 1` para verificar que sigue viva. Previene errores silenciosos tras una desconexión de red.
- Cuando `DB_DISABLE_PREPARED_STATEMENTS=True` y el driver es asyncpg, pasa `prepared_statement_cache_size=0` y `statement_cache_size=0` al `connect_args`. Esto es obligatorio con transaction-mode poolers (RDS Proxy, PgBouncer, Supavisor) que rotan la conexión entre transacciones, invalidando el caché de prepared statements del cliente.
- SQLite recibe `check_same_thread=False` y no recibe opciones de pool.

---

### `get_session_factory() -> async_sessionmaker[AsyncSession]`

```python
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory
```

`expire_on_commit=False` es deliberado: los objetos ORM no se marcan como expirados tras el commit, lo que significa que puedes seguir accediendo a sus atributos sin relanzar una consulta. Esencial en handlers async donde la sesión ya está cerrada cuando se serializa la respuesta.

---

### `get_db() -> AsyncGenerator[AsyncSession, None]`

```python
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

Dependencia FastAPI. Cada request recibe una sesión nueva, hace commit automático al terminar, y rollback si lanza excepción. Uso típico:

```python
from hotframe.config.database import get_db
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

@router.get("/items")
async def list_items(db: AsyncSession = Depends(get_db)):
    ...
```

---

### `dispose_engine() -> None`

```python
async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
```

Llamada por `bootstrap.py` durante el shutdown del lifespan. Cierra todas las conexiones del pool de forma ordenada. También limpia los singletons, lo que permite reutilizar el proceso en tests.

---

## `paths.py` — Rutas efímeras del sistema

### Filosofía de diseño

hotframe asume un entorno de contenedores: el filesystem es **100% efímero**. Los datos persistentes van a S3 o a la base de datos. Todas las rutas locales son cachés que se reconstruyen en el próximo arranque.

### Clase `DataPaths`

```python
class DataPaths:
    def __init__(self, base: Path | None = None) -> None:
        if base is not None:
            self._base = base.resolve()
        elif env := os.environ.get("DATA_PATH"):
            self._base = Path(env).resolve()
        else:
            self._base = Path("/tmp/hotframe-data")
```

El directorio base se resuelve en este orden: argumento explícito → variable de entorno `DATA_PATH` → `/tmp/hotframe-data`.

Todos los atributos son `@cached_property`, lo que significa que el Path se construye una sola vez por instancia:

| Propiedad | Ruta | Uso |
|---|---|---|
| `base` | `_base` (configurable) | Directorio raíz de datos efímeros. |
| `media` | `/tmp/hotframe-media` | Caché de media en dev. En producción la media va a S3. |
| `modules` | `/tmp/modules` | Caché del código de módulos descargados de S3. Se reconstruye en cada arranque frío. |
| `reports` | `_base/reports` | Directorio temporal de generación de informes. Los informes finales van a S3. |
| `temp` | `_base/temp` | Directorio temporal de propósito general. |
| `cache` | `_base/cache` | Caché de propósito general. |

### `ensure_dirs() -> None`

```python
def ensure_dirs(self) -> None:
    for d in self.all_dirs:
        d.mkdir(parents=True, exist_ok=True)
```

Crea todas las rutas si no existen. Se llama desde el código de inicialización cuando se necesita escribir algo al disco antes de que el directorio exista.

### `all_dirs: list[Path]`

Propiedad (no cached) que devuelve la lista de todas las rutas relevantes. Usada por `ensure_dirs()` y por tests para verificar que el setup es correcto.

### Funciones del módulo

#### `get_data_paths() -> DataPaths`

Singleton lazy. Análogo a `get_settings()` pero para paths. Devuelve siempre la misma instancia dentro del proceso.

#### `reset_data_paths() -> None`

Solo para tests. Pone el singleton a `None`.

---

## Cómo encaja con el resto del framework

`config/` es la capa más baja y no importa ningún otro subpaquete de hotframe. Todo el resto del framework sí la importa:

- **`bootstrap.py`** llama a `get_settings()` en el paso 4b del lifespan, `get_engine()` en el paso 1, y `dispose_engine()` en el shutdown.
- **`discovery/scanner.py`** no importa `config/` directamente, pero el orquestador que lo llama (el engine) lee `settings.MODULES_DIR` para saber dónde escanear.
- **`apps/registry.py`** y **`engine/module_runtime.py`** leen `get_settings()` para configurar el runtime.
- **Todos los repositorios y servicios** reciben la sesión vía `get_db()` como dependencia FastAPI.

---

## Gotchas y decisiones de diseño

**`SECRET_KEY` se regenera en cada arranque si no se fija.** El default es `secrets.token_urlsafe(64)`, que produce un valor diferente en cada proceso. En producción **debes** fijar `SECRET_KEY` explícitamente, o las sesiones firmadas quedarán inválidas tras un reinicio.

**`DB_DISABLE_PREPARED_STATEMENTS` no es opcional con poolers de transacción.** Si usas RDS Proxy, PgBouncer en modo transaction, o cualquier proxy que rote la conexión entre transacciones, asyncpg cachea prepared statements por conexión TCP y falla al recibir una conexión reciclada. Activa este flag.

**`expire_on_commit=False` es intencional.** En frameworks async, acceder a atributos de un objeto ORM después de que la sesión se cierra provocaría `MissingGreenlet` o `DetachedInstanceError`. El flag evita que SQLAlchemy marque los atributos como expirados al hacer commit.

**Los paths son efímeros por diseño.** No guardes nada importante bajo `/tmp/hotframe-*`. Si el contenedor se reinicia, esos datos desaparecen. El código que usa `DataPaths` asume que en cualquier momento esas rutas pueden no existir: por eso `ensure_dirs()` existe y se llama antes de escribir.

**`INSTALLED_APPS` no existe en hotframe.** Las apps se descubren automáticamente desde `apps/`. Puedes usar `EXTRA_ROUTERS` para montar routers adicionales que no sigan la estructura de carpetas convencional.
