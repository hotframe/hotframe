"""
Import manager with explicit tracking and weakref-based zombie detection.

Encapsulates the lifecycle of importing a module package into the running
Python process and purging it cleanly:

1. Snapshots ``sys.modules`` before/after to record every submodule that
   appeared as a side effect of the top-level import.
2. Allows callers to register exported classes (models, services, routers,
   etc.) so that, after a purge, weak references can verify that nothing
   keeps the module alive (Pydantic caches, SQLAlchemy mappers, signal
   receivers, FastAPI dependency injection, etc.).
3. Performs an atomic purge of every tracked submodule entry in
   ``sys.modules`` and triggers ``gc.collect()`` so weakrefs become
   resolvable.

This primitive is intentionally **standalone**: it does not know about
``ModuleLoader`` or any higher-level orchestration. Integration is the
responsibility of the caller (typically the engine pipeline or
``ModuleRuntime``) and may happen in a later phase of the migration.

Layering: lives in ``hotframe/engine/`` and intentionally depends on
nothing beyond stdlib so it stays at the engine layer without violating
the layered architecture enforced by ``import-linter``.

Thread-safety: a ``threading.Lock`` guards the internal bundle registry.
The actual ``importlib.import_module`` and ``sys.modules`` mutation are
serialized as well, but callers should still avoid concurrently importing
or purging the *same* module from multiple threads — that is a logical
race even with the lock (one thread could observe a half-built bundle if
it queried ``get_bundle`` between phases).

Caveats with ``weakref``:
    * Most ordinary classes can be weakly referenced.
    * Classes using ``__slots__`` without a ``__weakref__`` slot raise
      ``TypeError`` when passed to ``weakref.ref``. ``register_exported_class``
      catches and logs that case rather than failing the import.
    * Exotic metaclasses or classes captured by C extensions (Pydantic v1
      ``ModelMetaclass`` cache, SQLAlchemy mapper registry) may keep
      references the GC cannot break. The zombie report in that case is
      *informational* — the caller may still need a graceful restart.
"""

from __future__ import annotations

import gc
import importlib
import logging
import sys
import threading
import weakref
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ImportedBundle:
    """
    Result of importing a module package.

    Tracks every ``sys.modules`` entry that appeared as a side effect of
    the top-level import so the manager can purge them atomically later.

    Attributes:
        module_id: Caller-defined identifier (typically the marketplace
            module id, e.g. ``"invoice"``). May or may not match
            ``package_name``.
        package_name: The actual Python package that was imported (e.g.
            ``"invoice"`` for ``modules/invoice/__init__.py``).
        base_path: Filesystem location of the extracted package.
        imported_submodules: Fully-qualified names that appeared in
            ``sys.modules`` between the snapshot and the import. Includes
            the package itself.
        exported_classes: Weak references to classes the caller wants
            verified post-purge. A non-dead reference after purge means a
            zombie.
    """

    module_id: str
    package_name: str
    base_path: Path
    imported_submodules: list[str] = field(default_factory=list)
    exported_classes: list[weakref.ref] = field(default_factory=list)


@dataclass(slots=True)
class PurgeReport:
    """
    Outcome of a :meth:`ImportManager.purge` call.

    Attributes:
        module_id: The module that was purged.
        purged_count: Number of ``sys.modules`` entries removed.
        zombie_classes: Qualified names of classes whose weakref is still
            alive after ``gc.collect()``. Non-empty means the module did
            not unload cleanly.
    """

    module_id: str
    purged_count: int
    zombie_classes: list[str]


