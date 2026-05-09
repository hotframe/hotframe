# SPDX-License-Identifier: Apache-2.0
"""
Module state DB — CRUD operations on the module state table.

The model used is resolved from ``settings.MODULE_STATE_MODEL`` or
falls back to the built-in ``Module`` model.
"""

from __future__ import annotations

import importlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import delete, select, update

if TYPE_CHECKING:
    from hotframe.db.protocols import ISession

logger = logging.getLogger(__name__)


# Canonical set of values stored in ``Module.status``. The column itself is
# free-text (``String(20)``) — projects subclassing the model are not forced
# to a DB-level enum — but new code must use one of these literals so the
# boundary middleware, marketplace UI, and ``get_active_modules`` filter all
# stay in sync.
#
# - ``installing`` — install pipeline in progress
# - ``active``     — fully operational, eligible for boot mounting
# - ``disabled``   — explicitly turned off by the user/admin
# - ``error``      — install/activate/uninstall raised; not operational
# - ``degraded``   — still mounted but failing repeatedly; UI invites the
#                    user to disable. Distinct from ``error`` (which means
#                    "won't load") and from ``disabled`` (user choice).
ModuleStatus = Literal["installing", "active", "disabled", "error", "degraded"]


if TYPE_CHECKING:
    # Type alias for the configurable module-state ORM class. Projects swap
    # in their own model via ``settings.MODULE_STATE_MODEL``; the chosen
    # class is a SQLAlchemy declarative whose columns are accessed both as
    # values (instance.module_id) and as descriptors (Model.module_id in a
    # ``select`` / ``update``). No single static type can express both, so
    # we collapse to ``Any`` at the public boundary. The runtime contract —
    # that the model must expose the columns of ``hotframe.engine.models.Module`` —
    # is documented and enforced by tests, not by the type system.
    ModuleStateRow = Any
else:
    ModuleStateRow = Any


def _get_module_model() -> type[ModuleStateRow]:
    """Resolve the module state model from settings."""
    from hotframe.config.settings import get_settings

    settings = get_settings()
    if settings.MODULE_STATE_MODEL:
        module_path, class_name = settings.MODULE_STATE_MODEL.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)

    # Fall back to built-in model
    from hotframe.engine.models import Module

    return Module


class ModuleAlreadyInstallingError(Exception):
    pass


