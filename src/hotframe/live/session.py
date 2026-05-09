# SPDX-License-Identifier: Apache-2.0
"""
``LiveSession`` — one WebSocket = one session = many component instances.

The session owns the WS, the dict of live component instances keyed by
``cid``, and a per-cid asyncio Lock so events on the same component
serialise (handlers can mutate ``self`` without racing). Different
``cid`` events run concurrently — that is how a page with many
components stays responsive.

The session is the only writer to the WS. Components never call
``ws.send_*`` directly — they go through helpers (``send_patch``,
``send_nav``, ``send_toast``) which produce protocol-compliant
envelopes. This keeps the wire format in one place.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from hotframe.live.diff import render_component_inner
from hotframe.live.protocol import (
    AttachMessage,
    BindMessage,
    ClientMessage,
    DetachMessage,
    EventMessage,
    make_err,
    make_nav,
    make_patch,
    make_toast,
)

if TYPE_CHECKING:
    from fastapi import WebSocket

    from hotframe.live.base import LiveComponent
    from hotframe.live.runtime import LiveRuntime

logger = logging.getLogger(__name__)


# Reserved wire name for ``data-bind`` updates. Using ``__bind__`` (not
# a regular event name) keeps the protocol unambiguous and avoids any
# collision with user-defined events. See ``live.js`` for the matching
# emission.
BIND_EVENT_NAME = "__bind__"


class LiveSession:
    """Per-WS aggregate of live component instances.

    The session lives as long as the WebSocket. When the WS closes
    (clean or unclean) the runtime calls :meth:`shutdown` to run
    ``on_unmount`` on every instance and drop them.
    """

    def __init__(
        self,
        session_id: str,
        ws: WebSocket,
        runtime: LiveRuntime,
    ) -> None:
        self.id = session_id
        self.ws = ws
        self.runtime = runtime
        # cid -> instance. Insertion order preserved (Python dict 3.7+).
        self.components: dict[str, LiveComponent] = {}
        # cid -> Lock. Acquired around every event/bind/render to keep
        # mutations on the same instance ordered.
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    # ------------------------------------------------------------------
    # Inbound dispatch
    # ------------------------------------------------------------------

    async def handle_message(self, msg: ClientMessage) -> None:
        """Dispatch a single client envelope.

        Unknown types are logged and dropped (defence-in-depth — our
        own client should never send them). Handler errors are caught
        and reported as ``err`` envelopes to keep the WS alive.
        """
        try:
            t = msg.get("t")
        except AttributeError:
            logger.warning("LiveSession %s: malformed message %r", self.id, msg)
            return

        try:
            if t == "attach":
                await self._attach(msg)  # type: ignore[arg-type]
            elif t == "event":
                await self._event(msg)  # type: ignore[arg-type]
            elif t == "bind":
                await self._bind(msg)  # type: ignore[arg-type]
            elif t == "detach":
                await self._detach(msg)  # type: ignore[arg-type]
            else:
                logger.warning("LiveSession %s: unknown message type %r", self.id, t)
        except Exception:
            cid = msg.get("cid") if isinstance(msg, dict) else None
            logger.exception("LiveSession %s: handler crashed for %r (cid=%s)", self.id, t, cid)
            if cid:
                await self._send(make_err(cid, "Internal error", code="internal"))

    # ------------------------------------------------------------------
    # Individual handlers
    # ------------------------------------------------------------------

    async def _attach(self, msg: AttachMessage) -> None:
        """Instantiate a component, run on_mount, render, and patch."""
        cid = msg["cid"]
        name = msg["name"]
        props = msg.get("props") or {}

        registry = self.runtime.registry
        entry = registry.get(name)
        if entry is None or not getattr(entry, "is_live", False):
            await self._send(make_err(cid, f"Unknown live component {name!r}", code="not_found"))
            return

        cls = entry.props_cls
        if cls is None:
            await self._send(make_err(cid, f"Component {name!r} has no class", code="no_class"))
            return

        try:
            instance = cls(**props)
        except ValidationError as exc:
            logger.warning("attach: invalid props for %r: %s", name, exc)
            await self._send(make_err(cid, "Invalid props", code="props"))
            return

        # Stamp identity. PrivateAttr writes need the underscore form.
        instance._cid = cid
        instance._session = self
        instance._component_name = name
        self.components[cid] = instance
        self._locks[cid] = asyncio.Lock()

        async with self._locks[cid]:
            try:
                await instance.on_mount()
            except Exception:
                logger.exception("on_mount failed for %r (cid=%s)", name, cid)
                await self._send(make_err(cid, "Mount failed", code="mount"))
                return

            await self._render_and_send(instance)

    async def _event(self, msg: EventMessage) -> None:
        """Look up an ``@event`` handler, invoke it, re-render."""
        cid = msg["cid"]
        instance = self.components.get(cid)
        if instance is None:
            # Component was detached (or never attached). Tell the
            # client; they may need to refresh.
            await self._send(make_err(cid, "Component not attached", code="not_attached"))
            return

        wire_name = msg.get("n")
        if not wire_name:
            await self._send(make_err(cid, "Missing event name", code="protocol"))
            return

        handler = instance.__class__._events.get(wire_name)
        if handler is None:
            await self._send(make_err(cid, f"Unknown event {wire_name!r}", code="not_found"))
            return

        payload = msg.get("p")

        async with self._locks[cid]:
            try:
                await _invoke_handler(handler, instance, payload)
            except Exception as exc:
                logger.exception(
                    "Event %r raised on %s (cid=%s)",
                    wire_name,
                    instance.__class__.__name__,
                    cid,
                )
                await self._send(make_err(cid, str(exc) or "Handler failed", code="handler"))
                return

            await self._render_and_send(instance)

    async def _bind(self, msg: BindMessage) -> None:
        """Update one state field, no re-render.

        Bind is a high-frequency, fire-and-forget update. Re-rendering
        on every keystroke would saturate the WS and fight the user's
        IME / autocomplete. The next user-initiated event triggers a
        render with the updated state.
        """
        cid = msg["cid"]
        instance = self.components.get(cid)
        if instance is None:
            return

        field = msg.get("f")
        value = msg.get("v")
        if not field:
            return

        # Validate via Pydantic's assignment validator (model_config.validate_assignment=True).
        async with self._locks[cid]:
            try:
                setattr(instance, field, value)
            except ValidationError:
                logger.warning(
                    "bind: invalid value for %s.%s (cid=%s)",
                    instance.__class__.__name__,
                    field,
                    cid,
                )

    async def _detach(self, msg: DetachMessage) -> None:
        """Run on_unmount and drop the instance."""
        cid = msg["cid"]
        instance = self.components.pop(cid, None)
        self._locks.pop(cid, None)
        if instance is None:
            return
        try:
            await instance.on_unmount()
        except Exception:
            logger.exception("on_unmount failed for %s (cid=%s)", instance.__class__.__name__, cid)

    # ------------------------------------------------------------------
    # Outbound helpers (called by handlers / runtime).
    # ------------------------------------------------------------------

    async def _render_and_send(self, instance: LiveComponent) -> None:
        """Render the instance and push a ``patch`` envelope."""
        registry = self.runtime.registry
        entry = registry.get(instance.component_name)
        if entry is None:
            return
        html = render_component_inner(self.runtime.env, entry, instance)
        instance._last_html = html
        await self._send(make_patch(instance.cid, html))

    async def send_nav(self, url: str) -> None:
        await self._send(make_nav(url))

    async def send_toast(self, msg: str, *, level: str = "info") -> None:
        # ``level`` is checked against the protocol literal at the
        # protocol boundary. Pass through here; bad values surface as a
        # validation failure on the client side, never a server crash.
        await self._send(make_toast(msg, level=level))  # type: ignore[arg-type]

    async def _send(self, envelope: Any) -> None:
        if self._closed:
            return
        try:
            await self.ws.send_json(envelope)
        except Exception:
            logger.warning("LiveSession %s: send failed; closing", self.id)
            self._closed = True

    # ------------------------------------------------------------------
    # Lifecycle.
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Run ``on_unmount`` on every instance and clear state.

        Idempotent — safe to call once on WebSocketDisconnect and again
        from a finally block.
        """
        if self._closed:
            return
        self._closed = True
        for cid, instance in list(self.components.items()):
            try:
                await instance.on_unmount()
            except Exception:
                logger.exception(
                    "shutdown: on_unmount failed for %s (cid=%s)",
                    instance.__class__.__name__,
                    cid,
                )
        self.components.clear()
        self._locks.clear()


