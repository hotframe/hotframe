# SPDX-License-Identifier: Apache-2.0
"""Event name constants emitted by :class:`AuthenticatedClient`."""

from __future__ import annotations

EVENT_REQUEST_STARTED = "http.request.started"
"""Emitted just before an HTTP request is dispatched.

Payload keys: ``client_name``, ``method``, ``url``.
"""

EVENT_REQUEST_COMPLETED = "http.request.completed"
"""Emitted after an HTTP response has been received.

Payload keys: ``client_name``, ``method``, ``url``, ``status``,
``duration_ms``.
"""

EVENT_REQUEST_FAILED = "http.request.failed"
"""Emitted when a request raises before a response is produced.

Payload keys: ``client_name``, ``method``, ``url``, ``error`` (string).
"""

__all__ = [
    "EVENT_REQUEST_COMPLETED",
    "EVENT_REQUEST_FAILED",
    "EVENT_REQUEST_STARTED",
]
