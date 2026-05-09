"""
Pydantic-based typed event system.

Provides immutable, validated event objects with auto-populated context fields.
Designed for gradual migration — existing untyped ``bus.emit("event", **kwargs)``
continues to work alongside new ``bus.emit_typed(SaleCompletedEvent(...))`` calls.

Architecture:
    - ``BaseEvent`` — frozen Pydantic model with auto context (hub_id, user_id, etc.)
    - ``EventRegistry`` — tracks all registered event types for schema introspection
    - ``ValidationMode`` — controls strictness during migration (PERMISSIVE → STRICT)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)


class ValidationMode(str, Enum):
    """Controls how the event bus handles typed event validation."""

    STRICT = "strict"
    """Reject malformed events with an exception."""

    WARN = "warn"
    """Log a warning for malformed events but still emit them."""

    PERMISSIVE = "permissive"
    """No validation beyond Pydantic's own model validation."""


class BaseEvent(BaseModel):
    """
    Base class for all typed events.

    Subclasses MUST define ``event_name`` as a ClassVar string following
    the ``namespace.action`` convention (e.g., ``"sales.completed"``).

    Auto-populated fields:
        - ``event_id`` — unique UUID per event instance
        - ``timestamp`` — UTC datetime of event creation
        - ``hub_id`` — from request contextvars (if available)
        - ``triggered_by`` — user UUID from request contextvars (if available)
        - ``source_module`` — originating module identifier

    All events are frozen (immutable) after creation.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        ser_json_timedelta="iso8601",
        json_schema_extra={"description": "Hotframe typed event"},
    )

    event_name: ClassVar[str]
    """Signal string matching the ``namespace.action`` convention."""

    # --- Auto-populated context ---
    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    hub_id: UUID | None = None
    triggered_by: UUID | None = None
    source_module: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _populate_context(cls, data: Any) -> Any:
        """Auto-populate hub_id and triggered_by from request contextvars."""
        if not isinstance(data, dict):
            return data

        # Only fill if not explicitly provided
        if data.get("hub_id") is None or data.get("triggered_by") is None:
            try:
                from hotframe.utils.observability_context import request_context

                ctx = request_context.get()
                if data.get("hub_id") is None and ctx.hub_id:
                    try:
                        data["hub_id"] = UUID(ctx.hub_id)
                    except (ValueError, AttributeError):
                        pass
                if data.get("triggered_by") is None and ctx.user_id:
                    try:
                        data["triggered_by"] = UUID(ctx.user_id)
                    except (ValueError, AttributeError):
                        pass
            except (ImportError, LookupError):
                # Context not available (CLI, tests, migrations)
                pass

        return data

    def to_emit_kwargs(self) -> dict[str, Any]:
        """
        Convert to kwargs compatible with the legacy ``bus.emit()`` interface.

        Returns a dict with all model fields (excluding ClassVar) suitable
        for ``bus.emit(self.event_name, **event.to_emit_kwargs())``.
        """
        return self.model_dump(mode="python")


class EventRegistry:
    """
    Singleton registry tracking all known typed event classes.

    Provides schema introspection: list all event types, get JSON schemas,
    and look up event classes by their ``event_name`` string.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, type[BaseEvent]] = {}
        self._by_class: dict[type[BaseEvent], str] = {}

    def register(self, event_class: type[BaseEvent]) -> type[BaseEvent]:
        """
        Register a typed event class.

        Can be used as a decorator::

            @registry.register
            class SaleCompletedEvent(BaseEvent):
                event_name = "sales.completed"
                ...
        """
        name = event_class.event_name
        if not name:
            raise ValueError(
                f"Event class {event_class.__name__} must define a non-empty event_name ClassVar"
            )

        existing = self._by_name.get(name)
        if existing is not None and existing is not event_class:
            raise ValueError(
                f"Event name {name!r} already registered by {existing.__name__}, "
                f"cannot register {event_class.__name__}"
            )

        self._by_name[name] = event_class
        self._by_class[event_class] = name
        logger.debug("Registered typed event: %s → %s", name, event_class.__name__)
        return event_class

    def get_class(self, event_name: str) -> type[BaseEvent] | None:
        """Look up the event class for a given event_name string."""
        return self._by_name.get(event_name)

    def get_name(self, event_class: type[BaseEvent]) -> str | None:
        """Look up the event_name for a given event class."""
        return self._by_class.get(event_class)

    def is_registered(self, event_name: str) -> bool:
        """Check if an event_name has a registered typed class."""
        return event_name in self._by_name

    def list_events(self) -> dict[str, type[BaseEvent]]:
        """Return a copy of all registered event_name → class mappings."""
        return dict(self._by_name)

    def list_schemas(self) -> dict[str, dict[str, Any]]:
        """Return JSON schemas for all registered event types."""
        return {name: cls.model_json_schema() for name, cls in self._by_name.items()}

    def clear(self) -> None:
        """Remove all registrations. Intended for testing."""
        self._by_name.clear()
        self._by_class.clear()

    @property
    def count(self) -> int:
        return len(self._by_name)

    def __repr__(self) -> str:
        return f"<EventRegistry events={self.count}>"


# --- Module-level singleton ---
event_registry = EventRegistry()
"""Global event registry. Import and use directly or via ``register_event`` decorator."""


def register_event(cls: type[BaseEvent]) -> type[BaseEvent]:
    """
    Decorator to register a typed event class in the global registry.

    Usage::

        @register_event
        class SaleCompletedEvent(BaseEvent):
            event_name = "sales.completed"
            sale_id: UUID
            total: Decimal
    """
    return event_registry.register(cls)
