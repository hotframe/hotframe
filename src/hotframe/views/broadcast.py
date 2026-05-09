"""
Real-time broadcasting via SSE (Server-Sent Events).

Bridges the hotframe event system to browser clients. When a module
publishes data to a topic, all connected SSE clients subscribed to
that topic receive the update in real-time.

Architecture::

    Module code                   Browser clients
        |                         |            |
    broadcast("todos", html)  SSE /stream/   SSE /stream/
        |                     todos          todos
        v                         |            |
    BroadcastHub.publish()        v            v
        |                    queue_A       queue_B
        +---> fan-out -----> event         event

The BroadcastHub is process-local. For multi-container deployments,
combine with ``PgNotifyBridge`` from ``hotframe.orm.listeners`` to
propagate events across containers via PostgreSQL LISTEN/NOTIFY.

Usage from a module view::

    from hotframe.views.broadcast import get_broadcast_hub

    async def create_todo(request, db, user, hub_id):
        todo = await create(db, request.form)
        # Broadcast to all clients watching "todos"
        hub = get_broadcast_hub(request)
        await hub.publish("todos", rendered_item)
        return {"todo": todo}
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect

from hotframe.auth.current_user import CurrentUser

logger = logging.getLogger(__name__)


class BroadcastHub:
    """Process-local topic-based fan-out for SSE broadcasting.

    Each SSE connection creates a queue via ``subscribe()``. When
    ``publish()`` is called, the message is placed in every active
    queue for that topic. Disconnected clients are cleaned up
    automatically when their queue is garbage-collected.

    Thread-safety: uses asyncio primitives only — safe for the
    single event loop but not for multi-threaded access. This is
    fine because FastAPI runs on a single event loop.
    """

    def __init__(self) -> None:
        # topic -> set of asyncio.Queue (one per SSE connection)
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    async def subscribe(self, topic: str) -> asyncio.Queue:
        """Create a new queue for receiving messages on ``topic``.

        Returns an ``asyncio.Queue`` that the caller should ``await
        queue.get()`` in a loop. Call ``unsubscribe()`` when done.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers[topic].add(queue)
        logger.debug(
            "SSE subscribe topic=%s clients=%d",
            topic,
            len(self._subscribers[topic]),
        )
        return queue

    async def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        """Remove a queue from a topic's subscriber set."""
        self._subscribers[topic].discard(queue)
        if not self._subscribers[topic]:
            del self._subscribers[topic]
        logger.debug("SSE unsubscribe topic=%s", topic)

    async def publish(self, topic: str, data: str) -> int:
        """Send ``data`` to all subscribers of ``topic``.

        Returns the number of clients that received the message.
        Queues that are full (client not consuming fast enough) are
        skipped with a warning — we never block the publisher.
        """
        subscribers = self._subscribers.get(topic, set())
        if not subscribers:
            return 0

        delivered = 0
        stale: list[asyncio.Queue] = []
        for queue in subscribers:
            try:
                queue.put_nowait(data)
                delivered += 1
            except asyncio.QueueFull:
                logger.warning(
                    "SSE queue full for topic=%s, dropping message",
                    topic,
                )
                stale.append(queue)

        # Remove stale queues (client not consuming)
        for q in stale:
            subscribers.discard(q)

        return delivered

    def topic_count(self) -> int:
        """Number of active topics with at least one subscriber."""
        return len(self._subscribers)

    def subscriber_count(self, topic: str) -> int:
        """Number of active subscribers for a topic."""
        return len(self._subscribers.get(topic, set()))


# --- Singleton access ---


def get_broadcast_hub(request: Request) -> BroadcastHub:
    """Get the BroadcastHub from the app state."""
    return request.app.state.broadcast_hub


# --- SSE endpoint ---

broadcast_router = APIRouter(tags=["broadcast"])


@broadcast_router.get("/stream/{topic:path}")
async def stream_topic(
    request: Request,
    topic: str,
    user: CurrentUser,
) -> Response:
    """SSE endpoint that streams messages for a topic to the browser.

    Each message is an opaque payload (typically an HTML fragment or
    JSON blob) the consumer interprets however it wants. This endpoint
    is independent of the live runtime; live updates flow over the
    dedicated ``/ws/_live`` WebSocket instead.

    Authentication: requires an active session. Anonymous callers receive 401.
    """
    from sse_starlette.sse import EventSourceResponse

    hub = get_broadcast_hub(request)
    queue = await hub.subscribe(topic)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # Wait for next message with timeout (for disconnect check)
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {"event": "message", "data": data}
                except TimeoutError:
                    # No message — just loop to check disconnect
                    continue
        finally:
            await hub.unsubscribe(topic, queue)

    return EventSourceResponse(event_generator(), ping=15)