class ModuleStateDB:
    """CRUD operations on the module state table."""

    def _model(self) -> type[ModuleStateRow]:
        return _get_module_model()

    async def get_active_modules(self, session: ISession, **filters: Any) -> list:
        """Return all modules with status 'active', ordered by install date."""
        Model = self._model()
        stmt = select(Model).where(Model.status == "active").order_by(Model.installed_at)
        for key, value in filters.items():
            stmt = stmt.where(getattr(Model, key) == value)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_all_modules(self, session: ISession, **filters: Any) -> list:
        """Return all modules regardless of status, ordered by install date."""
        Model = self._model()
        stmt = select(Model).order_by(Model.installed_at)
        for key, value in filters.items():
            stmt = stmt.where(getattr(Model, key) == value)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_module(self, session: ISession, module_id: str, **filters: Any) -> Any | None:
        """Return a single module row by module_id, or None if not found."""
        Model = self._model()
        stmt = select(Model).where(Model.module_id == module_id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(Model, key) == value)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        session: ISession,
        module_id: str,
        version: str,
        *,
        checksum: str = "",
        status: str = "installing",
        **extra_fields: Any,
    ) -> Any:
        """Insert a new module row and flush; raises ModuleAlreadyInstallingError on duplicate.

        Args:
            session: Async SQLAlchemy session.
            module_id: Unique identifier of the module.
            version: Semantic version string.
            checksum: SHA-256 checksum of the module archive.
            status: Initial status (default ``'installing'``).
            **extra_fields: Additional columns forwarded to the model constructor.

        Returns:
            The newly created ORM instance.
        """
        from sqlalchemy.exc import IntegrityError

        Model = self._model()
        module = Model(
            module_id=module_id,
            version=version,
            checksum_sha256=checksum,
            status=status,
            manifest={},
            config={},
            **extra_fields,
        )
        session.add(module)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise ModuleAlreadyInstallingError(
                f"Module {module_id} is already being installed by another process"
            ) from None
        logger.info("Created module %s v%s (status=%s)", module_id, version, status)
        return module

    async def activate(
        self,
        session: ISession,
        module_id: str,
        manifest_dict: dict[str, Any],
        **filters: Any,
    ) -> None:
        """Set module status to 'active', update manifest, and clear error_message."""
        Model = self._model()
        now = datetime.now(UTC)
        stmt = update(Model).where(Model.module_id == module_id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(Model, key) == value)
        stmt = stmt.values(
            status="active",
            activated_at=now,
            disabled_at=None,
            manifest=manifest_dict,
            error_message=None,
        )
        await session.execute(stmt)
        logger.info("Activated module %s", module_id)

    async def deactivate(self, session: ISession, module_id: str, **filters: Any) -> None:
        """Set module status to 'disabled' and record disabled_at timestamp."""
        Model = self._model()
        now = datetime.now(UTC)
        stmt = update(Model).where(Model.module_id == module_id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(Model, key) == value)
        stmt = stmt.values(status="disabled", disabled_at=now)
        await session.execute(stmt)
        logger.info("Deactivated module %s", module_id)

    async def set_status(
        self,
        session: ISession,
        module_id: str,
        status: str,
        error: str | None = None,
        **filters: Any,
    ) -> None:
        """Update module status and adjust related timestamp fields accordingly."""
        Model = self._model()
        values: dict[str, Any] = {"status": status}
        if error is not None:
            values["error_message"] = error
        if status == "active":
            values["activated_at"] = datetime.now(UTC)
            values["disabled_at"] = None
            values["error_message"] = None
        elif status == "disabled":
            values["disabled_at"] = datetime.now(UTC)
        stmt = update(Model).where(Model.module_id == module_id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(Model, key) == value)
        stmt = stmt.values(**values)
        await session.execute(stmt)

    async def set_error(
        self,
        session: ISession,
        module_id: str,
        error_message: str,
        **filters: Any,
    ) -> None:
        """Set module status to 'error' and store the error message."""
        await self.set_status(session, module_id, "error", error=error_message, **filters)
        logger.error("Module %s error: %s", module_id, error_message)

    async def set_degraded(
        self,
        session: ISession,
        module_id: str,
        error_message: str,
        **filters: Any,
    ) -> None:
        """Mark a module as ``degraded`` after recurrent runtime errors.

        Conceptually distinct from ``set_error``: ``degraded`` means the
        module is still mounted and answering, but its boundary tracker
        crossed the failure threshold and the user should review/disable
        it. ``error`` means the module could not be loaded at all.

        ``get_active_modules`` deliberately does *not* return degraded
        rows — the next reboot leaves the module dormant until the user
        reactivates it through the UI, matching the design in doc 05.
        """
        await self.set_status(session, module_id, "degraded", error=error_message, **filters)
        logger.warning("Module %s marked degraded: %s", module_id, error_message)

    async def update_manifest(
        self,
        session: ISession,
        module_id: str,
        manifest_dict: dict[str, Any],
        **filters: Any,
    ) -> None:
        """Overwrite the stored manifest JSON for a module row."""
        Model = self._model()
        stmt = update(Model).where(Model.module_id == module_id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(Model, key) == value)
        stmt = stmt.values(manifest=manifest_dict)
        await session.execute(stmt)

    async def delete(self, session: ISession, module_id: str, **filters: Any) -> None:
        """Permanently delete a module row from the state table."""
        Model = self._model()
        stmt = delete(Model).where(Model.module_id == module_id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(Model, key) == value)
        await session.execute(stmt)
        logger.info("Deleted module %s", module_id)