# ---------------------------------------------------------------------------
# Handler invocation
# ---------------------------------------------------------------------------


async def _invoke_handler(
    handler: Any,
    instance: LiveComponent,
    payload: Any,
) -> None:
    """Invoke ``handler(instance, payload)`` adapting the call shape.

    Three call shapes are supported, in order of preference:

    1. Handler takes no extra args — ``await handler(self)``.
    2. Handler takes one positional arg — ``await handler(self, payload)``.
       If ``payload`` is a dict, it's also unpacked into kwargs as a
       fallback (so ``async def add(self, new_text: str)`` works when
       the form sends ``{"new_text": "..."}``).
    3. Handler takes a dict payload — ``await handler(self, **payload)``.

    The point: keep the ergonomics close to a normal Python method
    while still letting the wire protocol stay flat (``"event"`` +
    optional ``"p"``).
    """
    import inspect

    sig = inspect.signature(handler)
    # Strip self. It's the first parameter on an unbound method.
    params = list(sig.parameters.values())[1:]

    # No extra args — ignore payload.
    if not params:
        await handler(instance)
        return

    if isinstance(payload, dict):
        # Try kwargs match first; if the handler has a single positional
        # ``payload`` arg, pass the dict whole.
        if len(params) == 1 and params[0].kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        ):
            # If the param name matches a key, unpack; else pass the dict.
            if params[0].name in payload:
                await handler(instance, **payload)
            else:
                # Unpack dict into kwargs only if the handler params
                # exactly match keys; otherwise pass as a single arg.
                try:
                    await handler(instance, **payload)
                    return
                except TypeError:
                    await handler(instance, payload)
            return
        # Multiple params — unpack the dict.
        await handler(instance, **payload)
        return

    # Scalar payload (str/int/None) — single positional arg.
    if payload is None:
        # Handler may declare a default; try without arg first.
        try:
            await handler(instance)
            return
        except TypeError:
            pass
    await handler(instance, payload)