@broadcast_router.get("/stream/_mux")
async def stream_multiplexed(
    request: Request,
    user: CurrentUser,
    topics: str = "",
) -> Response:
    """Multiplexed SSE endpoint — one connection for multiple topics.

    The client sends ``topics=a,b,c`` as a query parameter. Messages
    are sent with the topic name as the SSE event type, so the client
    can route them to the correct handler.

    This reduces the number of open connections from N (one per topic)
    to 1 per browser tab.

    Authentication: requires an active session. Anonymous callers receive 401.
    Validation: at least one non-empty topic is required; otherwise the
    endpoint responds 400 **before** opening any queue or streaming body,
    so clients get an immediate error instead of a hung connection.
    """
    from sse_starlette.sse import EventSourceResponse

    topic_list = [t.strip() for t in topics.split(",") if t.strip()] if topics else []
    if not topic_list:
        raise HTTPException(
            status_code=400,
            detail="At least one topic required",
        )

    hub = get_broadcast_hub(request)
    queues: list[tuple[str, asyncio.Queue]] = []
    for topic in topic_list:
        queue = await hub.subscribe(topic)
        queues.append((topic, queue))

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                # Wait on ALL queues concurrently
                done, pending = await asyncio.wait(
                    [asyncio.create_task(_wait_queue(t, q)) for t, q in queues],
                    timeout=30.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    result = task.result()
                    if result:
                        topic_name, data = result
                        yield {"event": topic_name, "data": data}
                # Cancel pending tasks
                for task in pending:
                    task.cancel()
        finally:
            for topic, queue in queues:
                await hub.unsubscribe(topic, queue)

    return EventSourceResponse(event_generator(), ping=15)


async def _wait_queue(topic: str, queue: asyncio.Queue) -> tuple[str, str] | None:
    """Wait for a message on a single queue, returning (topic, data)."""
    try:
        data = await queue.get()
        return (topic, data)
    except asyncio.CancelledError:
        return None


@broadcast_router.websocket("/ws/stream/{topic:path}")
async def ws_broadcast_handler(websocket: WebSocket, topic: str) -> None:
    """WebSocket endpoint that streams broadcast messages for a topic.

    Alternative to the SSE endpoint for environments where SSE is
    unreliable (some corporate proxies, mobile networks).

    Usage::

        const ws = new WebSocket("wss://hub.example.com/ws/stream/todos");
        ws.onmessage = (e) => {
            // e.data is an HTML fragment — inject into DOM
            document.getElementById("container").innerHTML += e.data;
        };

    Authentication: since ``BaseHTTPMiddleware`` subclasses (including
    ``SessionMiddleware``) pass WebSocket scopes through untouched, the
    session is parsed manually from the upgrade request cookies via
    ``get_session_data``. Anonymous upgrades are closed with code 4401
    (policy violation, application-defined) before ``accept()`` is called.
    """
    from hotframe.auth.auth import SESSION_USER_KEY
    from hotframe.auth.session_helpers import get_session_data

    session = get_session_data(websocket)
    if not session.get(SESSION_USER_KEY):
        # Reject with a close code that maps to "unauthorized" in the
        # 4000-4999 application range. We close before accept() so the
        # handshake fails with HTTP 403 on the client side.
        await websocket.close(code=4401)
        return

    await websocket.accept()
    # WebSocket has .app like Request, so the structural lookup works at
    # runtime — the typed signature only documents the common case.
    hub = get_broadcast_hub(websocket)  # type: ignore[arg-type]
    queue = await hub.subscribe(topic)

    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_text(data)
            except TimeoutError:
                # Send keepalive ping
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unsubscribe(topic, queue)


@broadcast_router.post("/stream/{topic:path}")
async def publish_to_topic(request: Request, topic: str) -> Response:
    """Publish data to a broadcast topic from the browser.

    The request body is the raw data to broadcast (typically HTML fragments).
    """
    hub = get_broadcast_hub(request)
    body = await request.body()
    data = body.decode("utf-8")
    count = await hub.publish(topic, data)
    return JSONResponse({"published": True, "subscribers": count})
