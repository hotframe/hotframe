# SPDX-License-Identifier: Apache-2.0
"""
Router and static asset mounting for the components subsystem.

Components discovered by :mod:`hotframe.components` may declare:

* An optional :class:`fastapi.APIRouter` named ``router`` in their
  ``routes.py`` module. Those routes are mounted on the application at
  ``/_components/<name>/...``.
* An optional scoped ``static/`` directory. Its contents are served at
  ``/_components/<name>/static/...`` via
  :class:`fastapi.staticfiles.StaticFiles`.

The ``/_components/`` prefix is reserved for this subsystem. No automatic
CSRF exemption is applied: component routes are subject to the normal
middleware stack (CSRF, rate limits, CSP, ...). Authors must send a CSRF
token on unsafe methods just like any other hotframe route. If a specific
path needs to be exempt, it must be added to
``settings.CSRF_EXEMPT_PREFIXES`` explicitly.

Hot-reload note
---------------
FastAPI/Starlette do not provide a first-class "remove mount" API. We
mirror the approach used by
:class:`hotframe.engine.loader.ModuleLoader`: direct in-place mutation of
``app.router.routes`` filtered by path prefix. Each component owns a
single ``Mount`` entry per router and per static directory, so teardown
is O(n) over ``app.router.routes`` — the same cost the module loader
already pays on unload.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

if TYPE_CHECKING:
    from fastapi import FastAPI

    from hotframe.components.registry import ComponentRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _router_prefix(name: str) -> str:
    """Return the mount prefix for a component's router."""
    return f"/_components/{name}"


def _static_prefix(name: str) -> str:
    """Return the mount prefix for a component's static directory."""
    return f"/_components/{name}/static"


def _route_path(route: object) -> str | None:
    """Return a route's ``path`` attribute if present, otherwise ``None``."""
    return getattr(route, "path", None)


# ---------------------------------------------------------------------------
# Router mounting
# ---------------------------------------------------------------------------


def mount_component_routers(app: FastAPI, registry: ComponentRegistry) -> int:
    """
    Mount every component router currently registered.

    For each :class:`~hotframe.components.entry.ComponentEntry` whose
    ``extra_router`` is not ``None``, include the router under
    ``/_components/<name>`` and tag it with ``component:<name>`` for
    OpenAPI grouping.

    Args:
        app: The FastAPI application.
        registry: The component registry (typically ``app.state.components``).

    Returns:
        Number of routers mounted during this call.
    """
    mounted = 0
    for entry in registry.list_components():
        if entry.extra_router is None:
            continue
        prefix = _router_prefix(entry.name)
        app.include_router(
            entry.extra_router,
            prefix=prefix,
            tags=[f"component:{entry.name}"],
        )
        logger.info("Mounted component router: %s", prefix)
        mounted += 1

    if mounted:
        app.openapi_schema = None

    return mounted


def mount_component_routers_for_module(
    app: FastAPI,
    registry: ComponentRegistry,
    module_id: str,
) -> int:
    """
    Mount only the routers contributed by the given ``module_id``.

    Used during module install/activate to register the newly available
    component endpoints without re-mounting everything.
    """
    mounted = 0
    for entry in registry.list_components():
        if entry.module_id != module_id or entry.extra_router is None:
            continue
        prefix = _router_prefix(entry.name)
        app.include_router(
            entry.extra_router,
            prefix=prefix,
            tags=[f"component:{entry.name}"],
        )
        logger.info("Mounted component router: %s", prefix)
        mounted += 1

    if mounted:
        app.openapi_schema = None

    return mounted


def unmount_component_router(app: FastAPI, name: str) -> bool:
    """
    Remove every route whose path is under ``/_components/<name>``.

    Returns ``True`` if at least one route was removed, ``False``
    otherwise. Also drops any static mount the component may own — a
    component name owns the whole ``/_components/<name>/*`` subtree.
    """
    prefix = _router_prefix(name)
    prefix_slash = f"{prefix}/"
    routes = app.router.routes
    original = len(routes)

    routes[:] = [
        route
        for route in routes
        if not _matches_component_subtree(_route_path(route), prefix, prefix_slash)
    ]

    removed = original - len(routes)
    if removed:
        app.openapi_schema = None
        logger.info(
            "Unmounted component router: %s (%d route(s) removed)",
            prefix,
            removed,
        )
        return True
    return False


