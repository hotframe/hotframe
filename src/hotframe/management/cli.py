# SPDX-License-Identifier: Apache-2.0
"""
Hotframe CLI — project management commands.

Usage::

    hf startproject myapp
    hf startapp accounts
    hf startmodule blog
    hf runserver
    hf migrate
    hf makemigrations
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent

import typer

app = typer.Typer(
    name="hotframe",
    help="Hotframe — Modular Python web framework CLI.",
    no_args_is_help=True,
)


def _load_project_settings():
    """Load the project's settings (not hotframe's defaults).

    Tries to import ``settings`` from the project root (CWD/settings.py).
    This ensures the CLI uses the project's DATABASE_URL, env_prefix, etc.
    Falls back to hotframe's HotframeSettings if no project settings found.
    """
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


# ---------------------------------------------------------------------------
# startproject
# ---------------------------------------------------------------------------


@app.command()
def startproject(name: str) -> None:
    """Create a new hotframe project. Use '.' to create in the current directory."""
    if name == ".":
        project_dir = Path.cwd()
        name = project_dir.name
        # Check it's empty enough (allow .venv, pyproject.toml, uv.lock)
        existing = {p.name for p in project_dir.iterdir()} - {
            ".venv",
            "pyproject.toml",
            "uv.lock",
            ".git",
            ".gitignore",
            "__pycache__",
            ".python-version",
        }
        if existing:
            typer.echo(
                f"Error: directory is not empty. Found: {', '.join(sorted(existing))}", err=True
            )
            raise typer.Exit(1)
    else:
        project_dir = Path(name)
        if project_dir.exists():
            typer.echo(f"Error: directory '{name}' already exists.", err=True)
            raise typer.Exit(1)
        project_dir.mkdir(parents=True)

    # main.py
    (project_dir / "main.py").write_text(
        dedent("""\
        from hotframe import create_app
        from settings import settings

        app = create_app(settings)
    """)
    )

    # asgi.py
    (project_dir / "asgi.py").write_text(
        dedent("""\
        from main import app  # noqa: F401
        # uvicorn asgi:app
    """)
    )

    # settings.py
    (project_dir / "settings.py").write_text(
        dedent(f'''\
        from hotframe import HotframeSettings
        from pydantic_settings import SettingsConfigDict


        class Settings(HotframeSettings):
            model_config = SettingsConfigDict(
                env_prefix="{name.upper()}_",
                env_file=".env",
                env_file_encoding="utf-8",
                case_sensitive=False,
                extra="ignore",
            )

            APP_TITLE: str = "{name.replace("_", " ").title()}"

            # -----------------------------------------------------------------
            # Auth (uncomment and configure when you add user authentication)
            # -----------------------------------------------------------------
            # AUTH_USER_MODEL: str = "apps.accounts.models.User"
            # AUTH_LOGIN_URL: str = "/login"
            # AUTH_UNAUTHORIZED_URL: str = "/unauthorized"
            # PERMISSION_RESOLVER: str = ""

            # -----------------------------------------------------------------
            # CORS (uncomment to enable cross-origin requests)
            # -----------------------------------------------------------------
            # CORS_ORIGINS: list[str] = ["http://localhost:3000"]
            # CORS_METHODS: list[str] = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
            # CORS_HEADERS: list[str] = ["*"]
            # CORS_CREDENTIALS: bool = True

            # -----------------------------------------------------------------
            # CSRF (override to add exempt routes)
            # -----------------------------------------------------------------
            # CSRF_EXEMPT_PREFIXES: list[str] = ["/api/", "/health", "/static/"]

            # -----------------------------------------------------------------
            # Rate limiting
            # -----------------------------------------------------------------
            # RATE_LIMIT_API: int = 120          # requests/min for /api/
            # RATE_LIMIT_AUTH: int = 60           # requests/min for auth routes
            # RATE_LIMIT_AUTH_PREFIXES: list[str] = []

            # -----------------------------------------------------------------
            # Session
            # -----------------------------------------------------------------
            # SESSION_COOKIE_NAME: str = "session"
            # SESSION_MAX_AGE: int = 2592000     # 30 days

            # -----------------------------------------------------------------
            # Static & Media
            # -----------------------------------------------------------------
            # STATIC_ROOT: str = "./static"
            # STATIC_URL: str = "/static/"
            # MEDIA_ROOT: str = "./media"
            # MEDIA_URL: str = "/media/"
            # MEDIA_STORAGE: str = "local"       # "local" or "s3"
            # MEDIA_S3_BUCKET: str = ""

            # -----------------------------------------------------------------
            # CSP (Content Security Policy)
            #
            # CSP_ENFORCE = False (default): report-only mode, logs violations
            # CSP_ENFORCE = True: blocks any resource not explicitly allowed
            #
            # CSP_TRUSTED_TYPES = True (default): enables Trusted Types policy
            # in the CSP header and renders the JS policy in base.html.
            #
            # CSP_ALLOWED_SOURCES: allow-list of external domains per resource
            # type. The example below shows the CDNs used by base.html. Add
            # your own domains as needed (S3 buckets, Google Fonts, Stripe...).
            # -----------------------------------------------------------------
            # CSP_ENFORCE: bool = False
            # CSP_TRUSTED_TYPES: bool = True
            # CSP_ALLOWED_SOURCES: dict[str, list[str]] = {{
            #     "script": [                         # <script src="...">
            #         "https://cdn.jsdelivr.net",     # CDN-hosted libraries
            #     ],
            #     "style": [],                        # <link rel="stylesheet">
            #     "connect": [],                      # fetch(), WebSocket
            #     "img": [],                          # <img src="...">
            #     "font": [],                         # @font-face
            # }}

            # -----------------------------------------------------------------
            # Modules
            # -----------------------------------------------------------------
            # MODULE_MARKETPLACE_URL: str = ""
            # MODULE_STATE_MODEL: str = ""

            # -----------------------------------------------------------------
            # Extra routers (dotted paths, for routers outside apps/)
            # -----------------------------------------------------------------
            # EXTRA_ROUTERS: list[str] = []

            # -----------------------------------------------------------------
            # Template context hook (async callable: request -> dict)
            # -----------------------------------------------------------------
            # GLOBAL_CONTEXT_HOOK: str = ""

            # -----------------------------------------------------------------
            # Middleware (override to add/remove/reorder)
            # -----------------------------------------------------------------
            # MIDDLEWARE: list[str] = [
            #     "hotframe.middleware.timeout.TimeoutMiddleware",
            #     "hotframe.middleware.error_pages.ErrorPageMiddleware",
            #     "hotframe.middleware.body_limit.BodyLimitMiddleware",
            #     "asgi_correlation_id.CorrelationIdMiddleware",
            #     "hotframe.middleware.observability.RequestObservabilityMiddleware",
            #     "hotframe.middleware.rate_limit.APIRateLimitMiddleware",
            #     "hotframe.middleware.module_middleware.ModuleMiddlewareManager",
            #     "hotframe.auth.csrf.CSRFMiddleware",
            #     "hotframe.middleware.language.LanguageMiddleware",
            #     "hotframe.middleware.csp.CSPMiddleware",
            #     "starlette.middleware.sessions.SessionMiddleware",
            # ]


        settings = Settings()
    ''')
    )

    # manage.py
    (project_dir / "manage.py").write_text(
        dedent('''\
        #!/usr/bin/env python
        """Management CLI — delegates to hotframe."""
        from hotframe.management.cli import app

        if __name__ == "__main__":
            app()
    ''')
    )

    # .env
    (project_dir / ".env").write_text(
        dedent("""\
        # Database (SQLite for development)
        DATABASE_URL=sqlite+aiosqlite:///./app.db
        SECRET_KEY=change-me-in-production
        DEBUG=true
    """)
    )

    # .gitignore
    (project_dir / ".gitignore").write_text(
        dedent("""\
        # Python
        __pycache__/
        *.py[cod]
        *.egg-info/
        dist/
        build/
        .venv/

        # Cache (pytest, ruff, mypy)
        .cache/

        # Environment
        .env

        # Database
        *.db
        *.sqlite3

        # IDE
        .vscode/
        .idea/
    """)
    )

    # pyproject.toml — skip if already exists (user may have uv.lock, custom deps)
    if not (project_dir / "pyproject.toml").exists():
        (project_dir / "pyproject.toml").write_text(
            dedent(f'''\
            [project]
            name = "{name}"
            version = "0.1.0"
            requires-python = ">=3.12"
            dependencies = [
                "hotframe",
            ]

            [project.optional-dependencies]
            dev = [
                "pytest>=8.0",
                "pytest-asyncio>=0.24",
                "ruff>=0.7",
            ]

            [tool.pytest.ini_options]
            asyncio_mode = "auto"
            testpaths = ["tests"]
            cache_dir = ".cache/pytest"

            [tool.ruff]
            cache-dir = ".cache/ruff"
            line-length = 100

            [tool.mypy]
            cache_dir = ".cache/mypy"
        ''')
        )

    # apps/ directory
    apps_dir = project_dir / "apps"
    apps_dir.mkdir(exist_ok=True)
    (apps_dir / "__init__.py").write_text("")

    # apps/shared/ — base app with welcome page
    shared_dir = apps_dir / "shared"
    shared_dir.mkdir(parents=True)
    (shared_dir / "__init__.py").write_text("")

    (shared_dir / "app.py").write_text(
        dedent(f'''\
        from hotframe import AppConfig


        class SharedConfig(AppConfig):
            name = "shared"
            verbose_name = "{name.replace("_", " ").title()} Shared"

            def ready(self):
                pass
    ''')
    )

    (shared_dir / "routes.py").write_text(
        dedent('''\
        """Shared routes — index page and base endpoints."""
        from fastapi import APIRouter, Request
        from fastapi.responses import HTMLResponse

        router = APIRouter()


        @router.get("/", response_class=HTMLResponse)
        async def index(request: Request):
            """Index page — proves the app is running."""
            templates = getattr(request.app.state, "templates", None)
            if templates:
                return templates.TemplateResponse(
                    request, "shared/index.html",
                    {"request": request, "app_title": request.app.title},
                )
            return HTMLResponse(
                f"<h1>{request.app.title}</h1>"
                f"<p>Powered by <a href=\\"https://hotframe.dev\\">hotframe</a></p>"
            )
    ''')
    )

    # apps/shared/templates/
    shared_tpl = shared_dir / "templates" / "shared"
    shared_tpl.mkdir(parents=True)

    (shared_tpl / "base.html").write_text(
        dedent(f"""\
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
            <title>{{%- block title %}}{name.replace("_", " ").title()}{{%- endblock %}}</title>

            {{# Optional Trusted Types policy — enable via CSP_TRUSTED_TYPES setting. #}}
            {{%- if csp_trusted_types %}}
            <script nonce="{{{{ csp_nonce }}}}">
            if (window.trustedTypes && trustedTypes.createPolicy) {{
                trustedTypes.createPolicy('default', {{
                    createHTML: (s) => s,
                    createScript: (s) => s,
                    createScriptURL: (s) => s,
                }});
            }}
            </script>
            {{%- endif %}}

            {{# Iconify (icon system via CDN) — optional, remove if unused. #}}
            <script src="https://cdn.jsdelivr.net/npm/@iconify/iconify@3.1.1/dist/iconify.min.js" defer nonce="{{{{ csp_nonce }}}}"></script>

            {{# Live runtime client + morphdom (served from /static/hotframe/). #}}
            {{{{ live_assets() }}}}

            {{%- block head_extra %}}{{%- endblock %}}
        </head>
        <body {{%- block body_attrs %}}{{%- endblock %}}>

            {{%- block body %}}
            {{%- block content %}}{{%- endblock %}}
            {{%- endblock %}}

            {{# Toast container — live components dispatch hf:toast events. #}}
            <div id="toast-container" style="position:fixed; bottom:1rem; left:50%; transform:translateX(-50%); z-index:9999; display:flex; flex-direction:column; gap:0.5rem; align-items:center;"></div>

            <script nonce="{{{{ csp_nonce }}}}">
            (function() {{
                window.showToast = function(message, type, duration) {{
                    type = type || 'info';
                    duration = duration || 4000;
                    var container = document.getElementById('toast-container');
                    if (!container) return;
                    var el = document.createElement('div');
                    el.style.cssText = 'display:flex; align-items:center; gap:0.5rem; padding:0.75rem 1rem; border-radius:0.5rem; background:#1f2937; color:#fff; font-size:0.875rem; box-shadow:0 4px 12px rgba(0,0,0,0.15); transition:opacity 0.3s, transform 0.3s; max-width:24rem;';
                    el.textContent = message;
                    el.onclick = function() {{ el.remove(); }};
                    container.appendChild(el);
                    setTimeout(function() {{ el.remove(); }}, duration);
                }};

                document.addEventListener('hf:toast', function(e) {{
                    var d = e.detail || {{}};
                    if (d.msg) showToast(d.msg, d.level, d.level === 'error' ? 5000 : 4000);
                }});
            }})();
            </script>

            {{%- block scripts %}}{{%- endblock %}}
        </body>
        </html>
    """)
    )

    (shared_tpl / "index.html").write_text(
        dedent("""\
        {% extends "shared/base.html" %}

        {% block content %}
        <h1>{{ app_title }}</h1>
        <p>Your hotframe application is running.</p>
        <hr>
        <h3>Next steps</h3>
        <ul>
            <li><code>hf startapp accounts</code> — create your first app</li>
            <li><code>hf startmodule blog</code> — create a dynamic module</li>
            <li>Edit <code>settings.py</code> to configure your project</li>
        </ul>
        <p><small>Powered by <a href="https://hotframe.dev">hotframe</a></small></p>
        {% endblock %}
    """)
    )

    (shared_tpl.parent / "errors").mkdir()
    for code, msg in [("404", "Page not found"), ("500", "Server error")]:
        ((shared_tpl.parent / "errors") / f"{code}.html").write_text(
            dedent(f"""\
            {{% extends "shared/base.html" %}}
            {{% block title %}}{code} - {msg}{{% endblock %}}
            {{% block content %}}<h1>{code}</h1><p>{msg}</p>{{% endblock %}}
        """)
        )

    # apps/shared/components/ — example components (alert, badge).
    # These are scaffolding: the project owner may keep, modify, or
    # delete them freely. Hotframe discovers them at boot via the
    # standard apps/<app>/components/<name>/ convention.
    shared_components = shared_dir / "components"
    shared_components.mkdir()

    alert_dir = shared_components / "alert"
    alert_dir.mkdir()
    (alert_dir / "template.html").write_text(
        dedent("""\
        {# Example component generated by `hf startproject`. Delete or modify as needed. #}
        {# Usage: {% component 'alert' type='warning' %}body{% endcomponent %} #}
        <div class="alert alert-{{ type | default('info') }}" role="alert">
            {{ body }}
        </div>
    """)
    )

    badge_dir = shared_components / "badge"
    badge_dir.mkdir()
    (badge_dir / "template.html").write_text(
        dedent("""\
        {# Example component generated by `hf startproject`. Delete or modify as needed. #}
        {# Usage: {{ render_component('badge', text='New', variant='primary') }} #}
        <span class="badge badge-{{ variant | default('default') }}">{{ text }}</span>
    """)
    )

    # modules/ directory
    modules_dir = project_dir / "modules"
    modules_dir.mkdir(exist_ok=True)

    # tests/ directory
    tests_dir = project_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "conftest.py").write_text(
        dedent('''\
        """Shared test fixtures."""
        import pytest

        from hotframe.testing import create_test_app, test_db_session


        @pytest.fixture
        async def app():
            """Create a test application."""
            return create_test_app()


        @pytest.fixture
        async def db(app):
            """Create a test database session."""
            async for session in test_db_session():
                yield session
    ''')
    )

    typer.echo(f"Created project '{name}'")
    if name != project_dir.name or str(project_dir) != str(Path.cwd()):
        typer.echo(f"  cd {name}")
    typer.echo("  hf runserver")


# ---------------------------------------------------------------------------
# startapp
# ---------------------------------------------------------------------------


@app.command()
def startapp(name: str) -> None:
    """Create a new app inside apps/."""
    app_dir = Path("apps") / name
    if app_dir.exists():
        typer.echo(f"Error: app '{name}' already exists.", err=True)
        raise typer.Exit(1)

    app_dir.mkdir(parents=True)

    (app_dir / "__init__.py").write_text("")

    (app_dir / "app.py").write_text(
        dedent(f'''\
        from hotframe import AppConfig


        class {name.title().replace("_", "")}Config(AppConfig):
            name = "{name}"
            verbose_name = "{name.replace("_", " ").title()}"

            def ready(self):
                pass
    ''')
    )

    (app_dir / "models.py").write_text(
        dedent('''\
        """SQLAlchemy models."""
        from hotframe import Base
        # Define your models here
    ''')
    )

    (app_dir / "routes.py").write_text(
        dedent(f'''\
        """HTML view routes."""
        from fastapi import APIRouter

        router = APIRouter(prefix="/{name}", tags=["{name}"])
    ''')
    )

    (app_dir / "api.py").write_text(
        dedent(f'''\
        """REST API endpoints."""
        from fastapi import APIRouter

        api_router = APIRouter(prefix="/api/v1/{name}", tags=["{name}"])
    ''')
    )

    templates_dir = app_dir / "templates" / name
    templates_dir.mkdir(parents=True)
    (templates_dir / "pages").mkdir()
    (templates_dir / "partials").mkdir()

    migrations_dir = app_dir / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "versions").mkdir()
    (migrations_dir / "env.py").write_text(_generate_env_py(name))
    (migrations_dir / "script.py.mako").write_text(_generate_script_mako())

    tests_dir = app_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")

    typer.echo(f"Created app 'apps/{name}/'")


# ---------------------------------------------------------------------------
# startmodule
# ---------------------------------------------------------------------------


@app.command()
def startmodule(
    name: str,
    api_only: bool = typer.Option(False, "--api-only", help="API only, no HTML views"),
    system: bool = typer.Option(
        False,
        "--system",
        help="Mark the module as a system module (is_system=True, cannot be uninstalled)",
    ),
) -> None:
    """Create a new dynamic module inside modules/.

    Examples::

        hf startmodule blog              # views + API (default)
        hf startmodule payments --api-only   # API only
        hf startmodule audit --system        # system module
    """
    mod_dir = Path("modules") / name
    if mod_dir.exists():
        typer.echo(f"Error: module '{name}' already exists.", err=True)
        raise typer.Exit(1)

    has_views = not api_only and not system
    has_api = not system
    class_name = name.title().replace("_", "")
    verbose = name.replace("_", " ").title()

    mod_dir.mkdir(parents=True)

    (mod_dir / "__init__.py").write_text("")

    # module.py
    (mod_dir / "module.py").write_text(
        dedent(f'''\
        from hotframe import ModuleConfig


        class {class_name}Module(ModuleConfig):
            name = "{name}"
            verbose_name = "{verbose}"
            version = "1.0.0"
            is_system = {system}
            has_views = {has_views}
            has_api = {has_api}
            requires_restart = False
            dependencies = []

            async def ready(self) -> None:
                pass

            async def install(self, ctx) -> None:
                pass

            async def uninstall(self, ctx) -> None:
                pass
    ''')
    )

    # models.py
    (mod_dir / "models.py").write_text(
        dedent('''\
        """SQLAlchemy models."""
        from hotframe import Base
        # Define your models here
    ''')
    )

    # routes.py (views)
    if has_views:
        (mod_dir / "routes.py").write_text(
            dedent(f'''\
            """HTML view routes for {verbose}."""
            from fastapi import APIRouter, Request
            from fastapi.responses import HTMLResponse

            router = APIRouter(prefix="/m/{name}", tags=["{name}"])


            @router.get("/", response_class=HTMLResponse)
            async def index(request: Request):
                """Module landing page."""
                return request.app.state.templates.TemplateResponse(
                    request, "{name}/pages/index.html", {{
                        "request": request,
                        "module_name": "{verbose}",
                    }},
                )
        ''')
        )

        # Template
        templates_dir = mod_dir / "templates" / name
        templates_dir.mkdir(parents=True)
        (templates_dir / "pages").mkdir()
        (templates_dir / "partials").mkdir()
        (templates_dir / "pages" / "index.html").write_text(
            dedent(f"""\
            {{% extends "shared/base.html" %}}
            {{% block title %}}{verbose}{{% endblock %}}
            {{% block content %}}
            <h1>{verbose}</h1>
            <p>Module <strong>{name}</strong> is installed and running.</p>
            <p><a href="/">&larr; Home</a></p>
            {{% endblock %}}
        """)
        )

    # api.py
    if has_api:
        (mod_dir / "api.py").write_text(
            dedent(f'''\
            """REST API for {verbose}."""
            from fastapi import APIRouter

            api_router = APIRouter(prefix="/api/v1/{name}", tags=["{name}"])


            @api_router.get("/")
            async def list_items():
                """List items."""
                return {{"module": "{name}", "items": []}}
        ''')
        )

    # migrations/
    migrations_dir = mod_dir / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "versions").mkdir()
    (migrations_dir / "env.py").write_text(_generate_env_py(name))
    (migrations_dir / "script.py.mako").write_text(_generate_script_mako())

    # tests/
    tests_dir = mod_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")

    parts = []
    if has_views:
        parts.append("views")
    if has_api:
        parts.append("API")
    if system:
        parts.append("system")
    mode = " + ".join(parts) if parts else "minimal"

    typer.echo(f"Created module 'modules/{name}/' ({mode})")


# ---------------------------------------------------------------------------
# modules (subcommand group)
# ---------------------------------------------------------------------------

modules_app = typer.Typer(help="Module lifecycle management.")
app.add_typer(modules_app, name="modules")


@modules_app.command("list")
def modules_list() -> None:
    """List all modules and their status."""
    from pathlib import Path

    modules_dir = Path("modules")
    if not modules_dir.exists():
        typer.echo("No modules found in modules/")
        return

    typer.echo(f"{'Module':<20} {'Status':<12} {'Version':<10} {'Views':<6} {'API':<6}")
    typer.echo("-" * 60)

    for mod_dir in sorted(modules_dir.iterdir()):
        if not mod_dir.is_dir() or mod_dir.name.startswith((".", "_")):
            continue
        if not (mod_dir / "module.py").exists():
            continue

        name = mod_dir.name
        version = ""
        has_views = "yes"
        has_api = "yes"
        is_system = False

        try:
            import importlib

            mod = importlib.import_module(f"modules.{name}.module")
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (
                    isinstance(attr, type)
                    and hasattr(attr, "name")
                    and getattr(attr, "name", None) == name
                ):
                    version = getattr(attr, "version", "")
                    has_views = "yes" if getattr(attr, "has_views", True) else "no"
                    has_api = "yes" if getattr(attr, "has_api", True) else "no"
                    is_system = getattr(attr, "is_system", False)
                    break
        except Exception:
            pass

        status = "available"
        if is_system:
            status += " (system)"
        typer.echo(f"{name:<20} {status:<12} {version:<10} {has_views:<6} {has_api:<6}")


@modules_app.command("install")
def modules_install(source: str) -> None:
    """Install a module from name, URL, or .zip path."""
    import asyncio

    async def _install():
        from hotframe.config.database import get_engine, get_session_factory
        from hotframe.engine.module_runtime import ModuleRuntime
        from hotframe.models.base import Base
        from hotframe.signals.dispatcher import AsyncEventBus
        from hotframe.signals.hooks import HookRegistry
        from hotframe.templating.slots import SlotRegistry

        settings = _load_project_settings()
        bus = AsyncEventBus()
        hooks = HookRegistry()
        slots = SlotRegistry()
        runtime = ModuleRuntime(
            app=None, settings=settings, event_bus=bus, hooks=hooks, slots=slots
        )

        # For CLI without DB, just do a simple filesystem install
        # Create tables directly
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = get_session_factory()
        async with factory() as session:
            result = await runtime.install(session, hub_id=None, module_id=source, source=source)
            if result.success:
                typer.echo(f"OK: Module '{result.module_id}' v{result.version} installed")
            else:
                typer.echo(f"Error: {result.error}", err=True)
                raise typer.Exit(1)
            await session.commit()

        await engine.dispose()

    asyncio.run(_install())


@modules_app.command("update")
def modules_update(source: str) -> None:
    """Update a module to a new version."""
    import asyncio

    async def _update():
        from hotframe.config.database import get_session_factory
        from hotframe.engine.module_runtime import ModuleRuntime
        from hotframe.signals.dispatcher import AsyncEventBus
        from hotframe.signals.hooks import HookRegistry
        from hotframe.templating.slots import SlotRegistry

        settings = _load_project_settings()
        runtime = ModuleRuntime(
            app=None,
            settings=settings,
            event_bus=AsyncEventBus(),
            hooks=HookRegistry(),
            slots=SlotRegistry(),
        )

        factory = get_session_factory()
        async with factory() as session:
            result = await runtime.update(
                session, hub_id=None, module_id=source, new_version=None, source=source
            )
            if result.success:
                typer.echo(f"OK: Module '{result.module_id}' updated to v{result.to_version}")
            else:
                typer.echo(f"Error: {result.error}", err=True)
                raise typer.Exit(1)
            await session.commit()

        from hotframe.config.database import dispose_engine

        await dispose_engine()

    asyncio.run(_update())


@modules_app.command("activate")
def modules_activate(name: str) -> None:
    """Activate a disabled module."""
    import asyncio

    async def _activate():
        from hotframe.config.database import get_session_factory
        from hotframe.engine.module_runtime import ModuleRuntime
        from hotframe.signals.dispatcher import AsyncEventBus
        from hotframe.signals.hooks import HookRegistry
        from hotframe.templating.slots import SlotRegistry

        settings = _load_project_settings()
        runtime = ModuleRuntime(
            app=None,
            settings=settings,
            event_bus=AsyncEventBus(),
            hooks=HookRegistry(),
            slots=SlotRegistry(),
        )

        factory = get_session_factory()
        async with factory() as session:
            result = await runtime.activate(session, hub_id=None, module_id=name)
            if result.success:
                typer.echo(f"OK: Module '{name}' activated")
            else:
                typer.echo(f"Error: {result.error}", err=True)
                raise typer.Exit(1)
            await session.commit()

        from hotframe.config.database import dispose_engine

        await dispose_engine()

    asyncio.run(_activate())


@modules_app.command("deactivate")
def modules_deactivate(name: str) -> None:
    """Deactivate an active module (keeps data)."""
    import asyncio

    async def _deactivate():
        from hotframe.config.database import get_session_factory
        from hotframe.engine.module_runtime import ModuleRuntime
        from hotframe.signals.dispatcher import AsyncEventBus
        from hotframe.signals.hooks import HookRegistry
        from hotframe.templating.slots import SlotRegistry

        settings = _load_project_settings()
        runtime = ModuleRuntime(
            app=None,
            settings=settings,
            event_bus=AsyncEventBus(),
            hooks=HookRegistry(),
            slots=SlotRegistry(),
        )

        factory = get_session_factory()
        async with factory() as session:
            result = await runtime.deactivate(session, hub_id=None, module_id=name)
            if result.success:
                typer.echo(f"OK: Module '{name}' deactivated")
            else:
                typer.echo(f"Error: {result.error}", err=True)
                raise typer.Exit(1)
            await session.commit()

        from hotframe.config.database import dispose_engine

        await dispose_engine()

    asyncio.run(_deactivate())


@modules_app.command("uninstall")
def modules_uninstall(
    name: str,
    keep_data: bool = typer.Option(False, "--keep-data", help="Keep database tables"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Uninstall a module (removes files, optionally drops tables)."""
    import asyncio

    if not yes:
        confirm = typer.confirm(
            f"Uninstall module '{name}'?"
            + (" (keeping data)" if keep_data else " (including database tables)")
        )
        if not confirm:
            typer.echo("Cancelled.")
            raise typer.Exit(0)

    async def _uninstall():
        from hotframe.config.database import get_session_factory
        from hotframe.engine.module_runtime import ModuleRuntime
        from hotframe.signals.dispatcher import AsyncEventBus
        from hotframe.signals.hooks import HookRegistry
        from hotframe.templating.slots import SlotRegistry

        settings = _load_project_settings()
        runtime = ModuleRuntime(
            app=None,
            settings=settings,
            event_bus=AsyncEventBus(),
            hooks=HookRegistry(),
            slots=SlotRegistry(),
        )

        factory = get_session_factory()
        async with factory() as session:
            result = await runtime.uninstall(session, hub_id=None, module_id=name)
            if result.success:
                typer.echo(f"OK: Module '{name}' uninstalled")
            else:
                typer.echo(f"Error: {result.error}", err=True)
                raise typer.Exit(1)
            await session.commit()

        from hotframe.config.database import dispose_engine

        await dispose_engine()

    asyncio.run(_uninstall())


# ---------------------------------------------------------------------------
# runserver
# ---------------------------------------------------------------------------


@app.command()
def runserver(
    host: str = "0.0.0.0",
    port: int = 8000,
    reload: bool = True,
) -> None:
    """Start the development server."""
    import sys

    import uvicorn

    # Ensure CWD is in sys.path so uvicorn can import main.py
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    typer.echo(f"Starting server at http://{host}:{port}")
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=reload,
    )


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


def _extract_module_dependencies(module_path: Path) -> list[str]:
    """Read ``DEPENDENCIES`` from a module's ``module.py`` without importing.

    Importing would execute the module (registers, side effects). We parse
    the file with AST and extract the ``DEPENDENCIES`` literal — supports
    both ``DEPENDENCIES = [...]`` and ``DEPENDENCIES: list[str] = [...]``.

    Returns an empty list if the file or attribute is absent or malformed —
    a missing manifest means "no declared deps", not a fatal error.
    """
    import ast

    manifest = module_path / "module.py"
    if not manifest.exists():
        return []
    try:
        tree = ast.parse(manifest.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []

    for node in tree.body:
        target_names: list[str] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
            value = node.value
        if "DEPENDENCIES" in target_names and isinstance(value, ast.List):
            return [
                elt.value
                for elt in value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
    return []


def _topo_sort_modules(
    module_targets: list[tuple[str, Path]],
) -> list[tuple[str, Path]]:
    """Order modules so each comes after the modules it depends on.

    Uses Kahn's algorithm. Modules with deps outside ``module_targets``
    (e.g. optional, removed, or referencing apps) ignore those edges —
    they cannot be migration prerequisites since they are not in scope.

    Cycles raise ``typer.Exit`` with a clear list of the modules involved
    so the operator can fix the manifests.
    """
    index = dict(module_targets)
    deps: dict[str, set[str]] = {
        mid: {d for d in _extract_module_dependencies(path) if d in index}
        for mid, path in module_targets
    }
    # Kahn: repeatedly pick modules whose unresolved-deps set is empty.
    ordered: list[tuple[str, Path]] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted(mid for mid, d in remaining.items() if not d)
        if not ready:
            cycle = ", ".join(sorted(remaining))
            typer.echo(
                f"Error: circular DEPENDENCIES detected among modules: {cycle}",
                err=True,
            )
            raise typer.Exit(1)
        for mid in ready:
            ordered.append((mid, index[mid]))
            remaining.pop(mid)
            for d in remaining.values():
                d.discard(mid)
    return ordered


@app.command()
def migrate(
    name: str = typer.Argument(
        None, help="App or module name (e.g. 'accounts'). Omit to migrate all."
    ),
) -> None:
    """Run migrations for all apps and modules (or a specific one).

    Scans apps/*/migrations/ and modules/*/migrations/ and runs
    Alembic upgrade head for each, using namespaced version tables.

    Examples::

        hf migrate              # all apps + modules
        hf migrate accounts     # only accounts app
        hf migrate sales        # only sales module
    """
    import asyncio

    async def _migrate():
        from hotframe.migrations.runner import ModuleMigrationRunner

        settings = _load_project_settings()
        runner = ModuleMigrationRunner()
        db_url = runner.get_sync_db_url(settings.DATABASE_URL)
        cwd = Path.cwd()

        targets: list[tuple[str, Path]] = []

        if name:
            # Specific app or module
            app_path = cwd / "apps" / name
            mod_path = cwd / "modules" / name
            if app_path.exists():
                targets.append((name, app_path))
            elif mod_path.exists():
                targets.append((name, mod_path))
            else:
                typer.echo(f"Error: '{name}' not found in apps/ or modules/", err=True)
                raise typer.Exit(1)
        else:
            # All apps
            apps_dir = cwd / "apps"
            if apps_dir.exists():
                for d in sorted(apps_dir.iterdir()):
                    if (
                        d.is_dir()
                        and not d.name.startswith((".", "_"))
                        and (d / "migrations").exists()
                    ):
                        targets.append((d.name, d))
            # All modules — collect first, then topologically sort by
            # DEPENDENCIES from each module.py manifest. Cross-module FKs
            # (e.g. commissions → services_service, kitchen_orders → tables)
            # require the referenced module's tables to exist before alembic
            # creates the FK constraint, so alphabetical order is unsafe.
            modules_dir = cwd / "modules"
            module_targets: list[tuple[str, Path]] = []
            if modules_dir.exists():
                for d in sorted(modules_dir.iterdir()):
                    if (
                        d.is_dir()
                        and not d.name.startswith((".", "_"))
                        and (d / "migrations").exists()
                    ):
                        module_targets.append((d.name, d))

            if module_targets:
                module_targets = _topo_sort_modules(module_targets)
            targets.extend(module_targets)

        if not targets:
            typer.echo("No migrations found in apps/ or modules/")
            return

        for mid, mpath in targets:
            if runner.has_migrations(mpath):
                typer.echo(f"  Migrating {mid}...")
                await runner.upgrade(mid, mpath, db_url)
            else:
                typer.echo(f"  {mid}: no migration scripts, skipping")

        typer.echo(f"Done — {len(targets)} migration(s) processed.")

    asyncio.run(_migrate())


# ---------------------------------------------------------------------------
# makemigrations
# ---------------------------------------------------------------------------


@app.command()
def makemigrations(
    name: str = typer.Argument(..., help="App or module name (e.g. 'accounts')"),
    message: str = typer.Option("auto", "-m", "--message", help="Migration message"),
) -> None:
    """Generate a new migration for an app or module.

    Creates an Alembic revision in apps/{name}/migrations/ or
    modules/{name}/migrations/ with autogenerate.

    Examples::

        hf makemigrations accounts -m "add email field"
        hf makemigrations sales -m "initial"
    """
    import asyncio

    async def _makemigrations():
        from alembic import command
        from alembic.config import Config

        settings = _load_project_settings()
        cwd = Path.cwd()

        # Find the app or module
        app_path = cwd / "apps" / name
        mod_path = cwd / "modules" / name
        if app_path.exists():
            target_path = app_path
        elif mod_path.exists():
            target_path = mod_path
        else:
            typer.echo(f"Error: '{name}' not found in apps/ or modules/", err=True)
            raise typer.Exit(1)

        migrations_dir = target_path / "migrations"
        versions_dir = migrations_dir / "versions"

        # Create migrations structure if it doesn't exist
        migrations_dir.mkdir(exist_ok=True)
        versions_dir.mkdir(exist_ok=True)

        # Create env.py and script.py.mako if missing
        env_py = migrations_dir / "env.py"
        if not env_py.exists():
            env_py.write_text(_generate_env_py(name))
            typer.echo(f"  Created {migrations_dir}/env.py")

        mako = migrations_dir / "script.py.mako"
        if not mako.exists():
            mako.write_text(_generate_script_mako())
            typer.echo(f"  Created {migrations_dir}/script.py.mako")

        # Build Alembic config programmatically
        db_url = settings.DATABASE_URL.replace("+asyncpg", "").replace("+aiosqlite", "")
        version_table = f"alembic_{name}"

        config = Config()
        config.set_main_option("script_location", str(migrations_dir))
        config.set_main_option("sqlalchemy.url", db_url)
        config.set_main_option("version_table", version_table)

        typer.echo(f"Generating migration for '{name}': {message}")

        await asyncio.to_thread(command.revision, config, message=message, autogenerate=True)
        typer.echo(f"Done — migration created in {migrations_dir}/versions/")

    asyncio.run(_makemigrations())


def _generate_env_py(name: str) -> str:
    """Generate a minimal env.py for Alembic migrations."""
    return f'''\
"""Alembic migration environment for {name}."""
import sys
from pathlib import Path

# Ensure project root is in sys.path so app/module imports work
# env.py lives at apps/{name}/migrations/env.py → parents[3] = project root
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from alembic import context
from sqlalchemy import create_engine, pool

# Import Base so Alembic sees the models
from hotframe.models.base import Base  # noqa: F401

# Import this app/module's models
try:
    import importlib
    # Try as app first, then as module
    try:
        importlib.import_module("apps.{name}.models")
    except ImportError:
        importlib.import_module("modules.{name}.models")
except ImportError:
    pass

target_metadata = Base.metadata
config = context.config


def run_migrations_offline():
    url = _to_sync_url(config.get_main_option("sqlalchemy.url"))
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _to_sync_url(url):
    """Convert async DB URL to sync for Alembic."""
    return url.replace("+asyncpg", "").replace("+aiosqlite", "")


def run_migrations_online():
    # Check if a connection was passed (from ModuleMigrationRunner)
    connectable = config.attributes.get("connection")
    if connectable is None:
        url = _to_sync_url(config.get_main_option("sqlalchemy.url"))
        connectable = create_engine(url, poolclass=pool.NullPool)

    if hasattr(connectable, "connect"):
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    else:
        context.configure(
            connection=connectable,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
'''


def _generate_script_mako() -> str:
    """Generate the Alembic script.py.mako template for migration files."""
    return '''\
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
'''


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------


@app.command()
def shell(
    no_startup: bool = typer.Option(
        False,
        "--no-startup",
        help="Skip running the application lifespan (no DB engine, no registries).",
    ),
    settings_path: str = typer.Option(
        "",
        "--settings",
        help="Dotted path to the settings module (e.g. 'my_project.settings'). "
        "Defaults to auto-detecting 'settings.py' in the current working directory.",
    ),
    plain: bool = typer.Option(
        False,
        "--plain",
        help="Force the built-in code.interact() REPL even if IPython is available.",
    ),
) -> None:
    """Start an interactive Python REPL with the hotframe application loaded.

    By default the full application lifespan is executed so the REPL has a
    live database engine, event bus, hook/slot registries, broadcast hub
    and module runtime. Pre-loaded variables include ``app``, ``settings``,
    ``db``, ``events``, ``hooks``, ``slots``, ``runtime`` and ``SlotEntry``.

    Uses IPython when available, falling back to ``code.interact()``.
    """
    import asyncio
    import sys

    from hotframe import __version__

    # Ensure CWD is importable so a local settings.py can be found.
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    settings_obj = _resolve_shell_settings(settings_path)

    from hotframe.bootstrap import create_app

    fastapi_app = create_app(settings_obj)

    namespace: dict[str, object] = {
        "app": fastapi_app,
        "settings": settings_obj,
    }

    # Use a dedicated event loop so the REPL can reuse it to await coroutines.
    loop = asyncio.new_event_loop()
    lifespan_cm = None
    db_session = None

    try:
        if not no_startup:
            from hotframe.config.database import get_session_factory
            from hotframe.templating.slots import SlotEntry

            lifespan_cm = fastapi_app.router.lifespan_context(fastapi_app)
            loop.run_until_complete(lifespan_cm.__aenter__())

            state = fastapi_app.state
            factory = get_session_factory()
            db_session = factory()

            namespace.update(
                {
                    "db": db_session,
                    "events": getattr(state, "event_bus", None),
                    "hooks": getattr(state, "hooks", None),
                    "slots": getattr(state, "slots", None),
                    "runtime": getattr(state, "module_runtime", None),
                    "SlotEntry": SlotEntry,
                }
            )

        _launch_repl(
            namespace=namespace,
            version=__version__,
            plain=plain,
            loop=loop,
        )
    finally:
        try:
            if db_session is not None:
                loop.run_until_complete(db_session.close())
            if lifespan_cm is not None:
                loop.run_until_complete(lifespan_cm.__aexit__(None, None, None))
        finally:
            loop.close()


def _resolve_shell_settings(dotted_path: str):
    """Load the settings instance from a dotted path or auto-detect ``settings.py``.

    Args:
        dotted_path: Optional dotted import path to the settings module.
                     When empty, the function looks for ``settings`` in the
                     current working directory.

    Returns:
        The resolved ``HotframeSettings`` subclass instance.

    Raises:
        typer.Exit: If no settings module can be located or the target does
                    not expose a ``settings`` attribute.
    """
    import importlib

    from hotframe.config.settings import set_settings

    if dotted_path:
        module_name = dotted_path
    else:
        module_name = "settings"

    try:
        mod = importlib.import_module(module_name)
    except ImportError as exc:
        typer.echo(
            f"Error: could not import settings module '{module_name}': {exc}",
            err=True,
        )
        raise typer.Exit(1) from exc

    settings_obj = getattr(mod, "settings", None)
    if settings_obj is None:
        typer.echo(
            f"Error: module '{module_name}' does not define a 'settings' attribute.",
            err=True,
        )
        raise typer.Exit(1)

    set_settings(settings_obj)
    return settings_obj


def _launch_repl(
    *,
    namespace: dict[str, object],
    version: str,
    plain: bool,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Launch an IPython or ``code.interact()`` REPL with the given namespace.

    When IPython is available and ``plain`` is False, the embedded IPython
    shell is used with ``%autoawait asyncio`` enabled so coroutines can be
    awaited directly. Otherwise a ``code.interact()`` session is started and
    a ``run(coro)`` helper is injected that dispatches coroutines to the
    supplied event loop.
    """
    use_ipython = False
    if not plain:
        try:
            import IPython  # type: ignore[import-not-found]  # noqa: F401

            use_ipython = True
        except ImportError:
            use_ipython = False

    repl_name = "IPython" if use_ipython else "plain"
    banner = _build_shell_banner(
        version=version,
        repl_name=repl_name,
        namespace=namespace,
    )

    if use_ipython:
        from IPython.terminal.embed import InteractiveShellEmbed  # type: ignore[import-not-found]

        shell_instance = InteractiveShellEmbed(banner1=banner, user_ns=namespace)
        try:
            shell_instance.run_line_magic("autoawait", "asyncio")
        except Exception:
            # Older IPython versions may not support autoawait; the REPL still works.
            pass
        shell_instance()
        return

    import code

    def run(coro):
        """Run a coroutine to completion on the shell's event loop."""
        return loop.run_until_complete(coro)

    namespace["run"] = run
    code.interact(banner=banner, local=namespace, exitmsg="")


def _build_shell_banner(*, version: str, repl_name: str, namespace: dict[str, object]) -> str:
    """Build the startup banner shown when the shell opens."""
    preferred = ["app", "settings", "db", "events", "hooks", "slots", "runtime", "SlotEntry"]
    available = [name for name in preferred if name in namespace]

    if repl_name == "IPython":
        tip = "Tip: await works directly (autoawait asyncio)."
    else:
        tip = "Tip: use run(coro) to await a coroutine (e.g. run(db.execute(...)))."

    lines = [
        f"Hotframe {version} shell ({repl_name})",
        "Variables: " + ", ".join(available),
        tip,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Show hotframe version."""
    from hotframe import __version__

    typer.echo(f"hotframe {__version__}")


if __name__ == "__main__":
    app()
