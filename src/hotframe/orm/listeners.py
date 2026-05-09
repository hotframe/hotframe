"""
PostgreSQL LISTEN/NOTIFY bridge.

Connects to PostgreSQL via asyncpg, listens on specified channels, and
re-emits incoming notifications as events on the AsyncEventBus under
the ``pg.{channel}`` namespace.

Usage::

    bridge = PgNotifyBridge()
    await bridge.start(dsn, bus, channels=["module_sync", "cache_invalidate"])
    # ...
    await bridge.stop()

From application code, send a notification::

    PgNotifyBridge.notify(session, "module_sync", {"module_id": "inventory", "action": "installed"})
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

try:
    import asyncpg  # type: ignore[import-not-found]
except ImportError:
    asyncpg = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class PgNotifyBridge:
    """
    Bridges PostgreSQL LISTEN/NOTIFY to the AsyncEventBus.

    Each NOTIFY on channel ``X`` with JSON payload is emitted as
    ``pg.X`` on the bus, with the parsed payload spread as keyword arguments.
    """

    def __init__(self) -> None:
        self._connection: asyncpg.Connection | None = None
        self._bus: Any | None = None
        self._channels: list[str] = []

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and not self._connection.is_closed()

    async def start(
        self,
        dsn: str,
        bus: Any,
        channels: list[str],
    ) -> None:
        """
        Connect to PostgreSQL and start listening on the given channels.

        Args:
            dsn: PostgreSQL connection string (e.g. ``postgresql://user:pass@host/db``).
            bus: The AsyncEventBus to emit events to.
            channels: List of channel names to LISTEN on.
        """
        if asyncpg is None:
            raise ImportError(
                "asyncpg is required for PgNotifyBridge. Install it with: pip install asyncpg"
            )

        self._bus = bus
        self._channels = channels

        self._connection = await asyncpg.connect(dsn)
        logger.info("PgNotifyBridge connected to PostgreSQL")

        for channel in channels:
            await self._connection.add_listener(channel, self._handle)
            logger.info("Listening on pg channel: %s", channel)

    async def stop(self) -> None:
        """Disconnect from PostgreSQL and remove all listeners."""
        if self._connection is not None and not self._connection.is_closed():
            for channel in self._channels:
                await self._connection.remove_listener(channel, self._handle)
            await self._connection.close()
            logger.info("PgNotifyBridge disconnected")

        self._connection = None
        self._bus = None
        self._channels = []

    def _handle(
        self,
        connection: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        """
        Handle an incoming NOTIFY.

        Parses the payload as JSON and emits ``pg.{channel}`` on the bus.
        Non-JSON payloads are wrapped as ``{"raw": payload}``.
        """
        if self._bus is None:
            return

        try:
            data = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            data = {"raw": payload}

        if not isinstance(data, dict):
            data = {"value": data}

        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No event loop — cannot emit pg.%s", channel)
            return

        loop.create_task(self._bus.emit(f"pg.{channel}", sender=self, **data))

    @staticmethod
    async def notify(session: AsyncSession, channel: str, payload: Any = None) -> None:
        """
        Send a NOTIFY from application code using a SQLAlchemy async session.

        Args:
            session: An active SQLAlchemy AsyncSession.
            channel: The PostgreSQL channel name.
            payload: Data to send (will be JSON-serialized). Max 8000 bytes.
        """
        from sqlalchemy import text

        json_payload = json.dumps(payload, default=str) if payload is not None else ""

        if len(json_payload.encode("utf-8")) > 8000:
            raise ValueError(
                f"NOTIFY payload exceeds PostgreSQL 8000-byte limit "
                f"({len(json_payload.encode('utf-8'))} bytes)"
            )

        await session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": channel, "payload": json_payload},
        )


# Convenience alias used by legacy import paths
setup_pg_notify = PgNotifyBridge
