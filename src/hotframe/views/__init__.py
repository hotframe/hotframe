"""
views — view helpers, SSE responses, and real-time broadcasting.

``BroadcastHub`` provides topic-based fan-out for real-time SSE/WS
broadcasting to connected clients.

Key exports::

    from hotframe.views.broadcast import BroadcastHub, broadcast_router, get_broadcast_hub
"""
