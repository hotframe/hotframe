# SPDX-License-Identifier: Apache-2.0
"""
``LiveComponent`` — stateful component with a server-resident lifecycle.

A LiveComponent extends the existing :class:`hotframe.components.Component`
(a Pydantic model used today for prop validation) with three additions:

1. **Identity**: a ``cid`` string assigned by the runtime when the
   component is mounted. Used by the wire protocol to address the
   instance and by morphdom to locate the wrapper element in the DOM.
2. **Lifecycle hooks**: ``on_mount`` and ``on_unmount`` coroutines.
   ``on_mount`` runs after props are validated, before the first
   render. ``on_unmount`` runs when the WS closes or the client
   detaches the component.
3. **Event dispatch**: methods decorated with ``@event(name)`` are
   collected at subclass creation into a ``_events`` table. The
   runtime looks up handlers by wire name with O(1) dict access.

Mutable state lives as plain Pydantic fields. Setting ``self.x = …`` in
an event handler mutates the model instance; the runtime re-renders
the template against the post-mutation state. There is no manual
"dirty check" — every event triggers a render.

State should be *reconstructible from props + DB*. If the WS reconnects,
``on_mount`` runs again. Do not put live asyncio tasks, open file
handles, or unique IDs on ``self`` and expect them to survive a
reconnect.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, PrivateAttr

from hotframe.live.decorators import get_event_name

if TYPE_CHECKING:
    from hotframe.live.session import LiveSession


# Type alias for an event handler bound to ``self``.
EventHandler = Callable[..., Awaitable[Any]]


class LiveComponent(BaseModel):
    """Base class for stateful, server-rendered, reactive components.

    Subclass it and declare:

    - **Props** as Pydantic fields without defaults (or with defaults
      that are immutable per-instance — you are free to override them
      from the parent template).
    - **State** as Pydantic fields with sensible defaults. State
      mutations in event handlers are written straight to ``self``.
    - **Event handlers** as ``async def`` methods decorated with
      ``@event(name)``.
    - **Lifecycle hooks** ``on_mount`` and ``on_unmount`` (optional).

    Example::

        from hotframe.live import LiveComponent, event

        class TodoList(LiveComponent):
            user_id: int                # prop
            items: list[Todo] = []      # state
            filter: str = "all"         # state

            async def on_mount(self) -> None:
                self.items = await Todo.where(user_id=self.user_id).all()

            @event("toggle")
            async def toggle(self, todo_id: str) -> None:
                t = next(t for t in self.items if str(t.id) == todo_id)
                t.done = not t.done
                await t.save()
    """

    # Pydantic config: allow arbitrary attribute types (DB models often
    # do not have nested Pydantic schemas) and emit validators on
    # assignment so ``self.x = ...`` enforces the field type.
    model_config = ConfigDict(arbitrary_types_allowed=True, validate_assignment=True)

    # ------------------------------------------------------------------
    # Class-level event table — populated once per subclass.
    # ------------------------------------------------------------------
    # ``_events`` maps wire name -> unbound function. Lookup is O(1) at
    # dispatch time, no per-event introspection. We use a ClassVar so
    # Pydantic does not treat it as a field.
    _events: ClassVar[dict[str, EventHandler]] = {}

    # ------------------------------------------------------------------
    # Per-instance runtime state.
    # ------------------------------------------------------------------
    # PrivateAttr keeps these out of model_dump() and the wire format,
    # but available as plain attributes inside handlers.
    _cid: str = PrivateAttr(default="")
    _session: LiveSession | None = PrivateAttr(default=None)
    _component_name: str = PrivateAttr(default="")
    _last_html: str = PrivateAttr(default="")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Build the ``_events`` table by scanning decorated methods.

        Called once per subclass at class creation time. Walks the MRO
        in reverse so child classes can override parent handlers by
        re-declaring the same wire name.
        """
        super().__init_subclass__(**kwargs)

        events: dict[str, EventHandler] = {}
        # Iterate the MRO from the top down (object first, subclass last)
        # so child overrides win.
        for klass in reversed(cls.__mro__):
            if klass is object:
                continue
            for attr_name, attr in vars(klass).items():
                wire_name = get_event_name(attr)
                if wire_name is None:
                    continue
                if not inspect.iscoroutinefunction(attr):
                    raise TypeError(
                        f"{cls.__name__}.{attr_name} is decorated with "
                        f"@event({wire_name!r}) but is not an async function"
                    )
                events[wire_name] = attr
        cls._events = events

    # ------------------------------------------------------------------
    # Lifecycle hooks (override in subclasses).
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        """Run once when the runtime registers this instance.

        Override to load DB-backed state or open subscriptions. Default
        is a no-op so simple components don't need to implement it.
        """

    async def on_unmount(self) -> None:
        """Run once when the runtime drops this instance.

        Override to release resources (cancel tasks, close streams).
        Default is a no-op.
        """

    # ------------------------------------------------------------------
    # Identity / context (read-only from user code).
    # ------------------------------------------------------------------

    @property
    def cid(self) -> str:
        """The component instance id assigned by the runtime."""
        return self._cid

    @property
    def session(self) -> LiveSession | None:
        """The owning live session, or None during cold-load render."""
        return self._session

    @property
    def component_name(self) -> str:
        """The wire name (matches the directory name in components/)."""
        return self._component_name

    # ------------------------------------------------------------------
    # Helpers handlers can call.
    # ------------------------------------------------------------------

    async def navigate(self, url: str) -> None:
        """Trigger a full-page navigation on the client.

        The runtime serialises this as a ``nav`` envelope.
        """
        if self._session is None:
            raise RuntimeError("navigate() called outside a live session")
        await self._session.send_nav(url)

    async def toast(
        self,
        msg: str,
        level: str = "info",
    ) -> None:
        """Push a toast notification to the client.

        The level is one of ``info``, ``success``, ``warning``,
        ``error``. The framework ships no toast UI — projects render
        them however they like by listening for the ``hf:toast`` event
        emitted by ``live.js``.
        """
        if self._session is None:
            raise RuntimeError("toast() called outside a live session")
        await self._session.send_toast(msg, level=level)

    # ------------------------------------------------------------------
    # Internal: snapshot of mutable state for derived render context.
    # ------------------------------------------------------------------

    def render_context(self) -> dict[str, Any]:
        """Return the dict the template renders against.

        Default merges all model fields (props + state) plus any extras
        returned by :meth:`extra_context`. Override
        :meth:`extra_context` for derived values; override this method
        only if you need to reshape the entire context.
        """
        ctx = self.model_dump()
        extra = self.extra_context()
        if extra:
            ctx.update(extra)
        return ctx

    def extra_context(self) -> dict[str, Any]:
        """Return values derived from props/state.

        Override to expose computed properties or method results that
        the template should see alongside the raw fields. Default
        returns an empty dict.

        Example::

            def extra_context(self) -> dict:
                return {"open_count": sum(1 for t in self.items if not t.done)}
        """
        return {}
