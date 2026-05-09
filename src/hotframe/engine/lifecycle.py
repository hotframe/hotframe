"""
Module lifecycle hook caller.

Each module may define a ``lifecycle.py`` file with async functions:

- ``on_install(session, hub_id)`` — create initial data, seed defaults
- ``on_activate(session, hub_id)`` — enable functionality
- ``on_deactivate(session, hub_id)`` — disable, clean up caches
- ``on_uninstall(session, hub_id)`` — permanent data cleanup
- ``on_upgrade(session, hub_id, from_version, to_version)`` — data migration

All hooks are optional. If a hook raises, the error is logged but
propagated to the caller (which decides whether to abort or continue).
"""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from hotframe.db.protocols import ISession

logger = logging.getLogger(__name__)

# Valid lifecycle hook names
LIFECYCLE_HOOKS = frozenset(
    {
        "on_install",
        "on_activate",
        "on_deactivate",
        "on_uninstall",
        "on_upgrade",
    }
)


class ModuleLifecycleManager:
    """
    Calls lifecycle hooks defined in a module's ``lifecycle.py``.

    The module must already be importable (i.e. its parent dir is in
    ``sys.path`` and it has been loaded by the :class:`ModuleLoader`).
    """

    async def call(
        self,
        module_id: str,
        hook_name: str,
        session: ISession,
        hub_id: UUID,
        **kwargs,
    ) -> None:
        """
        Invoke a lifecycle hook on a module if it exists.

        Args:
            module_id: The module identifier.
            hook_name: One of the :data:`LIFECYCLE_HOOKS` (e.g. ``on_install``).
            session: Active DB session for the hook to use.
            hub_id: The hub this module belongs to.
            **kwargs: Extra keyword arguments (e.g. ``from_version``, ``to_version``).

        Raises:
            ValueError: If *hook_name* is not a recognized lifecycle hook.
            Exception: Re-raises whatever the hook function raises.
        """
        if hook_name not in LIFECYCLE_HOOKS:
            raise ValueError(
                f"Unknown lifecycle hook: {hook_name!r}. Valid hooks: {sorted(LIFECYCLE_HOOKS)}"
            )

        lifecycle_mod = self._try_import_lifecycle(module_id)
        if lifecycle_mod is None:
            return

        hook_fn = getattr(lifecycle_mod, hook_name, None)
        if hook_fn is None:
            logger.debug(
                "Module %s has lifecycle.py but no %s hook",
                module_id,
                hook_name,
            )
            return

        logger.info("Calling %s.lifecycle.%s", module_id, hook_name)

        try:
            if inspect.iscoroutinefunction(hook_fn):
                await hook_fn(session=session, hub_id=hub_id, **kwargs)
            else:
                hook_fn(session=session, hub_id=hub_id, **kwargs)
        except Exception:
            logger.exception(
                "Error in %s.lifecycle.%s for hub %s",
                module_id,
                hook_name,
                hub_id,
            )
            raise

    async def has_hook(self, module_id: str, hook_name: str) -> bool:
        """Check if a module defines a specific lifecycle hook."""
        lifecycle_mod = self._try_import_lifecycle(module_id)
        if lifecycle_mod is None:
            return False
        return hasattr(lifecycle_mod, hook_name)

    @staticmethod
    def _try_import_lifecycle(module_id: str):
        """Try to import ``{module_id}.lifecycle``. Returns module or None."""
        fqn = f"{module_id}.lifecycle"
        try:
            return importlib.import_module(fqn)
        except ModuleNotFoundError:
            return None
        except Exception:
            logger.exception("Error importing %s", fqn)
            return None
