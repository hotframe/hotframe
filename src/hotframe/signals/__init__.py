"""
signals — async event bus, hook registry, and typed event contracts.

``AsyncEventBus`` dispatches named or typed events to registered async
handlers with per-hub isolation. ``HookRegistry`` manages filter/action
hooks (WordPress-style) with priority ordering and LIFO removal.
``BaseEvent`` is the Pydantic base class for all strongly-typed events;
``@register_event`` marks an event class for schema validation.

Key exports::

    from hotframe.signals.dispatcher import AsyncEventBus
    from hotframe.signals.hooks import HookRegistry
    from hotframe.signals.types import BaseEvent, register_event

Usage::

    bus = AsyncEventBus()
    bus.on("product.created", my_handler)
    await bus.emit("product.created", product_id=pid)
"""
