"""
Dev hot-reload watcher for modules.

Watches the modules directory for file changes and triggers a hot-reload
of the affected module. Only active in development mode (``DEBUG=True``).

Uses ``watchfiles`` for efficient filesystem monitoring with native
OS-level change notification (FSEvents on macOS, inotify on Linux).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)


class ModuleWatcher:
    """
    Watch modules directory for changes, trigger hot-reload.

    Each subdirectory under ``modules_dir`` is treated as a module.
    When any file inside ``modules_dir/{module_id}/`` changes, the
    ``on_change`` callback is called with the ``module_id``.

    Only watches ``.py``, ``.html``, ``.json`` files by default.
    """

    WATCH_EXTENSIONS = frozenset({".py", ".html", ".json", ".jinja2"})

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    async def start(
        self,
        modules_dir: Path,
        on_change: Callable[[str], object],
    ) -> None:
        """
        Start watching the modules directory for changes.

        Args:
            modules_dir: Root directory containing module subdirectories.
            on_change: Async or sync callable receiving the ``module_id``
                       of the changed module.
        """
        if self._task is not None:
            logger.warning("ModuleWatcher already running")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._watch_loop(modules_dir, on_change),
            name="module-watcher",
        )
        logger.info("ModuleWatcher started for %s", modules_dir)

    async def stop(self) -> None:
        """Stop watching."""
        if self._task is None:
            return

        self._stop_event.set()
        self._task.cancel()

        try:
            await self._task
        except asyncio.CancelledError:
            pass

        self._task = None
        logger.info("ModuleWatcher stopped")

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _watch_loop(
        self,
        modules_dir: Path,
        on_change: Callable[[str], object],
    ) -> None:
        """Internal watch loop using watchfiles."""
        try:
            from watchfiles import awatch
        except ImportError:
            logger.warning(
                "watchfiles not installed — hot-reload disabled. "
                "Install with: pip install watchfiles"
            )
            return

        debounce_ms = 300
        recently_reloaded: dict[str, float] = {}

        try:
            async for changes in awatch(
                modules_dir,
                stop_event=self._stop_event,
                debounce=debounce_ms,
                recursive=True,
            ):
                # Deduplicate: collect unique module_ids from changed paths
                changed_modules: set[str] = set()

                for _change_type, changed_path in changes:
                    path = Path(changed_path)

                    # Only watch relevant file types
                    if path.suffix not in self.WATCH_EXTENSIONS:
                        continue

                    # Extract module_id from path
                    module_id = self._extract_module_id(modules_dir, path)
                    if module_id:
                        changed_modules.add(module_id)

                # Trigger reload for each changed module
                now = asyncio.get_event_loop().time()
                for module_id in changed_modules:
                    # Simple debounce: skip if reloaded within last second
                    last = recently_reloaded.get(module_id, 0)
                    if now - last < 1.0:
                        continue
                    recently_reloaded[module_id] = now

                    logger.info("Change detected in module %s — triggering hot-reload", module_id)
                    try:
                        result = on_change(module_id)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception("Error during hot-reload of %s", module_id)

        except asyncio.CancelledError:
            pass

    @staticmethod
    def _extract_module_id(modules_dir: Path, changed_path: Path) -> str | None:
        """
        Extract the module_id from a changed file path.

        Given ``modules_dir=/tmp/modules`` and ``changed_path=/tmp/modules/inventory/routes.py``
        returns ``inventory``.
        """
        try:
            relative = changed_path.relative_to(modules_dir)
            parts = relative.parts
            if parts:
                return parts[0]
        except ValueError:
            pass
        return None
