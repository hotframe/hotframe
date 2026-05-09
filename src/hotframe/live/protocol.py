# SPDX-License-Identifier: Apache-2.0
"""
Live wire protocol — message envelopes exchanged over the WebSocket.

The ``/ws/_live`` endpoint speaks JSON. Each message is a flat dict with
a single-letter ``t`` (type) discriminator. Compact field names keep
payloads small; this is a hot path on every user interaction.

Client → Server:

- ``attach`` — register a component instance keyed by ``cid``.
- ``event``  — invoke an ``@event`` handler on the component.
- ``bind``   — update a single state field (input/textarea bidirectional bind).
- ``detach`` — release a component instance and free server memory.

Server → Client:

- ``patch`` — full HTML re-render of one component, applied with morphdom.
- ``nav``   — server-initiated navigation (full page).
- ``err``   — handler raised, payload carries a human message.
- ``toast`` — flash-style notification; client decides where to render it.

The envelopes are TypedDicts (rather than Pydantic models) so the WS
loop can dispatch on ``msg["t"]`` without paying validation overhead per
message. We trust our own client and reject unknown shapes with a
warning + drop. Only handler payloads (``msg["p"]``) are validated, by
the handler itself.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

# ---------------------------------------------------------------------------
# Client -> Server
# ---------------------------------------------------------------------------


class AttachMessage(TypedDict):
    """Register a new component instance under ``cid``.

    Sent on cold-load WS open and on reconnect for every
    ``[data-hf-cid]`` element still present in the DOM.
    """

    t: Literal["attach"]
    cid: str
    name: str
    props: dict[str, Any]


class EventMessage(TypedDict, total=False):
    """Invoke an ``@event`` handler.

    ``p`` is optional. For form submits it is typically a dict (the
    serialized FormData); for click events with a payload it is a
    string; for click events without payload it is absent.
    """

    t: Literal["event"]
    cid: str
    n: str
    p: Any


class BindMessage(TypedDict):
    """Update a single state field via ``data-bind``.

    Bind messages are debounced client-side (default 250 ms) so a fast
    typist does not flood the WS. The server stores the new value but
    does NOT re-render — re-render only happens on explicit events.
    """

    t: Literal["bind"]
    cid: str
    f: str
    v: Any


class DetachMessage(TypedDict):
    """Release a component instance.

    Triggered when the client navigates away inside the SPA shell or
    when an observer detects a ``[data-hf-cid]`` element leaves the
    DOM. The server runs ``on_unmount`` and drops the instance.
    """

    t: Literal["detach"]
    cid: str


ClientMessage = AttachMessage | EventMessage | BindMessage | DetachMessage


# ---------------------------------------------------------------------------
# Server -> Client
# ---------------------------------------------------------------------------


class PatchMessage(TypedDict):
    """Full HTML re-render of one component.

    The client locates ``[data-hf-cid="${cid}"]`` and applies the new
    HTML with morphdom. Focus, scroll position, and selection are
    preserved by morphdom by default.
    """

    t: Literal["patch"]
    cid: str
    html: str


class NavMessage(TypedDict):
    """Server-initiated navigation.

    Equivalent to ``window.location.href = url`` on the client.
    """

    t: Literal["nav"]
    url: str


class ErrMessage(TypedDict, total=False):
    """A handler raised; carries a human-readable message.

    ``code`` is optional and lets the client branch on classes of
    error (e.g. ``"not_found"``, ``"forbidden"``) without parsing the
    text.
    """

    t: Literal["err"]
    cid: str
    msg: str
    code: str


class ToastMessage(TypedDict):
    """Flash-style notification.

    The client decides how to render it; hotframe ships no opinionated
    UI for it, so projects can wire it to their own toast layer.
    """

    t: Literal["toast"]
    level: Literal["info", "success", "warning", "error"]
    msg: str


ServerMessage = PatchMessage | NavMessage | ErrMessage | ToastMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_patch(cid: str, html: str) -> PatchMessage:
    """Build a ``patch`` envelope. Centralised to keep the wire format
    in one place — handlers should not construct envelopes by hand."""
    return {"t": "patch", "cid": cid, "html": html}


def make_nav(url: str) -> NavMessage:
    return {"t": "nav", "url": url}


def make_err(cid: str, msg: str, code: str | None = None) -> ErrMessage:
    out: ErrMessage = {"t": "err", "cid": cid, "msg": msg}
    if code:
        out["code"] = code
    return out


def make_toast(
    msg: str, level: Literal["info", "success", "warning", "error"] = "info"
) -> ToastMessage:
    return {"t": "toast", "level": level, "msg": msg}
