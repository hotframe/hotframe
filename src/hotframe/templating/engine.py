"""
Jinja2 template engine with module template discovery and i18n support.

Creates a Jinja2 environment that:
- Loads templates from the global ``templates/`` directory.
- Auto-discovers ``templates/`` directories inside each module.
- Installs gettext translations so ``_()`` and ``{% trans %}`` work.
- Supports hot-refresh of template directories when modules are loaded/unloaded.

Usage::

    from hotframe.templating.engine import create_template_engine, refresh_template_dirs
    from hotframe.config.settings import get_settings

    settings = get_settings()
    templates = create_template_engine(modules_dir=settings.MODULES_DIR)
    app.state.templates = templates

    # After loading/unloading a module:
    refresh_template_dirs(templates, settings.MODULES_DIR)
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

# Root templates directory: resolved from the project's working directory,
# not from the hotframe package itself. Like Django, templates/ lives in
# the project root, not inside the framework.
_GLOBAL_TEMPLATE_DIR = Path.cwd() / "templates"


def _collect_template_dirs(modules_dir: Path | None) -> list[str]:
    """Build the ordered list of template directories.

    Order: global templates first (CWD/templates/), then app template
    dirs (apps/*/templates/), then module template dirs, then component
    roots (framework built-ins and per-module components).
    """
    dirs: list[str] = []

    # 1. Project-level templates (CWD/templates/)
    if _GLOBAL_TEMPLATE_DIR.exists():
        dirs.append(str(_GLOBAL_TEMPLATE_DIR))

    # 2. App template dirs (apps/*/templates/) — scan apps/ if it exists
    apps_dir = Path.cwd() / "apps"
    if apps_dir.exists():
        for app_dir in sorted(apps_dir.iterdir()):
            if not app_dir.is_dir() or app_dir.name.startswith((".", "_")):
                continue
            tpl_dir = app_dir / "templates"
            if tpl_dir.exists():
                dirs.append(str(tpl_dir))

    # Modules directory — contains both bundled (is_system=True) and dynamic
    # modules downloaded from S3.
    if modules_dir and modules_dir.exists():
        for mod_dir in sorted(modules_dir.iterdir()):
            if not mod_dir.is_dir() or mod_dir.name.startswith((".", "_")):
                continue
            tpl_dir = mod_dir / "templates"
            if tpl_dir.exists():
                dirs.append(str(tpl_dir))

    # 3. Component search roots.
    #    Framework built-ins live at ``hotframe/components/_builtin/``
    #    and are addressed as ``_builtin/<name>/template.html``; adding
    #    the ``components/`` directory itself makes that reference resolve.
    #    App-shipped components are addressed as
    #    ``<app_name>/components/<name>/template.html``; adding the
    #    ``apps/`` directory makes that reference resolve.
    #    Module-shipped components are addressed as
    #    ``<module_id>/components/<name>/template.html``; adding
    #    ``modules_dir`` makes that reference resolve.
    from hotframe import components as _components_pkg

    framework_components_root = Path(_components_pkg.__file__).parent
    dirs.append(str(framework_components_root))

    if apps_dir.exists():
        dirs.append(str(apps_dir))

    if modules_dir and modules_dir.exists():
        dirs.append(str(modules_dir))

    return dirs


def create_template_engine(modules_dir: Path | None = None) -> Jinja2Templates:
    """Create the Jinja2 engine with module template discovery and i18n.

    Args:
        modules_dir: Path to the modules directory (e.g. ``/tmp/modules``).
            Each module's ``templates/`` subdirectory is added to the search path.

    Returns:
        A configured ``Jinja2Templates`` instance with extensions, globals,
        and gettext translations installed.
    """
    template_dirs = _collect_template_dirs(modules_dir)

    from hotframe.components.jinja_ext import (
        ComponentExtension,
        install_component_context_tracker,
    )
    from hotframe.live.jinja_ext import LiveExtension

    env = Environment(
        loader=FileSystemLoader(template_dirs),
        autoescape=select_autoescape(["html", "xml"]),
        extensions=[
            "jinja2.ext.i18n",
            "jinja2.ext.do",
            "jinja2.ext.loopcontrols",
            ComponentExtension,
            LiveExtension,
        ],
    )

    # Patch env.context_class so the currently-rendering Context is
    # exposed to the ``{% component %}`` tag's CallBlock helper.
    install_component_context_tracker(env)

    # Register global functions, filters, and constants.
    from hotframe.components.rendering import register_component_globals
    from hotframe.templating.extensions import register_extensions

    register_extensions(env)
    register_component_globals(env)

    # Live runtime asset helpers — templates emit the live.js script tag
    # via ``{{ live_assets() }}`` in their <head>. The runtime serves
    # ``/static/hotframe/live.js`` and ``/static/hotframe/morphdom.min.js``
    # automatically (see hotframe.bootstrap).
    from hotframe.live.assets import live_assets

    env.globals["live_assets"] = live_assets

    # Install gettext translations so {% trans %} and _() work in templates.
    # The translations adapter uses the context-local language (set per-request
    # by LanguageMiddleware), so templates are always rendered in the correct
    # language for each request.
    from hotframe.middleware.i18n_support import get_translations

    # ``install_gettext_translations`` is provided by the ``jinja2.ext.i18n``
    # extension we load via ``extensions=[...]`` above. Mypy can't see it on
    # the bare ``Environment`` class.
    env.install_gettext_translations(get_translations())  # type: ignore[attr-defined]

    templates = _HotframeTemplates(env=env)

    logger.info("Template engine created with %d search directories", len(template_dirs))
    return templates


class _HotframeTemplates(Jinja2Templates):
    """Jinja2Templates subclass that auto-injects request-scoped context.

    Every TemplateResponse automatically gets:
    - csrf_token: the CSRF token from request.state
    - csrf_input(): a callable that returns the hidden input HTML
    - csp_nonce: the CSP nonce from request.state
    """

    def TemplateResponse(self, request, name, context=None, **kwargs):
        if context is None:
            context = {}
        if "request" not in context:
            context["request"] = request

        # Auto-inject CSRF token
        if "csrf_token" not in context:
            from markupsafe import Markup

            csrf_token = getattr(request.state, "csrf_token", "")
            context["csrf_token"] = csrf_token
            if "csrf_input" not in context:
                context["csrf_input"] = lambda: (
                    Markup(f'<input type="hidden" name="csrf_token" value="{csrf_token}">')
                    if csrf_token
                    else lambda: Markup("")
                )

        # Auto-inject CSP nonce
        if "csp_nonce" not in context:
            context["csp_nonce"] = getattr(request.state, "csp_nonce", "")

        return super().TemplateResponse(request, name, context, **kwargs)


def refresh_template_dirs(templates: Jinja2Templates, modules_dir: Path) -> None:
    """Re-scan module directories and update the template loader.

    Called after module load/unload so new or removed templates take effect
    without restarting the application.
    """
    template_dirs = _collect_template_dirs(modules_dir)
    templates.env.loader = FileSystemLoader(template_dirs)
    logger.info("Template directories refreshed: %d search paths", len(template_dirs))
