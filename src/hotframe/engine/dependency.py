# SPDX-License-Identifier: Apache-2.0
"""
Dependency manager — topological sort, version checks, cascade protection.

Uses the module state model (from settings or built-in) for all queries.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from hotframe.apps.config import ModuleManifest
from hotframe.engine.state import _get_module_model

if TYPE_CHECKING:
    from uuid import UUID

    from hotframe.db.protocols import ISession
    from hotframe.engine.module_runtime import ModuleRuntime

logger = logging.getLogger(__name__)


@dataclass
class DependencyCheckResult:
    ok: bool = True
    missing: list[str] = field(default_factory=list)
    inactive: list[str] = field(default_factory=list)
    version_mismatch: list[tuple[str, str, str]] = field(default_factory=list)
    auto_installable: list[str] = field(default_factory=list)


@dataclass
class DeactivateCheckResult:
    can_deactivate: bool = True
    dependents: list[str] = field(default_factory=list)
    cascade_order: list[str] = field(default_factory=list)


@dataclass
class UninstallCheckResult:
    can_uninstall: bool = True
    dependents: list[tuple[str, str]] = field(default_factory=list)


_DEP_PATTERN = re.compile(
    r"^(?P<module_id>[a-z][a-z0-9_]*)"
    r"(?:(?P<op>>=|<=|==|!=|>|<)(?P<version>\d+\.\d+\.\d+))?$"
)


def _parse_dep(dep_spec: str) -> tuple[str, str | None, str | None]:
    dep_spec = dep_spec.strip()
    m = _DEP_PATTERN.match(dep_spec)
    if m is None:
        return dep_spec, None, None
    return m.group("module_id"), m.group("op"), m.group("version")


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def _version_satisfies(actual: str, op: str, required: str) -> bool:
    a = _version_tuple(actual)
    r = _version_tuple(required)
    if op == ">=":
        return a >= r
    if op == "<=":
        return a <= r
    if op == "==":
        return a == r
    if op == "!=":
        return a != r
    if op == ">":
        return a > r
    if op == "<":
        return a < r
    return True


class DependencyManager:
    """Manages inter-module dependencies."""

    async def check_install_deps(
        self,
        session: ISession,
        manifest: ModuleManifest,
        **filters,
    ) -> DependencyCheckResult:
        """Check that all declared dependencies are installed, active, and version-compatible."""
        Model = _get_module_model()
        result = DependencyCheckResult()

        for dep_spec in manifest.DEPENDENCIES:
            dep_id, op, ver_req = _parse_dep(dep_spec)

            stmt = select(Model).where(Model.module_id == dep_id)
            for key, value in filters.items():
                stmt = stmt.where(getattr(Model, key) == value)
            row = (await session.execute(stmt)).scalar_one_or_none()

            if row is None:
                result.missing.append(dep_id)
            elif row.status != "active":
                result.inactive.append(dep_id)
            elif op and ver_req and not _version_satisfies(row.version, op, ver_req):
                result.version_mismatch.append((dep_id, f"{op}{ver_req}", row.version))

        result.ok = not (result.missing or result.inactive or result.version_mismatch)
        return result

    async def check_can_deactivate(
        self,
        session: ISession,
        module_id: str,
        **filters,
    ) -> DeactivateCheckResult:
        """Return whether a module can be safely deactivated, listing any active dependents."""
        dependent_ids = await self._find_active_dependents(session, module_id, **filters)

        if not dependent_ids:
            return DeactivateCheckResult(can_deactivate=True)

        cascade = await self._build_cascade_order(session, module_id, **filters)

        return DeactivateCheckResult(
            can_deactivate=False,
            dependents=dependent_ids,
            cascade_order=cascade,
        )

    async def check_can_uninstall(
        self,
        session: ISession,
        module_id: str,
        **filters,
    ) -> UninstallCheckResult:
        """Return whether a module can be uninstalled, listing any installed dependents."""
        Model = _get_module_model()
        stmt = select(Model.module_id, Model.status).where(
            Model.module_id != module_id,
            Model.status.in_(["active", "installed", "disabled"]),
            Model.manifest["dependencies"].as_string().contains(module_id),
        )
        for key, value in filters.items():
            stmt = stmt.where(getattr(Model, key) == value)
        rows = (await session.execute(stmt)).all()

        dependents: list[tuple[str, str]] = []
        for dep_module_id, dep_status in rows:
            dep_manifest = (
                await session.execute(
                    select(Model.manifest).where(Model.module_id == dep_module_id)
                )
            ).scalar_one_or_none()
            if dep_manifest and self._depends_on(dep_manifest, module_id):
                dependents.append((dep_module_id, dep_status))

        return UninstallCheckResult(
            can_uninstall=len(dependents) == 0,
            dependents=dependents,
        )

    def resolve_load_order(self, modules: list[dict]) -> list[dict]:
        """Topologically sort modules so each dependency loads before its dependents.

        Args:
            modules: List of dicts with keys ``module_id`` and ``manifest``.

        Returns:
            Ordered list of module dicts safe to load sequentially; modules with
            missing or cyclic dependencies are excluded with a warning.
        """
        graph: dict[str, dict] = {m["module_id"]: m for m in modules}
        available = set(graph.keys())

        deps: dict[str, list[str]] = {}
        for mid, m in graph.items():
            manifest = m["manifest"]
            raw_deps = (
                manifest.DEPENDENCIES
                if isinstance(manifest, ModuleManifest)
                else manifest.get("dependencies", [])
            )
            deps[mid] = [_parse_dep(d)[0] for d in raw_deps]

        changed = True
        while changed:
            changed = False
            for mid in list(available):
                for dep_id in deps.get(mid, []):
                    if dep_id not in available:
                        available.discard(mid)
                        logger.warning("Module %s excluded: missing dependency %s", mid, dep_id)
                        changed = True
                        break

        in_degree: dict[str, int] = dict.fromkeys(available, 0)
        for mid in available:
            for dep_id in deps.get(mid, []):
                if dep_id in available:
                    in_degree[mid] += 1

        queue: deque[str] = deque(mid for mid, deg in in_degree.items() if deg == 0)
        ordered: list[dict] = []

        while queue:
            mid = queue.popleft()
            ordered.append(graph[mid])
            for other in available:
                if mid in deps.get(other, []):
                    in_degree[other] -= 1
                    if in_degree[other] == 0:
                        queue.append(other)

        if len(ordered) != len(available):
            loaded_ids = {m["module_id"] for m in ordered}
            cyclic = available - loaded_ids
            logger.error("Dependency cycle detected: %s — will NOT load", cyclic)

        return ordered

    async def deactivate_cascade(
        self,
        session: ISession,
        module_id: str,
        runtime: ModuleRuntime,
        **filters,
    ) -> None:
        """Deactivate all active modules that depend on module_id in reverse dependency order."""
        cascade = await self._build_cascade_order(session, module_id, **filters)
        # ModuleRuntime.deactivate expects (session, hub_id, module_id) — pull
        # the hub_id out of the dynamic filter bag (it is the only filter the
        # runtime cares about) and forward it positionally.
        hub_id: UUID = filters["hub_id"]
        for mid in cascade:
            if mid == module_id:
                continue
            logger.info("Cascade deactivating %s (depends on %s)", mid, module_id)
            await runtime.deactivate(session, hub_id, mid, cascade=False)

    async def _build_cascade_order(
        self,
        session: ISession,
        module_id: str,
        **filters,
    ) -> list[str]:
        order: list[str] = []
        visited: set[str] = set()
        queue: deque[str] = deque([module_id])

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            dependents = await self._find_active_dependents(session, current, **filters)
            for dep_id in dependents:
                if dep_id not in visited:
                    queue.append(dep_id)
            order.append(current)

        order.reverse()
        return order

    async def _find_active_dependents(
        self,
        session: ISession,
        module_id: str,
        **filters,
    ) -> list[str]:
        Model = _get_module_model()
        stmt = select(Model.module_id, Model.manifest).where(
            Model.status == "active",
            Model.module_id != module_id,
            Model.manifest["dependencies"].as_string().contains(module_id),
        )
        for key, value in filters.items():
            stmt = stmt.where(getattr(Model, key) == value)
        rows = (await session.execute(stmt)).all()

        result: list[str] = []
        for dep_module_id, dep_manifest in rows:
            if self._depends_on(dep_manifest, module_id):
                result.append(dep_module_id)
        return result

    @staticmethod
    def _depends_on(manifest_dict: dict, target_module_id: str) -> bool:
        deps = manifest_dict.get("DEPENDENCIES", [])
        for dep_spec in deps:
            dep_id, _, _ = _parse_dep(dep_spec)
            if dep_id == target_module_id:
                return True
        return False
