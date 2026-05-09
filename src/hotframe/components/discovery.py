# SPDX-License-Identifier: Apache-2.0
"""
Component discovery — scan a filesystem tree and register components.

Each subdirectory of the scan root is one component. Its directory name
becomes the component name. Supported files inside a component directory:

- ``template.html`` (required) — Jinja2 template rendered by
  ``render_component(name)``.
- ``component.py`` (optional) — Declares a class that inherits from
  :class:`hotframe.components.Component`. Used for prop validation and
  template context derivation.
- ``routes.py`` (optional) — Declares a module-level
  ``router: fastapi.APIRouter`` that the components mounting subsystem
  attaches at ``/components/{name}/`` (mounting is handled by the
  mounting subsystem, not here).
- ``static/`` (optional) — Per-component static assets. Path is stored
  on the entry for the mounting subsystem to serve.

Discovery is synchronous and side-effect-free aside from importing
Python modules via ``importlib`` and calling ``registry.register``.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from hotframe.components.base import Component
from hotframe.components.entry import ComponentEntry

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import APIRouter

    from hotframe.components.registry import ComponentRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers for importing component.py and routes.py outside the regular
# package tree. Module-shipped components live at
# ``modules/{module_id}/components/{name}/component.py`` which is not
# always on ``sys.path`` as a dotted package; we load via spec_from_file_location.
# ---------------------------------------------------------------------------


def _load_module_from_file(py_path: Path, module_name: str):
    """Import a Python file as a module. Returns the imported module object."""
    spec = importlib.util.spec_from_file_location(module_name, py_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not build import spec for {py_path}")
    module = importlib.util.module_from_spec(spec)
    # Cache it so relative imports and repeated discovery find the same object.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _find_component_class(module) -> type | None:
    """
    Locate the first class in ``module`` that is a valid component base.

    Accepts subclasses of :class:`Component` (stateless, template-only)
    or :class:`hotframe.live.LiveComponent` (stateful, live runtime).
    The two hierarchies are independent — ``LiveComponent`` does NOT
    inherit from ``Component`` because they validate via different
    Pydantic models — so we have to check both.

    Excludes the base classes themselves to avoid accidentally
    "discovering" the imported base.
    """
    # Local import to avoid a circular dependency with hotframe.live
    # (which imports from hotframe.components in turn).
    from hotframe.live.base import LiveComponent

    bases = (Component, LiveComponent)

    for _attr_name, attr in inspect.getmembers(module, inspect.isclass):
        if attr in bases:
            continue
        if issubclass(attr, bases) and attr.__module__ == module.__name__:
            return attr
    # Fallback: accept a subclass even if it was re-exported from another module.
    for _attr_name, attr in inspect.getmembers(module, inspect.isclass):
        if attr in bases:
            continue
        if issubclass(attr, bases):
            return attr
    return None


def _build_render_fn(
    props_cls: type | None,
) -> Callable[..., dict] | None:
    """
    Build the per-component ``render_fn`` used at render time.

    The function validates props by instantiating ``props_cls``, then
    returns the merged context dict: validated prop values plus anything
    returned by :meth:`Component.context`.

    Template-only components (no ``props_cls``) do not need a
    ``render_fn`` — the raw kwargs pass through unchanged.

    Live components (subclasses of :class:`hotframe.live.LiveComponent`)
    are rendered through the live runtime (``hotframe.live.diff``) and
    should not use ``render_fn`` at all — the live render path bypasses
    this entirely. We return ``None`` for them so a stray
    ``render_component('todo_list', ...)`` call falls into the default
    template-only path with raw kwargs (and logs a misuse warning at
    the call site).
    """
    if props_cls is None:
        return None

    # Live components do not render via render_fn — they have their own path.
    from hotframe.live.base import LiveComponent

    if issubclass(props_cls, LiveComponent):
        return None

    if not issubclass(props_cls, Component):
        # Unknown class — skip render_fn rather than crash.
        return None

    def render_fn(**props) -> dict:
        instance = props_cls(**props)
        context = instance.model_dump()
        extra = instance.context()
        if extra:
            context.update(extra)
        return context

    return render_fn


def _load_router(py_path: Path, module_name: str) -> APIRouter | None:
    """Import ``routes.py`` and return its ``router`` attribute or ``None``."""
    try:
        mod = _load_module_from_file(py_path, module_name)
    except Exception:
        logger.exception("Failed to import component routes file: %s", py_path)
        return None
    router = getattr(mod, "router", None)
    if router is None:
        logger.warning("Component routes file %s has no module-level `router` attribute", py_path)
        return None
    return router


def _load_component_class(py_path: Path, module_name: str) -> type | None:
    """Import ``component.py`` and return the first component class found.

    Returns the first subclass of :class:`Component` or
    :class:`hotframe.live.LiveComponent` declared in the file.
    """
    try:
        mod = _load_module_from_file(py_path, module_name)
    except Exception:
        logger.exception("Failed to import component file: %s", py_path)
        return None
    cls = _find_component_class(mod)
    if cls is None:
        logger.warning("Component file %s has no Component or LiveComponent subclass", py_path)
    return cls


# ---------------------------------------------------------------------------
# Public discovery API
# ---------------------------------------------------------------------------


def _template_path_for(
    component_dir: Path,
    template_search_prefix: str | None,
) -> str:
    """
    Return the template path as expected by the Jinja2 loader.

    The discovery subsystem adds the scan root to the Jinja2 loader's
    search path (see :func:`discover_module_components` and
    :func:`discover_app_components`). Given that, the template
    reference becomes ``{prefix}/{component_name}/template.html``.
    """
    name = component_dir.name
    if template_search_prefix:
        return f"{template_search_prefix}/{name}/template.html"
    return f"{name}/template.html"


def discover_components(
    root: Path,
    *,
    module_id: str | None = None,
    template_search_prefix: str | None = None,
    import_prefix: str = "_hotframe_components",
) -> list[ComponentEntry]:
    """
    Scan ``root`` for component directories and build
    :class:`ComponentEntry` objects.

    Args:
        root: Filesystem directory containing one subdirectory per
            component.
        module_id: Owning module ID written onto every entry.
        template_search_prefix: If set, prepended to the template path
            recorded on each entry (used when the Jinja2 loader sees the
            parent of ``root`` rather than ``root`` itself, e.g. the
            loader is pointed at ``modules/<id>/components`` and the
            template is referenced as ``<id>/components/<name>/template.html``).
        import_prefix: Namespace used for the ad-hoc ``sys.modules``
            entries created when importing ``component.py`` and
            ``routes.py`` from outside the normal package tree. Keep
            distinct per call to avoid collisions.

    Returns:
        A list of :class:`ComponentEntry` objects, one per valid
        component directory.
    """
    if not root.exists() or not root.is_dir():
        return []

    entries: list[ComponentEntry] = []

    for component_dir in sorted(root.iterdir()):
        if not component_dir.is_dir():
            continue
        if component_dir.name.startswith((".", "_")):
            # Skip dotfiles and internal markers (e.g. __pycache__).
            continue

        template_file = component_dir / "template.html"
        if not template_file.exists():
            logger.warning(
                "Skipping component %r: missing template.html (dir=%s)",
                component_dir.name,
                component_dir,
            )
            continue

        name = component_dir.name

        # component.py (optional). May be a stateless ``Component``
        # subclass or a stateful ``LiveComponent`` subclass; the entry
        # records both via ``props_cls`` and ``is_live``.
        props_cls: type | None = None
        component_py = component_dir / "component.py"
        if component_py.exists():
            props_cls = _load_component_class(
                component_py,
                f"{import_prefix}.{name}.component",
            )

        # Detect live components — local import to avoid a circular
        # dep between hotframe.components and hotframe.live.
        is_live = False
        if props_cls is not None:
            from hotframe.live.base import LiveComponent

            is_live = issubclass(props_cls, LiveComponent)

        # routes.py (optional)
        extra_router = None
        has_endpoint = False
        routes_py = component_dir / "routes.py"
        if routes_py.exists():
            extra_router = _load_router(
                routes_py,
                f"{import_prefix}.{name}.routes",
            )
            if extra_router is not None:
                has_endpoint = True

        # static/ (optional)
        static_dir_path = component_dir / "static"
        static_dir = str(static_dir_path) if static_dir_path.is_dir() else None

        entry = ComponentEntry(
            name=name,
            template=_template_path_for(component_dir, template_search_prefix),
            has_endpoint=has_endpoint,
            render_fn=_build_render_fn(props_cls),
            extra_router=extra_router,
            module_id=module_id,
            static_dir=static_dir,
            props_cls=props_cls,
            is_live=is_live,
        )
        entries.append(entry)

    return entries


# ---------------------------------------------------------------------------
# Scoped discovery helpers
# ---------------------------------------------------------------------------


def discover_module_components(
    registry: ComponentRegistry,
    module_dir: Path,
    module_id: str,
) -> int:
    """
    Scan ``<module_dir>/components/`` and register every component
    under the owning ``module_id``.

    Templates are referenced as ``<module_id>/components/<name>/template.html``
    which assumes the module root directory is on the Jinja2 loader
    search path (the hotframe template engine already adds each module
    root).

    Returns the number of components registered.
    """
    components_dir = module_dir / "components"
    if not components_dir.is_dir():
        return 0

    entries = discover_components(
        components_dir,
        module_id=module_id,
        template_search_prefix=f"{module_id}/components",
        import_prefix=f"_hotframe_components.{module_id}",
    )
    for entry in entries:
        registry.register(entry, module_id=module_id)
    if entries:
        logger.info(
            "Registered %d component(s) for module %r: %s",
            len(entries),
            module_id,
            ", ".join(e.name for e in entries),
        )
    return len(entries)


def discover_app_components(
    registry: ComponentRegistry,
    apps_dir: Path,
    app_name: str,
) -> int:
    """
    Scan ``<apps_dir>/<app_name>/components/`` and register every
    component found.

    This mirrors :func:`discover_module_components` but targets the
    statically-shipped ``apps/`` directory. Apps are part of the project
    itself — they cannot be hot-unmounted — so the registered entries
    carry ``module_id=None`` and are never removed by
    :meth:`ComponentRegistry.unregister_module`.

    Templates are referenced as ``<app_name>/components/<name>/template.html``
    which assumes the ``apps/`` directory is on the Jinja2 loader search
    path (the hotframe template engine adds it at engine creation time).

    Returns the number of components registered.
    """
    components_dir = apps_dir / app_name / "components"
    if not components_dir.is_dir():
        return 0

    entries = discover_components(
        components_dir,
        module_id=None,
        template_search_prefix=f"{app_name}/components",
        import_prefix=f"_hotframe_app_components.{app_name}",
    )
    for entry in entries:
        registry.register(entry)
    if entries:
        logger.info(
            "Registered %d component(s) for app %r: %s",
            len(entries),
            app_name,
            ", ".join(e.name for e in entries),
        )
    return len(entries)


def discover_apps_components(
    registry: ComponentRegistry,
    apps_dir: Path,
) -> int:
    """
    Scan every app under ``apps_dir`` for a ``components/`` directory
    and register its contents.

    Convenience wrapper around :func:`discover_app_components` that
    iterates all apps in alphabetical order. Apps whose names start
    with ``.`` or ``_`` are skipped, matching the app auto-discovery
    convention in :mod:`hotframe.bootstrap`.

    Returns the total number of components registered across all apps.
    """
    if not apps_dir.is_dir():
        return 0

    total = 0
    for app_dir in sorted(apps_dir.iterdir()):
        if not app_dir.is_dir() or app_dir.name.startswith((".", "_")):
            continue
        total += discover_app_components(registry, apps_dir, app_dir.name)
    return total
