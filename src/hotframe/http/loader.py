# SPDX-License-Identifier: Apache-2.0
"""
Discover :class:`Interceptor` implementations from filesystem paths.

Given one or more directories, :func:`discover_interceptors` imports
every ``.py`` file found inside and collects module-level attributes
that satisfy the :class:`~hotframe.http.interceptors.Interceptor`
protocol (``name``, ``applies_to``, ``order``, async ``intercept``).

The loader is deliberately permissive:

- A file that fails to import is logged at ``WARNING`` and skipped —
  one broken app interceptor file must not prevent the rest from
  loading.
- Duplicate ``name`` values are deduplicated, keeping the first one
  discovered and logging the collision.
- Results are sorted by ``order`` ascending to match
  :func:`hotframe.http.interceptors.build_chain`.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
from logging import Logger
from pathlib import Path

from hotframe.http.interceptors import Interceptor

logger = logging.getLogger(__name__)


def _looks_like_interceptor(obj: object) -> bool:
    """Return ``True`` when ``obj`` satisfies the :class:`Interceptor` protocol.

    We check instances (not classes) because interceptors are usually
    pre-instantiated module-level singletons so the app can configure
    them with real values (``RetryInterceptor(on_status=[503])``).
    """
    if inspect.isclass(obj) or inspect.ismodule(obj):
        return False
    if not all(hasattr(obj, attr) for attr in ("name", "applies_to", "order", "intercept")):
        return False
    intercept = getattr(obj, "intercept", None)
    if not callable(intercept):
        return False
    # ``inspect.iscoroutinefunction`` unwraps bound methods correctly.
    if not inspect.iscoroutinefunction(intercept):
        return False
    name = getattr(obj, "name", None)
    if not isinstance(name, str) or not name:
        return False
    return True


def _iter_python_files(path: Path, recursive: bool) -> list[Path]:
    """Return ``.py`` files under ``path`` — recursively if requested.

    Hidden files (``__init__.py`` aside) and dunder files are ignored.
    """
    if not path.is_dir():
        return []
    iterator = path.rglob("*.py") if recursive else path.glob("*.py")
    out: list[Path] = []
    for p in iterator:
        if p.name.startswith("_"):
            continue
        if not p.is_file():
            continue
        out.append(p)
    return out


def _import_file(path: Path) -> object | None:
    """Import a standalone ``.py`` file and return the module object."""
    # Synthesize a unique module name so two files with the same base
    # name in different directories don't clobber each other in
    # ``sys.modules``.
    module_name = f"_hotframe_interceptor_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_interceptors(
    search_paths: list[Path],
    logger: Logger | None = None,
    recursive: bool = False,
) -> list[Interceptor]:
    """Scan ``search_paths`` for :class:`Interceptor` instances.

    Args:
        search_paths: Directories to scan. Missing paths are skipped
            with a ``DEBUG`` log entry so projects that don't ship
            interceptors still start cleanly.
        logger: Logger override. Defaults to this module's logger.
        recursive: When ``True`` descend into subdirectories; defaults
            to ``False`` to keep discovery predictable and cheap.

    Returns:
        Deduplicated, order-sorted list of interceptor instances.
    """
    log = logger or globals()["logger"]
    discovered: list[Interceptor] = []
    seen_names: set[str] = set()

    for path in search_paths:
        p = Path(path)
        if not p.exists():
            log.debug("Interceptor path %s does not exist — skipping", p)
            continue
        files = _iter_python_files(p, recursive=recursive)
        for file in files:
            try:
                module = _import_file(file)
            except Exception as exc:
                log.warning("Failed to import interceptor file %s: %s", file, exc)
                continue
            if module is None:
                continue
            for attr_name in dir(module):
                if attr_name.startswith("_"):
                    continue
                attr = getattr(module, attr_name)
                if not _looks_like_interceptor(attr):
                    continue
                name = attr.name
                if name in seen_names:
                    log.info(
                        "Interceptor %r already registered — skipping duplicate from %s",
                        name,
                        file,
                    )
                    continue
                seen_names.add(name)
                discovered.append(attr)
                log.debug(
                    "Discovered interceptor %r (order=%s) from %s",
                    name,
                    getattr(attr, "order", None),
                    file,
                )

    discovered.sort(key=lambda i: getattr(i, "order", 100))
    if discovered:
        log.info(
            "Discovered %d HTTP interceptor(s): %s",
            len(discovered),
            ", ".join(i.name for i in discovered),
        )
    return discovered


__all__ = ["discover_interceptors"]
