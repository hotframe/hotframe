# SPDX-License-Identifier: Apache-2.0
"""Helpers for per-module Alembic env.py files.

The module-scoped ``env.py`` pattern (used by hub and any hotframe project
with per-module version tables) imports only its own module's models. When
other modules declare cross-module FKs (e.g. a module table → ``local_user``
owned by an ``apps/auth`` app), autogenerate fails with
``NoReferencedTableError`` because the referenced Table is not present in
``Base.metadata`` at the moment SQLAlchemy resolves the FK.

``import_all_app_models()`` walks ``apps/*/models.py`` from the project root
and imports them so their tables are registered on ``Base.metadata`` before
autogenerate runs. Modules themselves are not imported here — each env.py
imports its own module's models explicitly, and other modules' tables are
filtered out via ``include_object``.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def import_all_app_models(project_root: Path | None = None) -> list[str]:
    """Import every ``apps/<name>/models.py`` in the project.

    Ensures that tables owned by apps (typically ``local_user``, shared
    mixins, media storage) are registered on ``Base.metadata`` so FKs
    declared from modules can be resolved during autogenerate.

    Args:
        project_root: Directory containing ``apps/``. Defaults to the
            current working directory — the same convention ``hf`` uses.

    Returns:
        List of dotted import paths that were successfully imported.
    """
    root = project_root or Path.cwd()
    apps_dir = root / "apps"
    if not apps_dir.is_dir():
        return []

    # Make ``apps.<name>`` importable without requiring the project to be
    # installed. Idempotent: insert only if not already present.
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    imported: list[str] = []
    for app_dir in sorted(apps_dir.iterdir()):
        if not app_dir.is_dir() or app_dir.name.startswith((".", "_")):
            continue
        models_file = app_dir / "models.py"
        if not models_file.exists():
            continue
        dotted = f"apps.{app_dir.name}.models"
        try:
            importlib.import_module(dotted)
            imported.append(dotted)
        except Exception:
            # Best-effort: a broken app models.py should not crash the
            # migration of an unrelated module. Log and move on — the FK
            # resolution will fail loudly if this import was required.
            logger.warning("Could not import %s for autogenerate", dotted, exc_info=True)
    return imported


def import_module_dependencies(
    module_id: str,
    project_root: Path | None = None,
) -> list[str]:
    """Import the ``models`` of every module in this module's DEPENDENCIES tree.

    Cross-module FKs (e.g. ``sales_sale.customer_id`` → ``customers_customer``)
    require the dependency's tables on ``Base.metadata`` to resolve during
    autogenerate. We walk the DEPENDENCIES graph transitively — a module's
    deps' deps must also be loaded — and import each as ``<module_id>.models``.

    Modules NOT in the dependency tree are intentionally skipped: importing
    them would trigger their side effects (decorators, registers) for no
    benefit, and they are filtered from the diff by ``include_object`` anyway.

    Args:
        module_id: The module whose dependencies should be loaded.
        project_root: Directory containing ``modules/``. Defaults to cwd.

    Returns:
        List of dotted import paths that were successfully imported.
    """
    root = project_root or Path.cwd()
    modules_dir = root / "modules"
    if not modules_dir.is_dir():
        return []

    # Make ``<module_id>`` importable as a top-level package. Each module
    # is its own root in the modules/ folder.
    modules_str = str(modules_dir)
    if modules_str not in sys.path:
        sys.path.insert(0, modules_str)

    visited: set[str] = set()
    imported: list[str] = []

    def _walk(mid: str) -> None:
        if mid in visited or mid == module_id:
            visited.add(mid)
            return
        visited.add(mid)
        manifest = modules_dir / mid / "module.py"
        if not manifest.exists():
            return
        for dep in _read_dependencies(manifest):
            _walk(dep)
        models_file = modules_dir / mid / "models.py"
        if not models_file.exists():
            return
        dotted = f"{mid}.models"
        try:
            importlib.import_module(dotted)
            imported.append(dotted)
        except Exception:
            logger.warning("Could not import %s for autogenerate", dotted, exc_info=True)

    # Seed the walk with this module's direct deps (the module itself is
    # imported by its own env.py — we only load what it depends on).
    manifest = modules_dir / module_id / "module.py"
    if manifest.exists():
        for dep in _read_dependencies(manifest):
            _walk(dep)

    return imported


def _read_dependencies(manifest: Path) -> list[str]:
    """Parse ``DEPENDENCIES`` from a module manifest without importing it."""
    import ast

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