class ImportManager:
    """
    Manage import and purge of Python packages with explicit tracking.

    The goal is that, when a module is unmounted, every entry in
    ``sys.modules`` that the module created is removed and no class
    objects exported by the module remain referenced anywhere.

    Designed as a **best-effort** primitive: the weakref check is the
    strongest signal we can produce from pure Python. False positives are
    possible (Pydantic, SQLAlchemy, signal registries) and callers should
    interpret a non-empty :attr:`PurgeReport.zombie_classes` as
    "investigate" rather than "fatal".
    """

    def __init__(self) -> None:
        self._bundles: dict[str, ImportedBundle] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Import
    # ------------------------------------------------------------------ #

    def import_package(
        self,
        module_id: str,
        package_name: str,
        base_path: Path,
    ) -> ImportedBundle:
        """
        Import the package and record every submodule that appears in
        ``sys.modules`` as a result.

        Args:
            module_id: Caller identifier for tracking. Subsequent calls to
                :meth:`purge`, :meth:`register_exported_class` and
                :meth:`get_bundle` use this id.
            package_name: The Python-level package name to import (e.g.
                ``"invoice"``).
            base_path: Filesystem location of the package. The parent
                directory of ``base_path`` is added to ``sys.path`` if
                not already present so ``importlib.import_module`` can
                resolve ``package_name``.

        Returns:
            The :class:`ImportedBundle` recorded for this ``module_id``.

        Raises:
            ValueError: If ``module_id`` is already registered. Callers
                must purge before re-importing.
            ImportError / Exception: Whatever ``importlib.import_module``
                raises is propagated unchanged. ``sys.modules`` is left
                in whatever state the import produced — caller should
                ``purge`` to clean up.
        """
        with self._lock:
            if module_id in self._bundles:
                raise ValueError(f"module_id={module_id!r} already imported; purge first")

            parent_dir = str(base_path.parent.resolve())
            inserted_on_path = False
            if parent_dir not in sys.path:
                sys.path.insert(0, parent_dir)
                inserted_on_path = True

            before = set(sys.modules.keys())

            try:
                importlib.import_module(package_name)
            except Exception:
                # Best-effort cleanup: do not leave half-imported state if
                # the very first import attempt blew up.
                after = set(sys.modules.keys())
                for added in after - before:
                    sys.modules.pop(added, None)
                if inserted_on_path:
                    try:
                        sys.path.remove(parent_dir)
                    except ValueError:
                        pass
                raise

            after = set(sys.modules.keys())
            new_modules = sorted(after - before)

            bundle = ImportedBundle(
                module_id=module_id,
                package_name=package_name,
                base_path=base_path,
                imported_submodules=new_modules,
            )
            self._bundles[module_id] = bundle
            logger.debug(
                "imported package=%s module_id=%s submodules=%d",
                package_name,
                module_id,
                len(new_modules),
            )
            return bundle

    # ------------------------------------------------------------------ #
    # Class tracking
    # ------------------------------------------------------------------ #

    def register_exported_class(self, module_id: str, cls: type) -> None:
        """
        Register a class exported by the module for post-purge weakref
        verification.

        Silently skips classes that cannot be weakly referenced (typical
        cause: ``__slots__`` without ``__weakref__``). A debug log is
        emitted so callers can audit which exports are not verifiable.

        Args:
            module_id: Identifier passed to :meth:`import_package`.
            cls: The class to track.

        Raises:
            KeyError: If ``module_id`` has not been imported.
        """
        with self._lock:
            bundle = self._bundles.get(module_id)
            if bundle is None:
                raise KeyError(f"module_id={module_id!r} not registered; call import_package first")

            try:
                ref = weakref.ref(cls)
            except TypeError:
                logger.debug(
                    "cannot weakref class=%s.%s for module_id=%s "
                    "(probably __slots__ without __weakref__)",
                    cls.__module__,
                    cls.__qualname__,
                    module_id,
                )
                return

            bundle.exported_classes.append(ref)

    # ------------------------------------------------------------------ #
    # Purge
    # ------------------------------------------------------------------ #

    def purge(self, module_id: str) -> PurgeReport:
        """
        Purge the package: pop every tracked entry from ``sys.modules``,
        run ``gc.collect()``, and report any zombie classes.

        Idempotent: purging an unknown ``module_id`` returns an empty
        report instead of raising. Calling twice in a row likewise yields
        the second call as a no-op.

        Args:
            module_id: Identifier passed to :meth:`import_package`.

        Returns:
            A :class:`PurgeReport` listing the purge count and any
            classes whose weakref survived ``gc.collect()``.
        """
        with self._lock:
            bundle = self._bundles.pop(module_id, None)
            if bundle is None:
                return PurgeReport(
                    module_id=module_id,
                    purged_count=0,
                    zombie_classes=[],
                )

            purged = 0
            for name in bundle.imported_submodules:
                if sys.modules.pop(name, None) is not None:
                    purged += 1

            # Force a collection so weakrefs become None for anything not
            # otherwise referenced.
            gc.collect()

            zombies: list[str] = []
            for ref in bundle.exported_classes:
                obj = ref()
                if obj is not None:
                    qualified = f"{obj.__module__}.{obj.__qualname__}"
                    zombies.append(qualified)

            if zombies:
                logger.warning(
                    "zombie classes after purge module_id=%s count=%d names=%s",
                    module_id,
                    len(zombies),
                    zombies,
                )

            return PurgeReport(
                module_id=module_id,
                purged_count=purged,
                zombie_classes=zombies,
            )

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    def get_bundle(self, module_id: str) -> ImportedBundle | None:
        """Return the recorded bundle for ``module_id`` or ``None``."""
        with self._lock:
            return self._bundles.get(module_id)
