# SPDX-License-Identifier: Apache-2.0
"""
``hotframe.live`` — stateful, server-rendered, WebSocket-driven components.

Quick start::

    from hotframe.live import LiveComponent, event

    class TodoList(LiveComponent):
        user_id: int             # prop
        items: list = []         # state

        async def on_mount(self) -> None:
            self.items = await Todo.where(user_id=self.user_id).all()

        @event("toggle")
        async def toggle(self, todo_id: str) -> None:
            t = next(t for t in self.items if str(t.id) == todo_id)
            t.done = not t.done
            await t.save()

The component lives in ``modules/<id>/components/todo_list/`` next to
its ``template.html``. Cold-load with ``{% live "todo_list"
user_id=user.id %}``. The runtime takes care of the WS handshake and
the patch loop on every event.

Key collaborators:

- :class:`LiveComponent`  — base class; subclass to define a component.
- :func:`event`           — decorator marking a method as an event handler.
- :class:`LiveSession`    — per-WS runtime; owns the dict of instances.
- :class:`LiveRuntime`    — per-app singleton; owns the sessions.
- ``{% live %}`` tag      — cold-load entry from a Jinja template.
- ``/ws/_live`` endpoint  — single WebSocket per page, JSON envelopes.
- ``live.js`` (static)    — client-side: WS, morphdom, event capture.
"""

from hotframe.live.base import LiveComponent
from hotframe.live.decorators import event, get_event_name
from hotframe.live.runtime import LiveRuntime, get_runtime
from hotframe.live.session import LiveSession
from hotframe.live.ws import live_router

__all__ = [
    "LiveComponent",
    "LiveRuntime",
    "LiveSession",
    "event",
    "get_event_name",
    "get_runtime",
    "live_router",
]
