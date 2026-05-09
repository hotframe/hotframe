# SPDX-License-Identifier: Apache-2.0
"""
Session helpers for non-HTTP scopes (WebSocket auth).

Starlette's ``SessionMiddleware`` populates ``scope["session"]`` (exposed via
``request.session``) for HTTP requests but, like every other middleware
implemented as a pure ASGI app, it does NOT process the WebSocket upgrade
itself — it simply runs and stores the session on the scope. That works for
HTTP but not when a WebSocket handler wants to authenticate the client
*before* calling ``websocket.accept()``: at that point we have the upgrade
request cookies but no session is decoded yet because Starlette's session
machinery is wired into the HTTP send-cycle.

This module exposes a tiny helper that decodes a Starlette-format session
cookie from any object with ``.cookies`` (Request, WebSocket, HTTPConnection).
"""

from __future__ import annotations

import json
from base64 import b64decode
from typing import Any, Protocol

from itsdangerous import BadSignature, TimestampSigner


class _HasCookies(Protocol):
    @property
    def cookies(self) -> dict[str, str]: ...


def get_session_data(scope_or_request: _HasCookies) -> dict[str, Any]:
    """Decode a Starlette session cookie from any scope (Request, WebSocket).

    Returns an empty dict when the cookie is absent, malformed or expired.
    Mirrors the on-the-wire format of ``starlette.middleware.sessions``:
    ``timestamp_signer.sign(b64encode(json.dumps(session)))``.
    """
    from hotframe.config.settings import get_settings

    settings = get_settings()
    signer = TimestampSigner(settings.SECRET_KEY)

    raw = scope_or_request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not raw:
        return {}
    try:
        unsigned = signer.unsign(raw.encode("utf-8"), max_age=settings.SESSION_MAX_AGE)
        data = json.loads(b64decode(unsigned))
        if isinstance(data, dict):
            return data
    except (BadSignature, ValueError, json.JSONDecodeError):
        pass
    return {}
