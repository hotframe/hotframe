"""
SQLAlchemy ORM event listeners that bridge model lifecycle to the AsyncEventBus.

Automatically emits:
    - Typed events (``ModelPostSaveEvent``, ``ModelPostDeleteEvent``, etc.)
    - Legacy string events (``model.post_save``, ``{tablename}.created``, etc.)

Both typed and legacy handlers are triggered. Existing untyped subscribers
continue to work without modification.

Usage::

    from hotframe.orm.events import setup_orm_events
    setup_orm_events(bus)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapper, Session

if TYPE_CHECKING:
    from sqlalchemy.orm import DeclarativeBase


logger = logging.getLogger(__name__)


def _emit_async(bus: Any, event_name: str, **kwargs: Any) -> None:
    """
    Emit an async event from a synchronous SQLAlchemy listener.

    If an event loop is running (normal request flow), schedules the emission
    as a task. If no loop is running (e.g. during migrations or CLI), logs
    a debug message and skips emission.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop running (migrations, CLI, tests without async)
        logger.debug("No event loop — skipping event emission: %s", event_name)
        return

    loop.create_task(bus.emit(event_name, **kwargs))


def _emit_typed_async(bus: Any, event: Any) -> None:
    """
    Emit a typed event from a synchronous SQLAlchemy listener.

    Same loop-detection logic as ``_emit_async`` but for Pydantic events.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "No event loop — skipping typed event emission: %s",
            type(event).event_name,
        )
        return

    loop.create_task(bus.emit_typed(event))


def _get_tablename(instance: Any) -> str | None:
    """Extract the table name from a mapped instance."""
    try:
        mapper = sa_inspect(type(instance))
        return mapper.local_table.name  # type: ignore[union-attr]
    except Exception:
        return None


def _get_hub_id(instance: Any) -> Any:
    """Extract hub_id from an instance if available."""
    return getattr(instance, "hub_id", None)


def _get_instance_id(instance: Any) -> Any:
    """Extract the primary key from a mapped instance."""
    return getattr(instance, "id", None)


def setup_orm_events(bus: Any, base: type[DeclarativeBase] | None = None) -> None:
    """
    Register SQLAlchemy ORM event listeners that emit to the AsyncEventBus.

    Emits both typed (Pydantic) and legacy (string) events for backward compatibility.

    Args:
        bus: The AsyncEventBus instance to emit events to.
        base: The SQLAlchemy declarative base class. If None, listeners are
              registered on ``Mapper`` directly (catches all mapped classes).
    """
    import importlib

    _catalog = importlib.import_module("hotframe.signals.catalog")
    ModelPostDeleteEvent = _catalog.ModelPostDeleteEvent
    ModelPostSaveEvent = _catalog.ModelPostSaveEvent
    ModelPreDeleteEvent = _catalog.ModelPreDeleteEvent
    ModelPreSaveEvent = _catalog.ModelPreSaveEvent

    target = base if base is not None else Mapper

    # ------------------------------------------------------------------
    # before_insert — auto-set timestamps and hub_id + typed pre_save
    # ------------------------------------------------------------------
    @event.listens_for(target, "before_insert", propagate=True)
    def _before_insert(mapper: Mapper, connection: Any, instance: Any) -> None:
        now = datetime.now(UTC)

        if hasattr(instance, "created_at") and instance.created_at is None:
            instance.created_at = now

        if hasattr(instance, "updated_at"):
            instance.updated_at = now

        # Auto-set hub_id from session context if not already set
        if hasattr(instance, "hub_id") and instance.hub_id is None:
            session = Session.object_session(instance)
            if session is not None:
                ctx_hub_id = session.info.get("hub_id")
                if ctx_hub_id is not None:
                    instance.hub_id = ctx_hub_id

        tablename = _get_tablename(instance) or type(instance).__name__
        hub_id = _get_hub_id(instance)

        # Typed event
        _emit_typed_async(
            bus,
            ModelPreSaveEvent(
                model_name=tablename,
                instance_id=_get_instance_id(instance),
                created=True,
                hub_id=hub_id,
            ),
        )

        # Legacy event (backward compat)
        _emit_async(
            bus,
            "model.pre_save",
            sender=type(instance),
            instance=instance,
            created=True,
        )

    # ------------------------------------------------------------------
    # after_insert — typed post_save (created=True) + table event
    # ------------------------------------------------------------------
    @event.listens_for(target, "after_insert", propagate=True)
    def _after_insert(mapper: Mapper, connection: Any, instance: Any) -> None:
        tablename = _get_tablename(instance)
        hub_id = _get_hub_id(instance)
        model_name = tablename or type(instance).__name__

        # Typed event
        _emit_typed_async(
            bus,
            ModelPostSaveEvent(
                model_name=model_name,
                instance_id=_get_instance_id(instance),
                created=True,
                hub_id=hub_id,
            ),
        )

        # Legacy events (backward compat)
        _emit_async(
            bus,
            "model.post_save",
            sender=type(instance),
            instance=instance,
            created=True,
            hub_id=hub_id,
        )

        if tablename:
            _emit_async(
                bus,
                f"{tablename}.created",
                sender=type(instance),
                instance=instance,
                hub_id=hub_id,
            )

    # ------------------------------------------------------------------
    # before_update — auto-update updated_at + typed pre_save
    # ------------------------------------------------------------------
    @event.listens_for(target, "before_update", propagate=True)
    def _before_update(mapper: Mapper, connection: Any, instance: Any) -> None:
        if hasattr(instance, "updated_at"):
            instance.updated_at = datetime.now(UTC)

        tablename = _get_tablename(instance) or type(instance).__name__
        hub_id = _get_hub_id(instance)

        # Typed event
        _emit_typed_async(
            bus,
            ModelPreSaveEvent(
                model_name=tablename,
                instance_id=_get_instance_id(instance),
                created=False,
                hub_id=hub_id,
            ),
        )

        # Legacy event (backward compat)
        _emit_async(
            bus,
            "model.pre_save",
            sender=type(instance),
            instance=instance,
            created=False,
        )

    # ------------------------------------------------------------------
    # after_update — typed post_save (created=False) + table event
    # ------------------------------------------------------------------
    @event.listens_for(target, "after_update", propagate=True)
    def _after_update(mapper: Mapper, connection: Any, instance: Any) -> None:
        tablename = _get_tablename(instance)
        hub_id = _get_hub_id(instance)
        model_name = tablename or type(instance).__name__

        # Typed event
        _emit_typed_async(
            bus,
            ModelPostSaveEvent(
                model_name=model_name,
                instance_id=_get_instance_id(instance),
                created=False,
                hub_id=hub_id,
            ),
        )

        # Legacy events (backward compat)
        _emit_async(
            bus,
            "model.post_save",
            sender=type(instance),
            instance=instance,
            created=False,
            hub_id=hub_id,
        )

        if tablename:
            _emit_async(
                bus,
                f"{tablename}.updated",
                sender=type(instance),
                instance=instance,
                hub_id=hub_id,
            )

    # ------------------------------------------------------------------
    # after_delete — typed post_delete + table event
    # ------------------------------------------------------------------
    @event.listens_for(target, "after_delete", propagate=True)
    def _after_delete(mapper: Mapper, connection: Any, instance: Any) -> None:
        tablename = _get_tablename(instance)
        hub_id = _get_hub_id(instance)
        model_name = tablename or type(instance).__name__

        # Typed event — pre_delete (emitted on after_delete since SA has no before_delete)
        _emit_typed_async(
            bus,
            ModelPreDeleteEvent(
                model_name=model_name,
                instance_id=_get_instance_id(instance),
                hub_id=hub_id,
            ),
        )

        # Typed event — post_delete
        _emit_typed_async(
            bus,
            ModelPostDeleteEvent(
                model_name=model_name,
                instance_id=_get_instance_id(instance),
                hub_id=hub_id,
            ),
        )

        # Legacy events (backward compat)
        _emit_async(
            bus,
            "model.post_delete",
            sender=type(instance),
            instance=instance,
            hub_id=hub_id,
        )

        if tablename:
            _emit_async(
                bus,
                f"{tablename}.deleted",
                sender=type(instance),
                instance=instance,
                hub_id=hub_id,
            )

    logger.info("ORM event listeners registered on %s (typed + legacy)", target)
