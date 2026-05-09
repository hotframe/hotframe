"""
App/module discovery scanner.

Given a root directory (e.g. ``apps/`` or ``modules/``), discovers each
subdirectory as a candidate app/module, detects which conventional files
are present, and imports them in a deterministic order. The return value
is a list of ``DiscoveryResult`` — one per subdirectory found.

This module does **not** mount routes, register signals, or touch the
AppRegistry. It only collects what's there and makes it available for
the orchestrator (ModuleRuntime or a future AppBootLoader) to wire up.

Layering: lives in ``hotframe.discovery``, which is a mid-level layer.
Can import from signals, orm, db, utils, but not from engine/apps.

Specifically: this module MUST NOT import ``hotframe.apps`` statically,
because ``hotframe.apps`` lives in a higher layer. Any function that
needs ``AppConfig`` / ``ModuleConfig`` must resolve those symbols via
``importlib.import_module`` inside the function body.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from hotframe.discovery.conventions import (
    APP_CONVENTIONS,
    Convention,
    Kind,
)

logger = logging.getLogger(__name__)


class DiscoveryError(Exception):
    """Raised when a directory violates discovery conventions."""


@dataclass(slots=True)
class FileArtifact:
    """A single detected file/directory within an app."""

    convention: Convention
    path: Path
    imported_module: ModuleType | None = None  # set only if it was importable


@dataclass(slots=True)
class DiscoveryResult:
    """Per-directory output of the scanner."""

    name: str  # e.g. "accounts"
    root_path: Path  # e.g. /path/to/apps/accounts
    package_name: str  # e.g. "apps.accounts" or "modules.invoice"
    entry_point: FileArtifact | None = None  # app.py xor module.py
    artifacts: list[FileArtifact] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_entry_point(self) -> bool:
        return self.entry_point is not None

    def find(self, kind: Kind) -> FileArtifact | None:
        """Return the first artifact matching this kind (or None)."""
        if kind == Kind.ENTRY_POINT:
            return self.entry_point
        for a in self.artifacts:
            if a.convention.kind == kind:
                return a
        return None


# Directory names that must always be skipped during scanning.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".git",
    }
)


def scan(
    root: Path,
    *,
    package_prefix: str,
    import_side_effects: bool = True,
) -> list[DiscoveryResult]:
    """
    Scan ``root`` for subdirectories that look like apps/modules.

    Args:
        root: directory to scan (e.g. ``Path("apps")`` or ``Path("modules")``).
        package_prefix: dotted prefix for importing the directory as a Python
            package. If ``root`` is ``apps/`` and you want imports like
            ``apps.accounts``, pass ``"apps"``. If it's absolute under
            hotframe, pass the fully-qualified prefix.
        import_side_effects: if True (default), actually ``importlib.import_module``
            each detected file. If False, only collects paths (useful for testing).

    Returns:
        A list of DiscoveryResult, one per subdirectory in root.

    Raises:
        DiscoveryError: if a subdirectory has both ``app.py`` and ``module.py``,
            or has a conventional file that fails a required_exports check.
    """
    if not root.is_dir():
        raise DiscoveryError(f"Root path is not a directory: {root}")

    results: list[DiscoveryResult] = []

    for subdir in sorted(root.iterdir(), key=lambda p: p.name):
        if not subdir.is_dir():
            continue
        if subdir.name in _SKIP_DIRS or subdir.name.startswith("."):
            continue

        result = _scan_subdir(
            subdir,
            package_prefix=package_prefix,
            import_side_effects=import_side_effects,
        )
        results.append(result)

    return results


def _scan_subdir(
    subdir: Path,
    *,
    package_prefix: str,
    import_side_effects: bool,
) -> DiscoveryResult:
    """Scan one candidate subdirectory and return its DiscoveryResult."""
    name = subdir.name
    package_name = f"{package_prefix}.{name}" if package_prefix else name
    result = DiscoveryResult(
        name=name,
        root_path=subdir,
        package_name=package_name,
    )

    # Detect entry point conflict first (app.py XOR module.py).
    app_py = subdir / "app.py"
    module_py = subdir / "module.py"
    if app_py.exists() and module_py.exists():
        raise DiscoveryError(
            f"Subdirectory {subdir} contains BOTH app.py and module.py. "
            "Exactly one must be present."
        )

    for conv in APP_CONVENTIONS:
        candidate = subdir / conv.filename_or_dir

        if conv.is_directory:
            if not candidate.is_dir():
                continue
            artifact = FileArtifact(convention=conv, path=candidate)
            result.artifacts.append(artifact)
            continue

        # File convention
        if not candidate.is_file():
            continue

        artifact = FileArtifact(convention=conv, path=candidate)

        # Attempt import if requested.
        if import_side_effects:
            stem = candidate.stem  # e.g. "app", "models", "routes"
            module_dotted = f"{package_name}.{stem}"
            try:
                artifact.imported_module = importlib.import_module(module_dotted)
            except ImportError as exc:
                result.errors.append(f"Failed to import {module_dotted}: {exc}")
            except Exception as exc:  # pragma: no cover - defensive
                result.errors.append(f"Unexpected error importing {module_dotted}: {exc}")

            # Contract: required_exports uses at-least-one-of semantics.
            # The imported module must expose at least one of the listed
            # names. This lets a convention accept multiple equivalent
            # shapes (e.g. routes.py with urlpatterns OR router).
            if artifact.imported_module is not None and conv.required_exports:
                present = [
                    sym for sym in conv.required_exports if hasattr(artifact.imported_module, sym)
                ]
                if not present:
                    raise DiscoveryError(
                        f"{module_dotted} must export at least one of: "
                        f"{', '.join(conv.required_exports)}"
                    )

        # Classify: entry_point (app.py / module.py) is stored separately.
        if conv.kind is Kind.ENTRY_POINT:
            if result.entry_point is not None:
                # Should already have been caught by the XOR check above,
                # but keep as a safety net.
                raise DiscoveryError(f"Subdirectory {subdir} has multiple entry-point files.")
            result.entry_point = artifact
        else:
            result.artifacts.append(artifact)

    return result


def find_entry_config(result: DiscoveryResult) -> Any:
    """
    Introspect ``result.entry_point.imported_module`` and return the
    unique AppConfig/ModuleConfig subclass declared in it.

    Raises DiscoveryError if zero or >1 subclasses are found.
    """
    if result.entry_point is None:
        raise DiscoveryError(
            f"DiscoveryResult {result.name!r} has no entry point (app.py/module.py)."
        )
    if result.entry_point.imported_module is None:
        raise DiscoveryError(
            f"Entry point for {result.name!r} was not imported "
            "(import_side_effects=False or import failed)."
        )

    # Deferred import: scanner is mid-layer, hotframe.apps is upper layer.
    # Using importlib here keeps the static dependency graph clean.
    apps_config = importlib.import_module("hotframe.apps.config")
    AppConfig = apps_config.AppConfig

    module = result.entry_point.imported_module

    candidates: list[type] = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        # Only consider classes actually defined in this module.
        if obj.__module__ != module.__name__:
            continue
        if not issubclass(obj, AppConfig):
            continue
        if obj is AppConfig:
            continue
        # ModuleConfig is a subclass of AppConfig — allow it, but skip the
        # base class itself if it happens to be imported.
        try:
            ModuleConfig = apps_config.ModuleConfig
            if obj is ModuleConfig:
                continue
        except AttributeError:
            pass
        candidates.append(obj)

    if len(candidates) == 0:
        raise DiscoveryError(
            f"No AppConfig/ModuleConfig subclass found in {result.entry_point.path}"
        )
    if len(candidates) > 1:
        names = ", ".join(c.__name__ for c in candidates)
        raise DiscoveryError(
            f"Multiple AppConfig/ModuleConfig subclasses found in "
            f"{result.entry_point.path}: {names}. Exactly one is expected."
        )

    return candidates[0]
