"""
Async pub/sub event system replacing Django signals.

Supports async/sync handlers, priority ordering, wildcard subscriptions,
module-scoped cleanup, fire-once subscriptions, and Pydantic-typed events.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

from hotframe.signals.types import BaseEvent, EventRegistry, ValidationMode, event_registry
from hotframe.utils.observability_metrics import (
    get_error_counter,
    get_event_emit_counter,
    get_event_handler_duration_histogram,
)
from hotframe.utils.observability_telemetry import create_event_span

logger = logging.getLogger(__name__)

# Events matching these prefixes default to fail_fast error policy
# (raise on first handler error) unless explicitly overridden.
CRITICAL_EVENT_PREFIXES = {"sale.", "payment.", "inventory."}


def _is_critical_event(event_name: str) -> bool:
    """Check if an event name matches any critical prefix."""
    return any(event_name.startswith(prefix) for prefix in CRITICAL_EVENT_PREFIXES)


def _handler_name(handler: Callable) -> str:
    """Get a readable name for a handler function (for metrics and logs)."""
    module = getattr(handler, "__module__", "")
    qualname = getattr(handler, "__qualname__", repr(handler))
    return f"{module}.{qualname}" if module else qualname


@dataclass(slots=True)
class EmitResult:
    """Result of emitting an event, exposing any handler errors."""

    event: str
    handler_count: int
    errors: list[Exception]

    @property
    def success(self) -> bool:
        return not self.errors


@dataclass(slots=True)
class HandlerEntry:
    """A registered event handler with metadata."""

    handler: Callable
    priority: int = 10
    module_id: str | None = None
    once: bool = False
    typed: bool = False


class AsyncEventBus:
    """
    Async pub/sub event system with typed event support.

    Supports two parallel interfaces for gradual migration:

    **Legacy (untyped):**
        ``await bus.emit("sales.completed", sale_id=uuid, total=99.99)``
        ``await bus.subscribe("sales.completed", handler)``

    **Typed (Pydantic):**
        ``await bus.emit_typed(SaleCompletedEvent(sale_id=uuid, total=99.99))``
        ``await bus.subscribe_typed(SaleCompletedEvent, handler)``

    Both interfaces share the same handler pool: a typed event emission also
    triggers legacy handlers subscribed to the same ``event_name``, and vice versa.

    Features:
    - Async handlers (awaited) and sync handlers (called directly)
    - Priority ordering (lower number = called first, default 10)
    - Wildcard subscriptions: ``sales.*`` matches ``sales.created``
    - Module-scoped: ``unsubscribe_module(id)`` cleans up on module unload
    - Once: fire-once subscriptions (auto-unsubscribe after first emit)
    - Thread-safe via ``asyncio.Lock``
    - Schema introspection via ``EventRegistry``
    """

    def __init__(
        self,
        *,
        registry: EventRegistry | None = None,
        validation_mode: ValidationMode = ValidationMode.PERMISSIVE,
    ) -> None:
        self._handlers: dict[str, list[HandlerEntry]] = {}
        self._lock = asyncio.Lock()
        self._registry = registry or event_registry
        self._validation_mode = validation_mode

    @property
    def registry(self) -> EventRegistry:
        """The event registry tracking typed event classes."""
        return self._registry

    @property
    def validation_mode(self) -> ValidationMode:
        return self._validation_mode

    @validation_mode.setter
    def validation_mode(self, mode: ValidationMode) -> None:
        self._validation_mode = mode

    # ------------------------------------------------------------------
    # Legacy (untyped) interface — full backward compatibility
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        event: str,
        handler: Callable,
        *,
        priority: int = 10,
        module_id: str | None = None,
        once: bool = False,
    ) -> None:
        """Register a handler for an event pattern (legacy untyped interface)."""
        entry = HandlerEntry(
            handler=handler,
            priority=priority,
            module_id=module_id,
            once=once,
            typed=False,
        )
        async with self._lock:
            if event not in self._handlers:
                self._handlers[event] = []
            self._handlers[event].append(entry)

    async def unsubscribe(self, event: str, handler: Callable) -> None:
        """Remove a specific handler from an event."""
        async with self._lock:
            entries = self._handlers.get(event)
            if entries is None:
                return
            self._handlers[event] = [e for e in entries if e.handler is not handler]
            if not self._handlers[event]:
                del self._handlers[event]

    async def emit(
        self,
        event: str,
        *,
        sender: Any = None,
        error_policy: str | None = None,
        **data: Any,
    ) -> EmitResult:
        """
        Emit an event, calling all matching handlers (legacy untyped interface).

        Matches exact event subscriptions and wildcard patterns (fnmatch).
        Handlers are sorted by priority (ascending). Fire-once handlers
        are removed after invocation.

        Automatically creates a tracing span and records metrics.

        Args:
            error_policy: ``"collect"`` (default) — gather errors in result,
                don't raise.  ``"fail_fast"`` — raise on first handler error,
                stopping further handlers.  When *None*, critical events
                (matching ``CRITICAL_EVENT_PREFIXES``) auto-select
                ``"fail_fast"``.

        Returns an ``EmitResult`` with any handler errors collected.
        """
        # Resolve effective error policy
        if error_policy is None:
            error_policy = "fail_fast" if _is_critical_event(event) else "collect"
        # Validation: warn if a typed class exists but untyped emit is used
        if self._validation_mode == ValidationMode.WARN and self._registry.is_registered(event):
            logger.warning(
                "Untyped emit for %r which has a typed event class — consider using emit_typed()",
                event,
            )

        with create_event_span(event) as span:
            async with self._lock:
                matched: list[tuple[str, HandlerEntry]] = []
                for pattern, entries in self._handlers.items():
                    if pattern == event or fnmatch(event, pattern):
                        for entry in entries:
                            matched.append((pattern, entry))

            # Sort by priority (stable sort preserves registration order for equal priorities)
            matched.sort(key=lambda pair: pair[1].priority)

            # Record emission metric
            get_event_emit_counter().add(
                1,
                attributes={"event.name": event, "event.handler_count": len(matched)},
            )
            span.set_attribute("event.handler_count", len(matched))

            once_to_remove: list[tuple[str, HandlerEntry]] = []
            errors: list[Exception] = []

            for pattern, entry in matched:
                handler_name = _handler_name(entry.handler)
                t0 = time.perf_counter()
                try:
                    if entry.typed:
                        # Typed handler expects (event: BaseEvent) — wrap legacy data
                        event_class = self._registry.get_class(event)
                        if event_class is not None:
                            try:
                                typed_event = event_class(**data)
                                if inspect.iscoroutinefunction(entry.handler):
                                    await entry.handler(typed_event)
                                else:
                                    entry.handler(typed_event)
                            except Exception as exc:
                                logger.warning(
                                    "Could not construct typed event %s from legacy data: %s",
                                    event_class.__name__,
                                    exc,
                                )
                                # Fall through — skip this handler for incompatible data
                        else:
                            logger.debug(
                                "Typed handler for %r but no event class registered — skipping",
                                event,
                            )
                    else:
                        # Legacy handler: receives (event=str, sender=..., **data)
                        if inspect.iscoroutinefunction(entry.handler):
                            await entry.handler(event=event, sender=sender, **data)
                        else:
                            entry.handler(event=event, sender=sender, **data)
                except Exception as exc:
                    logger.exception(
                        "Error in event handler %s for event %s",
                        handler_name,
                        event,
                    )
                    errors.append(exc)
                    get_error_counter().add(
                        1,
                        attributes={
                            "error.source": "event_handler",
                            "error.type": type(exc).__name__,
                            "module_id": entry.module_id or "core",
                        },
                    )
                    if error_policy == "fail_fast":
                        # Clean up any once handlers seen so far before raising
                        if once_to_remove:
                            async with self._lock:
                                for p, e in once_to_remove:
                                    cleanup_entries = self._handlers.get(p)
                                    if cleanup_entries and e in cleanup_entries:
                                        cleanup_entries.remove(e)
                                    if cleanup_entries is not None and not cleanup_entries:
                                        del self._handlers[p]
                        span.set_attribute("event.error_count", len(errors))
                        raise
                finally:
                    duration_ms = (time.perf_counter() - t0) * 1000
                    get_event_handler_duration_histogram().record(
                        duration_ms,
                        attributes={
                            "event.name": event,
                            "handler": handler_name,
                        },
                    )

                if entry.once:
                    once_to_remove.append((pattern, entry))

            # Clean up once handlers
            if once_to_remove:
                async with self._lock:
                    for pattern, entry in once_to_remove:
                        cleanup_entries = self._handlers.get(pattern)
                        if cleanup_entries and entry in cleanup_entries:
                            cleanup_entries.remove(entry)
                        if cleanup_entries is not None and not cleanup_entries:
                            del self._handlers[pattern]

            if errors:
                span.set_attribute("event.error_count", len(errors))

            return EmitResult(event=event, handler_count=len(matched), errors=errors)

    # ------------------------------------------------------------------
    # Typed interface — Pydantic-validated events
    # ------------------------------------------------------------------

    async def subscribe_typed(
        self,
        event_class: type[BaseEvent],
        handler: Callable,
        *,
        priority: int = 10,
        module_id: str | None = None,
        once: bool = False,
    ) -> None:
        """
        Register a typed handler for a Pydantic event class.

        The handler receives a single argument: the validated event instance.

        Example::

            async def on_sale(event: SaleCompletedEvent) -> None:
                print(f"Sale {event.sale_id} for {event.total}")

            await bus.subscribe_typed(SaleCompletedEvent, on_sale, module_id="analytics")
        """
        event_name = event_class.event_name
        if not event_name:
            raise ValueError(f"Event class {event_class.__name__} must define event_name ClassVar")

        # Auto-register the event class if not already known
        if not self._registry.is_registered(event_name):
            self._registry.register(event_class)

        entry = HandlerEntry(
            handler=handler,
            priority=priority,
            module_id=module_id,
            once=once,
            typed=True,
        )
        async with self._lock:
            if event_name not in self._handlers:
                self._handlers[event_name] = []
            self._handlers[event_name].append(entry)

    async def emit_typed(self, event: BaseEvent) -> EmitResult:
        """
        Emit a typed Pydantic event.

        Validates the event, auto-populates context fields, and dispatches to
        both typed handlers (receive the event object) and legacy handlers
        (receive ``event=str, sender=None, **data``).

        Automatically creates a tracing span and records metrics.

        Example::

            result = await bus.emit_typed(
                SaleCompletedEvent(sale_id=uuid, total=Decimal("99.99"))
            )
        """
        event_name = type(event).event_name
        if not event_name:
            raise ValueError(
                f"Event instance {type(event).__name__} must define event_name ClassVar"
            )

        # Auto-register if not yet known
        if not self._registry.is_registered(event_name):
            self._registry.register(type(event))

        with create_event_span(event_name) as span:
            # Gather matching handlers
            async with self._lock:
                matched: list[tuple[str, HandlerEntry]] = []
                for pattern, entries in self._handlers.items():
                    if pattern == event_name or fnmatch(event_name, pattern):
                        for entry in entries:
                            matched.append((pattern, entry))

            matched.sort(key=lambda pair: pair[1].priority)

            # Record emission metric
            get_event_emit_counter().add(
                1,
                attributes={"event.name": event_name, "event.handler_count": len(matched)},
            )
            span.set_attribute("event.handler_count", len(matched))

            once_to_remove: list[tuple[str, HandlerEntry]] = []
            errors: list[Exception] = []

            # Pre-compute legacy kwargs once (only if we have legacy handlers)
            legacy_kwargs: dict[str, Any] | None = None

            for pattern, entry in matched:
                handler_name = _handler_name(entry.handler)
                t0 = time.perf_counter()
                try:
                    if entry.typed:
                        # Typed handler: receives the event object directly
                        if inspect.iscoroutinefunction(entry.handler):
                            await entry.handler(event)
                        else:
                            entry.handler(event)
                    else:
                        # Legacy handler: receives (event=str, sender=None, **data)
                        if legacy_kwargs is None:
                            legacy_kwargs = event.to_emit_kwargs()
                        if inspect.iscoroutinefunction(entry.handler):
                            await entry.handler(event=event_name, sender=None, **legacy_kwargs)
                        else:
                            entry.handler(event=event_name, sender=None, **legacy_kwargs)
                except Exception as exc:
                    logger.exception(
                        "Error in event handler %s for typed event %s",
                        handler_name,
                        event_name,
                    )
                    errors.append(exc)
                    get_error_counter().add(
                        1,
                        attributes={
                            "error.source": "event_handler",
                            "error.type": type(exc).__name__,
                            "module_id": entry.module_id or "core",
                        },
                    )
                finally:
                    duration_ms = (time.perf_counter() - t0) * 1000
                    get_event_handler_duration_histogram().record(
                        duration_ms,
                        attributes={
                            "event.name": event_name,
                            "handler": handler_name,
                        },
                    )

                if entry.once:
                    once_to_remove.append((pattern, entry))

            # Clean up once handlers
            if once_to_remove:
                async with self._lock:
                    for pattern, entry in once_to_remove:
                        cleanup_entries = self._handlers.get(pattern)
                        if cleanup_entries and entry in cleanup_entries:
                            cleanup_entries.remove(entry)
                        if cleanup_entries is not None and not cleanup_entries:
                            del self._handlers[pattern]

            if errors:
                span.set_attribute("event.error_count", len(errors))

            return EmitResult(event=event_name, handler_count=len(matched), errors=errors)

    # ------------------------------------------------------------------
    # Module cleanup
    # ------------------------------------------------------------------

    async def unsubscribe_module(self, module_id: str) -> None:
        """
        Remove ALL handlers registered by a module.

        Called during module unload.  Acquires the lock to avoid racing
        with concurrent ``emit()`` calls that iterate ``_handlers``.
        """
        async with self._lock:
            empty_patterns: list[str] = []
            for pattern, entries in self._handlers.items():
                self._handlers[pattern] = [e for e in entries if e.module_id != module_id]
                if not self._handlers[pattern]:
                    empty_patterns.append(pattern)
            for pattern in empty_patterns:
                del self._handlers[pattern]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_handlers(self, event: str) -> list[HandlerEntry]:
        """
        List all handlers that would match a given event.

        Includes both exact and wildcard matches. For diagnostics.
        """
        matched: list[HandlerEntry] = []
        for pattern, entries in self._handlers.items():
            if pattern == event or fnmatch(event, pattern):
                matched.extend(entries)
        matched.sort(key=lambda e: e.priority)
        return matched

    def list_typed_events(self) -> dict[str, type[BaseEvent]]:
        """List all registered typed event classes."""
        return self._registry.list_events()

    def list_event_schemas(self) -> dict[str, dict[str, Any]]:
        """Return JSON schemas for all registered typed events."""
        return self._registry.list_schemas()

    def clear(self) -> None:
        """Remove all handlers. Intended for testing."""
        self._handlers.clear()

    @property
    def handler_count(self) -> int:
        """Total number of registered handlers across all events."""
        return sum(len(entries) for entries in self._handlers.values())

    def __repr__(self) -> str:
        patterns = len(self._handlers)
        handlers = self.handler_count
        typed = self._registry.count
        return f"<AsyncEventBus patterns={patterns} handlers={handlers} typed_events={typed}>"
