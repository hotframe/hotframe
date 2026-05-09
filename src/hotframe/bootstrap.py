# SPDX-License-Identifier: Apache-2.0
"""
FastAPI application factory with lifespan management.

Creates the application, initializes core systems on startup,
and cleans up resources on shutdown. Application-specific setup
(routers, models, services) is done by the user in their project.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from hotframe.config.settings import HotframeSettings

logger = logging.getLogger("hotframe")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown lifecycle."""
    t0 = time.monotonic()

    # 1. Initialize database engine
    from hotframe.config.database import get_engine

    engine = get_engine()
    logger.info("Database engine initialized: %s", engine.url.render_as_string(hide_password=True))

    # 2. Create core registries
    from hotframe.components.registry import ComponentRegistry
    from hotframe.signals.dispatcher import AsyncEventBus
    from hotframe.signals.hooks import HookRegistry
    from hotframe.templating.slots import SlotRegistry

    event_bus = AsyncEventBus()
    hooks = HookRegistry()
    slots = SlotRegistry()
    components = ComponentRegistry()

    # 2b. Create broadcast hub (SSE/WS real-time fan-out)
    from hotframe.views.broadcast import BroadcastHub

    broadcast_hub = BroadcastHub()
    app.state.broadcast_hub = broadcast_hub

    # 3. Setup ORM event listeners (SQLAlchemy -> EventBus bridge)
    from hotframe.models.base import Base
    from hotframe.orm.events import setup_orm_events

    setup_orm_events(event_bus, base=Base)

    # 4. Store core systems on app.state for dependency injection
    app.state.event_bus = event_bus
    app.state.hooks = hooks
    app.state.slots = slots
    app.state.components = components

    # 4b. Resolve settings early — used by both the HTTP client registry
    # (for ambient interceptor discovery) and the template engine below.
    from hotframe.config.settings import get_settings

    settings = get_settings()

    # 4c. HTTP client registry — named, authenticated httpx clients.
    # Projects and modules register clients here; the registry closes
    # every one on shutdown so no connection pool leaks.
    #
    # Interceptors are discovered from ``settings.HTTP_INTERCEPTOR_PATHS``
    # (a list of filesystem paths containing Python files that define
    # module-level :class:`Interceptor` instances) and installed as the
    # registry's ambient pool. Any client registered later without an
    # explicit ``interceptors=`` is auto-wrapped in the interceptors
    # whose ``applies_to`` matcher picks its name.
    from pathlib import Path as _Path

    from hotframe.http import HttpClientRegistry, discover_interceptors

    interceptor_paths = getattr(settings, "HTTP_INTERCEPTOR_PATHS", [])
    if interceptor_paths:
        try:
            discovered = discover_interceptors([_Path(p) for p in interceptor_paths])
        except Exception:
            logger.exception("Failed to discover HTTP interceptors from %s", interceptor_paths)
            discovered = []
    else:
        discovered = []
    app.state.http_interceptors = discovered
    app.state.http_clients = HttpClientRegistry(ambient_interceptors=discovered)

    # 5. Initialize Jinja2 template engine
    from hotframe.templating.engine import create_template_engine

    app.state.templates = create_template_engine(modules_dir=settings.MODULES_DIR)

    # Expose the component registry to the Jinja2 environment so the
    # ``render_component`` global and ``{% component %}`` / ``{% live %}``
    # tags can resolve entries without having to reach into ``app.state``
    # at render time.
    app.state.templates.env.globals["_hotframe_components"] = components

    # Live runtime — owns the per-WS sessions and the dispatch loop.
    # Components run their on_mount/event handlers through this object.
    from hotframe.live.runtime import LiveRuntime

    app.state.live = LiveRuntime(components, app.state.templates.env)

    # 6. Initialize ModuleRuntime
    from hotframe.engine.module_runtime import ModuleRuntime

    runtime = ModuleRuntime(app, settings, event_bus, hooks, slots, components=components)
    app.state.module_runtime = runtime
    app.state.module_registry = runtime.registry

    # 7. Components — discover every project app, then mount routers + static.
    # Module components are discovered/mounted by the loader on module load.
    from pathlib import Path as _Path

    from hotframe.components.discovery import discover_apps_components
    from hotframe.components.mounting import (
        mount_component_routers,
        mount_component_static,
    )

    discover_apps_components(components, _Path.cwd() / "apps")
    mount_component_routers(app, components)
    mount_component_static(app, components)

    # 8. Boot: mount every DB-active module's router into the live FastAPI
    # app so ``/m/<module_id>/`` routes exist from the first request after
    # a restart. Without this pass, ``status='active'`` rows persist in the
    # DB but their handlers return 404 until the user clicks Activate again
    # from the marketplace. Failures are logged and swallowed — a broken
    # module must not prevent the rest of the app from starting.
    from hotframe.config.database import get_session_factory

    try:
        session_factory = get_session_factory()
        async with session_factory() as boot_session:
            # AsyncSession satisfies the ISession protocol structurally —
            # mypy can't see the equivalence without an explicit cast and
            # we don't want to leak SQLAlchemy types into hotframe's public
            # boot signature.
            count = await runtime.boot_all_active_modules(boot_session)  # type: ignore[arg-type]
            await boot_session.commit()
        logger.info("Boot: mounted routers for %d active module(s)", count)
    except Exception:
        logger.exception("Boot: failed to mount active modules (continuing startup)")

    elapsed = (time.monotonic() - t0) * 1000
    logger.info("Application started in %.0fms", elapsed)

    yield

    # --- SHUTDOWN ---
    if app.state.module_runtime is not None:
        await app.state.module_runtime.shutdown()

    # Drain every live session — runs on_unmount on each component and
    # closes the dict so memory is released cleanly.
    live_runtime = getattr(app.state, "live", None)
    if live_runtime is not None:
        await live_runtime.shutdown()

    # Close every HTTP client still registered — project-scoped clients
    # live for the process, and modules may have skipped their own
    # unregister on deactivate. The registry is defensive and swallows
    # per-client close errors.
    http_clients = getattr(app.state, "http_clients", None)
    if http_clients is not None:
        await http_clients.aclose_all()

    from hotframe.config.database import dispose_engine

    await dispose_engine()
    logger.info("Application shutdown complete")