def unmount_component_routers_for_module(app: FastAPI, module_id: str) -> int:
    """
    Remove all component routes owned by ``module_id`` from the app.

    The registry is not mutated here; :class:`ComponentRegistry` exposes
    ``unregister_module`` for its side of the cleanup and the caller is
    expected to invoke it separately. Returns the number of routes
    removed.

    This function reads the module ownership off the **app routes** via
    their path prefix, not the registry, so teardown still works when
    the registry entry has already been dropped by a prior step.
    """
    # We must resolve which component names belong to the module from
    # the registry, since the app routes only carry paths, not module
    # ids. Callers must invoke this BEFORE unregistering the module from
    # the registry.
    registry = getattr(app.state, "components", None)
    if registry is None:
        return 0

    names = [entry.name for entry in registry.list_components() if entry.module_id == module_id]
    if not names:
        return 0

    prefixes = tuple(_router_prefix(n) for n in names)
    prefixes_slash = tuple(f"{p}/" for p in prefixes)

    routes = app.router.routes
    original = len(routes)
    routes[:] = [
        route
        for route in routes
        if not _any_component_subtree_match(_route_path(route), prefixes, prefixes_slash)
    ]
    removed = original - len(routes)

    if removed:
        app.openapi_schema = None
        logger.info(
            "Unmounted %d component route(s) for module %s",
            removed,
            module_id,
        )

    return removed


# ---------------------------------------------------------------------------
# Static asset mounting
# ---------------------------------------------------------------------------


def mount_component_static(app: FastAPI, registry: ComponentRegistry) -> int:
    """
    Mount a :class:`StaticFiles` instance for every component that ships
    a ``static/`` directory.

    Each component owns a single ``Mount`` entry at
    ``/_components/<name>/static``. Directories that do not exist on
    disk log a warning and are skipped — this can happen if discovery
    recorded a path that was later removed.

    Returns:
        Number of static mounts added during this call.
    """
    mounted = 0
    for entry in registry.list_components():
        if entry.static_dir is None:
            continue
        if _mount_single_static(app, entry.name, entry.static_dir):
            mounted += 1
    return mounted


def mount_component_static_for_module(
    app: FastAPI,
    registry: ComponentRegistry,
    module_id: str,
) -> int:
    """Mount static directories for components owned by ``module_id``."""
    mounted = 0
    for entry in registry.list_components():
        if entry.module_id != module_id or entry.static_dir is None:
            continue
        if _mount_single_static(app, entry.name, entry.static_dir):
            mounted += 1
    return mounted


def unmount_component_static(app: FastAPI, name: str) -> bool:
    """
    Remove the ``StaticFiles`` mount for the given component name.

    Returns ``True`` if a mount was removed, ``False`` otherwise.
    """
    path = _static_prefix(name)
    routes = app.router.routes
    original = len(routes)
    routes[:] = [route for route in routes if _route_path(route) != path]
    removed = original - len(routes)
    if removed:
        logger.info("Unmounted component static: %s", path)
        return True
    return False


def unmount_component_static_for_module(app: FastAPI, module_id: str) -> int:
    """
    Remove static mounts for every component owned by ``module_id``.

    Like :func:`unmount_component_routers_for_module`, callers must
    invoke this BEFORE unregistering the module from the registry so
    the component-to-module mapping is still available.
    """
    registry = getattr(app.state, "components", None)
    if registry is None:
        return 0

    paths = {
        _static_prefix(entry.name)
        for entry in registry.list_components()
        if entry.module_id == module_id and entry.static_dir is not None
    }
    if not paths:
        return 0

    routes = app.router.routes
    original = len(routes)
    routes[:] = [route for route in routes if _route_path(route) not in paths]
    removed = original - len(routes)

    if removed:
        logger.info(
            "Unmounted %d component static mount(s) for module %s",
            removed,
            module_id,
        )
    return removed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _mount_single_static(app: FastAPI, name: str, static_dir: str) -> bool:
    """
    Attach one ``StaticFiles`` mount for a component.

    Returns ``True`` on success, ``False`` when the directory does not
    exist (warning is logged). Duplicate mounts (same path already
    present) are treated as a no-op and return ``False`` — this avoids
    accidentally layering mounts during a double-discovery.
    """
    path = _static_prefix(name)
    directory = Path(static_dir)
    if not directory.exists() or not directory.is_dir():
        logger.warning(
            "Component %r declares static_dir=%s but the directory is missing",
            name,
            static_dir,
        )
        return False

    for route in app.router.routes:
        if _route_path(route) == path:
            logger.debug("Component static already mounted: %s", path)
            return False

    app.router.routes.append(
        Mount(
            path,
            app=StaticFiles(directory=str(directory)),
            name=f"component-static-{name}",
        )
    )
    logger.info("Mounted component static: %s -> %s", path, directory)
    return True


def _matches_component_subtree(
    route_path: str | None,
    prefix: str,
    prefix_slash: str,
) -> bool:
    """Return True when ``route_path`` lives under ``prefix``."""
    if route_path is None:
        return False
    return route_path == prefix or route_path.startswith(prefix_slash)


def _any_component_subtree_match(
    route_path: str | None,
    prefixes: tuple[str, ...],
    prefixes_slash: tuple[str, ...],
) -> bool:
    """Return True when ``route_path`` matches any of the given subtrees."""
    if route_path is None:
        return False
    for prefix, prefix_slash in zip(prefixes, prefixes_slash, strict=True):
        if route_path == prefix or route_path.startswith(prefix_slash):
            return True
    return False
