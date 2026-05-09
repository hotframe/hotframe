# SPDX-License-Identifier: Apache-2.0
"""
``LiveRuntime`` — application-scoped singleton wired into ``app.state.live``.

The runtime is the bridge between the WS endpoint and the
:class:`hotframe.components.ComponentRegistry`. It owns every active
:class:`LiveSession` keyed by a stable session id (typically the
signed-cookie session id, but any opaque string works).

There is exactly one LiveRuntime per FastAPI app; the bootstrap
constructs it during the lifespan startup phase, after the registry
and Jinja2 environment are ready.

The runtime does not interpret messages itself — that is the session's
job. It only:

- Creates / destroys sessions on WS open / close.
- Holds a reference to the registry and the Jinja2 environment so
  every session can find them without a circular import.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from hotframe.live.session import LiveSession

if TYPE_CHECKING:
    from fastapi import WebSocket
    from jinja2 import Environment

    from hotframe.components.registry import ComponentRegistry

logger = logging.getLogger(__name__)


class LiveRuntime:
    """Per-app singleton coordinating live sessions."""

    def __init__(
        self,
        registry: ComponentRegistry,
        env: Environment,
    ) -> None:
        self.registry = registry
        self.env = env
        self.sessions: dict[str, LiveSession] = {}

    async def open_session(self, session_id: str, ws: WebSocket) -> LiveSession:
        """Create a session for ``ws`` and register it.

        If a session with the same id already exists (for example, a
        client reconnected before the previous WS noticed it was
        closed), the old one is shut down first.
        """
        existing = self.sessions.pop(session_id, None)
        if existing is not None:
            logger.info("LiveRuntime: replacing stale session %s", session_id)
            await existing.shutdown()

        session = LiveSession(session_id, ws, self)
        self.sessions[session_id] = session
        return session

    async def close_session(self, session_id: str) -> None:
        """Drop the session and run unmount on every component."""
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        await session.shutdown()

    async def shutdown(self) -> None:
        """Tear down every session — called from the lifespan shutdown."""
        for session_id in list(self.sessions.keys()):
            await self.close_session(session_id)


def get_runtime(app) -> LiveRuntime:
    """Return the runtime stored on ``app.state.live`` or raise.

    Helper used by the WS endpoint and JinjaX extension.
    """
    runtime: LiveRuntime | None = getattr(app.state, "live", None)
    if runtime is None:
        raise RuntimeError(
            "LiveRuntime not initialised. The bootstrap should set app.state.live during startup."
        )
    return runtime
