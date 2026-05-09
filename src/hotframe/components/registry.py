# SPDX-License-Identifier: Apache-2.0
"""
Component registry — in-memory catalogue of reusable UI components.

A component (see :class:`ComponentEntry`) is a named, callable UI unit
that templates invoke via ``render_component(name, ...)``. The registry
stores one entry per name and supports module-scoped cleanup so a module
that registers components at load time can have them all dropped on
unload (including failed-load rollback).

This module mirrors the pattern of
:class:`hotframe.templating.slots.SlotRegistry`: a flat dict keyed by
a stable identifier, module ownership tracked on the entry itself, and
an ``unregister_module`` helper for teardown.

Usage::

    registry = ComponentRegistry()
    registry.register(
        ComponentEntry(name="button", template="ui/button.html"),
        module_id="ui_kit",
    )
    entry = registry.get("button")
    "button" in registry   # True
    len(registry)          # 1
    registry.unregister_module("ui_kit")
"""

from __future__ import annotations

import logging

from hotframe.components.entry import ComponentEntry

logger = logging.getLogger(__name__)


class ComponentRegistry:
    """
    Registry for reusable UI components.

    Components are keyed by :attr:`ComponentEntry.name`. Each entry may
    declare an owning ``module_id`` so the registry can drop all entries
    contributed by a module during unload. Name collisions log a warning
    and overwrite the previous entry — this mirrors the dev-time reload
    cycle where a module is re-imported with updated definitions.
    """

    def __init__(self) -> None:
        self._components: dict[str, ComponentEntry] = {}

    def register(
        self,
        entry: ComponentEntry,
        *,
        module_id: str | None = None,
    ) -> None:
        """
        Register a component entry.

        The entry is stored keyed by ``entry.name``. If ``module_id`` is
        provided, it overrides any value already on the entry so the
        registry owns teardown information even when callers build
        ``ComponentEntry`` instances without setting it.

        Args:
            entry: The component entry to register.
            module_id: Optional owning module ID for scoped cleanup on
                unload. When provided, it is written back to
                ``entry.module_id``.

        Notes:
            A name collision logs a warning and overwrites the existing
            entry. This is intentional to support dev-time module reload.
        """
        if module_id is not None:
            entry.module_id = module_id

        if entry.name in self._components:
            previous = self._components[entry.name]
            logger.warning(
                "Component name collision: %r is being overwritten "
                "(previous module=%s, new module=%s)",
                entry.name,
                previous.module_id,
                entry.module_id,
            )

        self._components[entry.name] = entry

    def unregister(self, name: str) -> None:
        """Remove a component by name. No-op if the name is not registered."""
        self._components.pop(name, None)

    def unregister_module(self, module_id: str) -> None:
        """
        Remove every component registered by ``module_id``.

        Called during module unload (and load-failure rollback) so
        module teardown leaves no stale component definitions behind.
        Mirrors :meth:`hotframe.templating.slots.SlotRegistry.unregister_module`.
        """
        to_remove = [
            name for name, entry in self._components.items() if entry.module_id == module_id
        ]
        for name in to_remove:
            del self._components[name]

    def get(self, name: str) -> ComponentEntry | None:
        """Return the entry for ``name`` or ``None`` if not registered."""
        return self._components.get(name)

    def has(self, name: str) -> bool:
        """Return True if a component with ``name`` is registered."""
        return name in self._components

    def list_components(self) -> list[ComponentEntry]:
        """Return all registered component entries. Insertion-ordered."""
        return list(self._components.values())

    def clear(self) -> None:
        """Remove all component entries. Intended for testing."""
        self._components.clear()

    def __len__(self) -> int:
        return len(self._components)

    def __contains__(self, name: object) -> bool:
        return name in self._components

    def __repr__(self) -> str:
        return f"<ComponentRegistry components={len(self._components)}>"
