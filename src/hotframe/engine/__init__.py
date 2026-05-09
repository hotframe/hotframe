"""
engine subpackage — dynamic module orchestration.

Public API:
  - ``ModuleRuntime``: full-lifecycle orchestrator (install/activate/
    deactivate/uninstall/update/hot_reload).
  - ``ModuleLoader``: importlib + route/service/event/hook mount + unmount.
  - ``ImportManager``: sys.modules tracking with weakref zombie detection.
    Already wired into ``ModuleLoader`` so imports and purges are exact
    and zombie-checked.
  - ``HotMountPipeline``: state-machine primitive with LIFO rollback.
    Reusable for building custom install flows; ``ModuleRuntime.install``
    uses its own linear flow today.

See ``hotframe/ARCHITECTURE.md`` for the design.
"""

from hotframe.engine.import_manager import (
    ImportedBundle,
    ImportManager,
    PurgeReport,
)
from hotframe.engine.loader import ModuleLoader
from hotframe.engine.module_runtime import (
    ActivateResult,
    DeactivateResult,
    InstallResult,
    ModuleRuntime,
    UninstallResult,
    UpdateResult,
)
from hotframe.engine.pipeline import (
    HotMountPipeline,
    PhaseResult,
    PhaseStatus,
    PipelineState,
    RollbackHandle,
)

__all__ = [
    "ActivateResult",
    "DeactivateResult",
    "HotMountPipeline",
    # Primitives
    "ImportManager",
    "ImportedBundle",
    # Result dataclasses
    "InstallResult",
    "ModuleLoader",
    # Orchestration
    "ModuleRuntime",
    "PhaseResult",
    "PhaseStatus",
    "PipelineState",
    "PurgeReport",
    "RollbackHandle",
    "UninstallResult",
    "UpdateResult",
]
