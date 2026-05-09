"""
templating — Jinja2 template engine wired with hotframe's globals and tags.

``create_template_engine`` builds a ``Jinja2Templates`` instance with all
app and module template directories pre-registered and returns it ready
to use with FastAPI. ``register_extensions`` installs the framework's
Jinja2 globals and filters: ``url_for``, ``static_url``, ``render_icon``,
``slugify``, ``currency``, ``dateformat``, ``timesince``, slot rendering,
and the live-runtime asset helper.

Custom Jinja2 tags wired by the engine: ``{% component %}`` (stateless
reusable widgets) and ``{% live %}`` (stateful, WebSocket-driven
components).

Key exports::

    from hotframe.templating.engine import create_template_engine, refresh_template_dirs
    from hotframe.templating.extensions import register_extensions

Usage::

    templates = create_template_engine(modules_dir=Path("/app/modules"))
    return templates.TemplateResponse(request, "sales/index.html", context)
"""