def create_app(settings: HotframeSettings | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        settings: Application settings instance. If None, loads from
                  environment using ``HotframeSettings()``.

    Returns:
        Configured FastAPI application.
    """
    from hotframe.config.settings import get_settings, set_settings

    if settings is not None:
        set_settings(settings)
    settings = get_settings()

    # --- Observability ---
    from hotframe.utils.observability_logging import setup_logging
    from hotframe.utils.observability_telemetry import setup_telemetry

    json_output = settings.LOG_FORMAT == "json" or (
        settings.LOG_FORMAT == "console" and settings.is_production
    )
    setup_logging(log_level=settings.LOG_LEVEL, json_output=json_output)
    # Skip telemetry setup under pytest: the BatchSpanProcessor + console
    # exporter spawn a background thread that writes to stderr after
    # pytest closes its capture stream, producing post-run "I/O operation
    # on closed file" tracebacks that flip CI exit codes despite all
    # tests passing. Real apps and explicit OTLP endpoints are unaffected.
    import sys as _sys

    in_pytest = "pytest" in _sys.modules
    try:
        if not in_pytest:
            setup_telemetry(
                debug=settings.DEBUG,
                service_name=settings.OTEL_SERVICE_NAME,
            )
    except Exception as exc:
        logger.warning("Telemetry setup failed (non-fatal): %s", exc)

    app = FastAPI(
        title=settings.APP_TITLE,
        version="0.1.0",
        docs_url="/api/docs" if settings.DEBUG else None,
        redoc_url="/api/redoc" if settings.DEBUG else None,
        openapi_url="/api/openapi.json" if settings.DEBUG else None,
        lifespan=lifespan,
    )

    # --- Middleware stack (from settings.MIDDLEWARE) ---
    from hotframe.middleware.stack import build_middleware_stack

    build_middleware_stack(app, settings)

    # --- CORS (optional — enabled when CORS_ORIGINS is set) ---
    if settings.CORS_ORIGINS:
        from starlette.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_methods=settings.CORS_METHODS,
            allow_headers=settings.CORS_HEADERS,
            allow_credentials=settings.CORS_CREDENTIALS,
        )

    # --- Proxy fix (optional) ---
    if settings.PROXY_FIX_ENABLED:
        from hotframe.middleware.proxy_fix import ProxyFixMiddleware

        app.add_middleware(
            ProxyFixMiddleware,
            slug=settings.PROXY_SLUG,
            domain_base=settings.PROXY_DOMAIN_BASE,
            ecs_region=settings.PROXY_AWS_REGION,
        )

    # --- Rate limiter singleton ---
    from hotframe.auth.rate_limit import PINRateLimiter

    app.state.rate_limiter = PINRateLimiter()

    # --- Broadcast router (SSE real-time) ---
    from hotframe.views.broadcast import broadcast_router

    app.include_router(broadcast_router)

    # --- Live runtime WebSocket endpoint ---
    # Mounts /ws/_live for stateful component sessions. The session id
    # is derived from the signed cookie at handshake time (see
    # hotframe.live.ws._resolve_session_id).
    from hotframe.live.ws import live_router

    app.include_router(live_router)

    # --- Health check ---
    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok"}

    # --- Auto-discover app routers ---
    _auto_discover_apps(app)

    # --- Static files ---
    from pathlib import Path as _Path

    from fastapi.staticfiles import StaticFiles
    from starlette.responses import Response as _Response

    class CachedStaticFiles(StaticFiles):
        """StaticFiles subclass that adds long-lived Cache-Control headers.

        ``public, max-age=31536000, immutable`` tells browsers (and CDNs) to
        cache fingerprinted assets for one year without revalidation.  Only
        applied to the ``/static/`` mount — not to media files.
        """

        async def get_response(self, path: str, scope) -> _Response:
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return response

    static_root = _Path(settings.STATIC_ROOT).resolve()
    if static_root.exists():
        app.mount(settings.STATIC_URL, CachedStaticFiles(directory=str(static_root)), name="static")

    # --- Hotframe-shipped static assets ---
    # Mount the framework's own static directory at /static/hotframe/.
    # Today this serves the live runtime client (live.js + morphdom).
    # Always mounted because the live layer is built-in, not optional.
    from hotframe import live as _live_pkg

    live_static_dir = _Path(_live_pkg.__file__).parent / "static"
    if live_static_dir.is_dir():
        app.mount(
            "/static/hotframe",
            CachedStaticFiles(directory=str(live_static_dir)),
            name="hotframe-static",
        )

    # --- Media files (local dev only) ---
    if settings.MEDIA_STORAGE == "local" and settings.DEBUG:
        media_root = _Path(settings.MEDIA_ROOT).resolve()
        media_root.mkdir(parents=True, exist_ok=True)
        app.mount(settings.MEDIA_URL, StaticFiles(directory=str(media_root)), name="media")

    # --- Error handlers ---
    login_url = settings.AUTH_LOGIN_URL

    @app.exception_handler(401)
    async def unauthorized_handler(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
            )
        from starlette.responses import RedirectResponse

        return RedirectResponse(url=login_url, status_code=302)

    @app.exception_handler(403)
    async def forbidden_handler(request: Request, exc):
        templates = request.app.state.templates
        nonce = getattr(request.state, "csp_nonce", "")
        return templates.TemplateResponse(
            request,
            "errors/403.html",
            {"request": request, "csp_nonce": nonce},
            status_code=403,
        )

    @app.exception_handler(405)
    async def method_not_allowed_handler(request: Request, exc):
        templates = request.app.state.templates
        nonce = getattr(request.state, "csp_nonce", "")
        return templates.TemplateResponse(
            request,
            "errors/405.html",
            {"request": request, "csp_nonce": nonce},
            status_code=405,
        )

    return app


# ---------------------------------------------------------------------------
# App auto-discovery
# ---------------------------------------------------------------------------


def _auto_discover_apps(app: FastAPI) -> None:
    """Auto-discover and mount app routers from apps/ directory.

    Scans ``apps/*/`` for ``routes.py`` (HTML views) and ``api.py``
    (REST API) and mounts any ``router`` / ``api_router`` found.

    Also calls ``AppConfig.ready()`` if ``app.py`` defines one. Because
    this function runs synchronously during ``create_app`` (before the
    lifespan has started), an async ``ready`` is scheduled on a fresh
    event loop instead of being awaited inline.

    If ``settings.INSTALLED_APPS`` is set, only those apps are loaded
    (in that order). Otherwise, all apps in ``apps/`` are loaded
    alphabetically.
    """
    import asyncio
    import importlib
    import inspect
    from pathlib import Path

    from hotframe.config.settings import get_settings

    settings = get_settings()
    apps_dir = Path.cwd() / "apps"

    if not apps_dir.exists():
        return

    # Auto-discover all apps
    app_names = sorted(
        d.name
        for d in apps_dir.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_")) and (d / "__init__.py").exists()
    )

    mounted = []

    for name in app_names:
        app_dir = apps_dir / name
        if not app_dir.exists():
            logger.warning("INSTALLED_APPS: app '%s' not found in apps/", name)
            continue

        # Mount views router (routes.py → router)
        try:
            mod = importlib.import_module(f"apps.{name}.routes")
            router = getattr(mod, "router", None)
            if router:
                app.include_router(router)
                mounted.append(name)
        except ImportError:
            pass
        except Exception:
            logger.exception("Failed to load routes for app '%s'", name)

        # Mount API router (api.py → api_router)
        try:
            mod = importlib.import_module(f"apps.{name}.api")
            api_router = getattr(mod, "api_router", None)
            if api_router:
                app.include_router(api_router)
        except ImportError:
            pass
        except Exception:
            logger.exception("Failed to load API for app '%s'", name)

        # Call AppConfig.ready() if defined. AppConfig subclasses may
        # declare ``ready`` as either a plain or ``async def`` method; we
        # must run both correctly. ``_auto_discover_apps`` itself runs
        # synchronously from ``create_app`` (outside the lifespan), so an
        # async ``ready`` is executed on a transient event loop here.
        try:
            mod = importlib.import_module(f"apps.{name}.app")
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (
                    isinstance(attr, type)
                    and hasattr(attr, "ready")
                    and hasattr(attr, "name")
                    and getattr(attr, "name", None) == name
                ):
                    config = attr()
                    ready_callable = config.ready
                    if callable(ready_callable):
                        if inspect.iscoroutinefunction(ready_callable):
                            asyncio.run(ready_callable())
                        else:
                            ready_callable()
                    break
        except ImportError:
            pass
        except Exception:
            logger.exception("Failed to call ready() for app '%s'", name)

    # Mount extra routers from settings
    for dotted_path in settings.EXTRA_ROUTERS:
        try:
            module_path, attr_name = dotted_path.rsplit(".", 1)
            mod = importlib.import_module(module_path)
            router = getattr(mod, attr_name)
            app.include_router(router)
            mounted.append(f"extra:{dotted_path}")
        except Exception:
            logger.exception("Failed to load extra router: %s", dotted_path)

    if mounted:
        logger.info("Auto-discovered %d app(s): %s", len(mounted), ", ".join(mounted))
