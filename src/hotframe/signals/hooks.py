"""
WordPress-style actions and filters — async-ready hook system.

Actions execute side effects. Filters transform values through a chain.
Both support priority ordering and module-scoped cleanup.

Usage::

    hooks = HookRegistry()

    # Register
    hooks.add_action("sale.before_complete", validate_stock, priority=5, module_id="inventory")
    hooks.add_filter("sale.line_price", apply_discount, priority=10, module_id="loyalty")

    # Execute
    await hooks.do_action("sale.before_complete", sale=sale)
    final_price = await hooks.apply_filters("sale.line_price", base_price, item=item)

    # Module cleanup
    hooks.remove_module_hooks("inventory")
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hotframe.utils.observability_metrics import (
    get_error_counter,
    get_hook_callback_counter,
    get_hook_duration_histogram,
)
from hotframe.utils.observability_telemetry import create_hook_span

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ActionResult:
    """Result of executing an action hook, exposing any callback errors."""

    hook: str
    callback_count: int
    errors: list[Exception]

    @property
    def success(self) -> bool:
        return not self.errors


@dataclass(slots=True)
class HookEntry:
    """A registered hook callback with metadata."""

    callback: Callable
    priority: int = 10
    module_id: str | None = None


class HookRegistry:
    """
    WordPress-style actions and filters with async support.

    - **Actions** — fire-and-forget side effects (no return value).
    - **Filters** — chained transformations (each receives previous value).
    - Priority ordering (lower = first, default 10).
    - Module-scoped removal via ``remove_module_hooks(module_id)``.
    - Supports both sync and async callbacks.
    """

    def __init__(self) -> None:
        self._actions: dict[str, list[HookEntry]] = {}
        self._filters: dict[str, list[HookEntry]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def add_action(
        self,
        hook: str,
        callback: Callable,
        *,
        priority: int = 10,
        module_id: str | None = None,
    ) -> None:
        """Register a callback to be executed when an action fires."""
        entry = HookEntry(callback=callback, priority=priority, module_id=module_id)
        self._actions.setdefault(hook, []).append(entry)

    def add_filter(
        self,
        hook: str,
        callback: Callable,
        *,
        priority: int = 10,
        module_id: str | None = None,
    ) -> None:
        """Register a callback in the filter chain."""
        entry = HookEntry(callback=callback, priority=priority, module_id=module_id)
        self._filters.setdefault(hook, []).append(entry)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def do_action(self, hook: str, **kwargs: Any) -> ActionResult:
        """
        Execute all action callbacks for a hook in priority order.

        Supports both sync and async callbacks. Exceptions in individual
        callbacks are logged but do not prevent subsequent callbacks from running.

        Automatically creates a tracing span and records metrics.

        Returns an ``ActionResult`` with any callback errors collected.
        Callers that ignore the return value keep working as before.
        """
        entries = self._actions.get(hook)
        if not entries:
            return ActionResult(hook=hook, callback_count=0, errors=[])

        sorted_entries = sorted(entries, key=lambda e: e.priority)
        errors: list[Exception] = []

        with create_hook_span(hook, "action") as span:
            span.set_attribute("hook.callback_count", len(sorted_entries))

            for entry in sorted_entries:
                callback_name = _callback_name(entry.callback)
                t0 = time.perf_counter()
                try:
                    if inspect.iscoroutinefunction(entry.callback):
                        await entry.callback(**kwargs)
                    else:
                        entry.callback(**kwargs)
                except Exception as exc:
                    logger.exception(
                        "Error in action hook %s callback %s (module=%s)",
                        hook,
                        callback_name,
                        entry.module_id,
                    )
                    errors.append(exc)
                    get_error_counter().add(
                        1,
                        attributes={
                            "error.source": "hook_callback",
                            "error.type": type(exc).__name__,
                            "module_id": entry.module_id or "core",
                        },
                    )
                finally:
                    duration_ms = (time.perf_counter() - t0) * 1000
                    get_hook_duration_histogram().record(
                        duration_ms,
                        attributes={"hook.name": hook, "hook.type": "action"},
                    )
                    get_hook_callback_counter().add(
                        1,
                        attributes={"hook.name": hook, "hook.type": "action"},
                    )

            if errors:
                span.set_attribute("hook.error_count", len(errors))

        return ActionResult(hook=hook, callback_count=len(sorted_entries), errors=errors)

    async def apply_filters(self, hook: str, value: Any, **kwargs: Any) -> Any:
        """
        Chain filter callbacks, each receiving the previous return value.

        The first callback receives ``value`` as its first argument.
        Each subsequent callback receives the return value of the previous one.
        Supports both sync and async callbacks.

        Automatically creates a tracing span and records metrics.

        Returns the final transformed value.
        """
        entries = self._filters.get(hook)
        if not entries:
            return value

        sorted_entries = sorted(entries, key=lambda e: e.priority)

        result = value
        with create_hook_span(hook, "filter") as span:
            span.set_attribute("hook.callback_count", len(sorted_entries))

            for entry in sorted_entries:
                callback_name = _callback_name(entry.callback)
                t0 = time.perf_counter()
                try:
                    if inspect.iscoroutinefunction(entry.callback):
                        result = await entry.callback(result, **kwargs)
                    else:
                        result = entry.callback(result, **kwargs)
                except Exception:
                    logger.exception(
                        "Error in filter hook %s callback %s (module=%s)",
                        hook,
                        callback_name,
                        entry.module_id,
                    )
                    get_error_counter().add(
                        1,
                        attributes={
                            "error.source": "hook_callback",
                            "error.type": "filter_error",
                            "module_id": entry.module_id or "core",
                        },
                    )
                finally:
                    duration_ms = (time.perf_counter() - t0) * 1000
                    get_hook_duration_histogram().record(
                        duration_ms,
                        attributes={"hook.name": hook, "hook.type": "filter"},
                    )
                    get_hook_callback_counter().add(
                        1,
                        attributes={"hook.name": hook, "hook.type": "filter"},
                    )

        return result

    # ------------------------------------------------------------------
    # Removal
    # ------------------------------------------------------------------

    def remove_action(
        self,
        hook: str,
        callback: Callable | None = None,
        module_id: str | None = None,
    ) -> None:
        """
        Remove action callbacks by callback reference, module_id, or both.

        If neither callback nor module_id is provided, removes ALL actions for the hook.
        """
        self._remove_from(self._actions, hook, callback, module_id)

    def remove_filter(
        self,
        hook: str,
        callback: Callable | None = None,
        module_id: str | None = None,
    ) -> None:
        """
        Remove filter callbacks by callback reference, module_id, or both.

        If neither callback nor module_id is provided, removes ALL filters for the hook.
        """
        self._remove_from(self._filters, hook, callback, module_id)

    def remove_module_hooks(self, module_id: str) -> None:
        """
        Remove ALL hooks (actions and filters) registered by a module.

        Called during module unload to ensure clean teardown.
        """
        for registry in (self._actions, self._filters):
            empty_hooks: list[str] = []
            for hook, entries in registry.items():
                registry[hook] = [e for e in entries if e.module_id != module_id]
                if not registry[hook]:
                    empty_hooks.append(hook)
            for hook in empty_hooks:
                del registry[hook]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def has_action(self, hook: str) -> bool:
        """Check if any action callbacks are registered for a hook."""
        return bool(self._actions.get(hook))

    def has_filter(self, hook: str) -> bool:
        """Check if any filter callbacks are registered for a hook."""
        return bool(self._filters.get(hook))

    def list_hooks(self) -> dict[str, int]:
        """
        List all registered hooks with handler counts.

        Returns a dict mapping hook name to total handler count
        (actions + filters combined).
        """
        result: dict[str, int] = {}
        for hook, entries in self._actions.items():
            result[hook] = result.get(hook, 0) + len(entries)
        for hook, entries in self._filters.items():
            result[hook] = result.get(hook, 0) + len(entries)
        return result

    def clear(self) -> None:
        """Remove all hooks. Intended for testing."""
        self._actions.clear()
        self._filters.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _remove_from(
        registry: dict[str, list[HookEntry]],
        hook: str,
        callback: Callable | None,
        module_id: str | None,
    ) -> None:
        """Remove entries from a registry dict by callback and/or module_id."""
        if hook not in registry:
            return

        if callback is None and module_id is None:
            # Remove all entries for this hook
            del registry[hook]
            return

        entries = registry[hook]
        registry[hook] = [
            e
            for e in entries
            if not (
                (callback is None or e.callback is callback)
                and (module_id is None or e.module_id == module_id)
            )
        ]

        if not registry[hook]:
            del registry[hook]

    def __repr__(self) -> str:
        actions = sum(len(v) for v in self._actions.values())
        filters = sum(len(v) for v in self._filters.values())
        return f"<HookRegistry actions={actions} filters={filters}>"


def _callback_name(callback: Callable) -> str:
    """Return a human-readable name for a callback function."""
    module = getattr(callback, "__module__", "")
    qualname = getattr(callback, "__qualname__", repr(callback))
    if module:
        return f"{module}.{qualname}"
    return qualname
