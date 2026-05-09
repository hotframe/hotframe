"""
Slot system for cross-module UI extensibility.

Allows a module to inject UI content into another module's templates
without direct coupling. For example, the ``loyalty`` module can inject
a points badge into the ``customers`` detail view.

Standard slot locations:
- pos_header_start, pos_header_end
- pos_cart_item, pos_cart_footer
- customers_detail_tabs, customers_detail_sidebar
- product_detail_tabs
- dashboard_widgets
- settings_sections

Usage::

    # Module registers content for a slot
    slots.register(
        "customers_detail_sidebar",
        template="loyalty/partials/customer_badge.html",
        priority=5,
        module_id="loyalty",
        context_fn=get_loyalty_context,
        condition_fn=has_loyalty_card,
    )

    # In Jinja2 templates
    {{ render_slot('customers_detail_sidebar', customer=customer) }}
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SlotEntry:
    """A registered piece of content for a named slot."""

    template: str
    priority: int = 10
    module_id: str | None = None
    context_fn: Callable | None = None
    condition_fn: Callable | None = None


class SlotRegistry:
    """
    Registry for cross-module UI slot content.

    Modules register Jinja2 template fragments for named slots.
    Templates render all registered content for a slot via ``render_slot()``.
    Supports priority ordering, conditional rendering, and module-scoped cleanup.
    """

    def __init__(self) -> None:
        self._slots: dict[str, list[SlotEntry]] = {}

    def register(
        self,
        slot_name: str,
        template: str,
        *,
        priority: int = 10,
        module_id: str | None = None,
        context_fn: Callable | None = None,
        condition_fn: Callable | None = None,
    ) -> None:
        """
        Register content for a named slot.

        Args:
            slot_name: The slot identifier (e.g. ``customers_detail_sidebar``).
            template: Jinja2 template path to render for this slot.
            priority: Rendering order (lower = rendered first, default 10).
            module_id: Owning module ID for scoped cleanup on unload.
            context_fn: Async or sync callable returning extra template context.
            condition_fn: Async or sync callable returning bool (show/hide).
        """
        entry = SlotEntry(
            template=template,
            priority=priority,
            module_id=module_id,
            context_fn=context_fn,
            condition_fn=condition_fn,
        )
        self._slots.setdefault(slot_name, []).append(entry)

    async def get_entries(
        self,
        slot_name: str,
        request: Any = None,
        **extra_context: Any,
    ) -> list[tuple[SlotEntry, dict[str, Any]]]:
        """
        Get visible slot entries with their resolved contexts.

        Returns a list of (entry, context_dict) tuples, sorted by priority,
        filtered by condition_fn results.
        """
        entries = self._slots.get(slot_name)
        if not entries:
            return []

        sorted_entries = sorted(entries, key=lambda e: e.priority)
        result: list[tuple[SlotEntry, dict[str, Any]]] = []

        for entry in sorted_entries:
            # Check condition
            if entry.condition_fn is not None:
                try:
                    if inspect.iscoroutinefunction(entry.condition_fn):
                        visible = await entry.condition_fn(request=request, **extra_context)
                    else:
                        visible = entry.condition_fn(request=request, **extra_context)
                    if not visible:
                        continue
                except Exception:
                    logger.exception(
                        "Error in slot condition %r for slot %r (module=%s)",
                        entry.condition_fn,
                        slot_name,
                        entry.module_id,
                    )
                    continue

            # Resolve extra context
            ctx: dict[str, Any] = dict(extra_context)
            if entry.context_fn is not None:
                try:
                    if inspect.iscoroutinefunction(entry.context_fn):
                        extra = await entry.context_fn(request=request, **extra_context)
                    else:
                        extra = entry.context_fn(request=request, **extra_context)
                    if isinstance(extra, dict):
                        ctx.update(extra)
                except Exception:
                    logger.exception(
                        "Error in slot context_fn %r for slot %r (module=%s)",
                        entry.context_fn,
                        slot_name,
                        entry.module_id,
                    )

            result.append((entry, ctx))

        return result

    def unregister_module(self, module_id: str) -> None:
        """
        Remove all slot entries registered by a module.

        Called during module unload to ensure clean teardown.
        """
        empty_slots: list[str] = []
        for slot_name, entries in self._slots.items():
            self._slots[slot_name] = [e for e in entries if e.module_id != module_id]
            if not self._slots[slot_name]:
                empty_slots.append(slot_name)
        for slot_name in empty_slots:
            del self._slots[slot_name]

    def has_content(self, slot_name: str) -> bool:
        """Check if any content is registered for a slot."""
        return bool(self._slots.get(slot_name))

    def list_slots(self) -> dict[str, int]:
        """List all slots with their entry counts. For diagnostics."""
        return {name: len(entries) for name, entries in self._slots.items()}

    def clear(self) -> None:
        """Remove all slot entries. Intended for testing."""
        self._slots.clear()

    def __repr__(self) -> str:
        slots = len(self._slots)
        entries = sum(len(v) for v in self._slots.values())
        return f"<SlotRegistry slots={slots} entries={entries}>"
