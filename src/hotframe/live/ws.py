# SPDX-License-Identifier: Apache-2.0
"""
WebSocket endpoint mounted at ``/ws/_live``.

The endpoint is intentionally thin — accept the connection, pull the
session id off the request, hand control to a :class:`LiveSession`,
and clean up on disconnect. All real work happens in the session.

Authentication: today we trust the signed session cookie that the WS
upgrade handshake carries. There is no per-event auth check; if the
WS opens, the session has whatever permissions the cookie grants.
Components that need to gate access on a per-event basis must do that
themselves inside ``on_mount`` (raise to abort) or each handler.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from hotframe.live.runtime import get_runtime

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


live_router = APIRouter()


# ---------------------------------------------------------------------------
# Session id resolution
# ---------------------------------------------------------------------------


def _resolve_session_id(ws: WebSocket) -> str:
    """Pick a stable session id off the WS handshake.

    Order of preference:

    1. Starlette's ``ws.session`` (populated by SessionMiddleware) — has
       a unique key per signed cookie. We hash it via ``id()`` only as
       a fallback when the dict is non-empty but key-less.
    2. The raw ``session`` cookie value, if present.
    3. The remote address + port (IPv4/v6 tuple).
    4. ``id(ws)`` as a last resort — guarantees uniqueness within a
       process but is not stable across reconnects.

    Stability across reconnects matters because we use the id as the
    key in :attr:`LiveRuntime.sessions`. A reconnect with the same id
    closes the previous session cleanly.
    """
    try:
        session = ws.session  # type: ignore[attr-defined]
    except (AssertionError, AttributeError):
        session = None

    if session:
        for k in ("user_id", "session_id", "sid"):
            v = session.get(k)
            if v:
                return str(v)

    cookie = ws.cookies.get("session")
    if cookie:
        return cookie

    if ws.client is not None:
        return f"{ws.client.host}:{ws.client.port}"

    return f"anon:{id(ws)}"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@live_router.websocket("/ws/_live")
async def live_endpoint(ws: WebSocket) -> None:
    """Handle a single live session for the duration of the WS."""
    await ws.accept()

    try:
        runtime = get_runtime(ws.app)
    except RuntimeError:
        logger.error("WS /ws/_live opened but LiveRuntime not initialised; closing")
        await ws.close(code=1011)  # internal error
        return

    session_id = _resolve_session_id(ws)
    session = await runtime.open_session(session_id, ws)
    logger.info("Live WS opened: session=%s", session_id)

    try:
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict):
                logger.warning("Live WS %s: dropped non-dict frame", session_id)
                continue
            await session.handle_message(msg)  # type: ignore[arg-type]
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Live WS %s: receive loop crashed", session_id)
    finally:
        await runtime.close_session(session_id)
        logger.info("Live WS closed: session=%s", session_id)
