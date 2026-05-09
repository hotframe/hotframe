# SPDX-License-Identifier: Apache-2.0
"""
Robust session middleware wrapper.

Wraps ``starlette.middleware.sessions.SessionMiddleware`` to recover gracefully
from session cookies that the upstream middleware cannot decode. Starlette's
implementation calls ``base64.b64decode(cookie)`` and ``json.loads(...)`` on
the result without catching ``UnicodeDecodeError`` or ``binascii.Error``, so
a cookie left over from a prior framework version (zlib-compressed payloads,
custom serialisers, anything non-base64-json) propagates as a 500.

This wrapper intercepts those errors at request time, drops the bad cookie
on the next response, and lets the request continue with an empty session —
the exact behaviour the user expects when their cookie store is stale.
"""

from __future__ import annotations

from typing import Any

from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RobustSessionMiddleware(SessionMiddleware):
    """Drop-in replacement for Starlette's ``SessionMiddleware``.

    Behaviour matches the upstream class for all valid cookies. When the
    cookie cannot be decoded for any reason — bad signature, bad base64,
    non-utf8 bytes, malformed JSON — the request scope is given an empty
    session dict and a ``Set-Cookie`` header is emitted to clear the bad
    cookie on the client.
    """

    def __init__(self, app: ASGIApp, **kwargs: Any) -> None:
        super().__init__(app, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Try the upstream middleware first. If it raises while decoding the
        # cookie (UnicodeDecodeError, binascii.Error, ValueError, etc.), fall
        # back to a clean session. The ASGI scope mutations that Starlette's
        # SessionMiddleware does are all on a copy, so we can safely retry.
        try:
            await super().__call__(scope, receive, send)
            return
        except (UnicodeDecodeError, ValueError, OSError) as exc:
            # OSError covers binascii.Error which is a subclass of ValueError
            # in 3.10+; keep both for portability.
            import logging

            logging.getLogger("hotframe.middleware.session").warning(
                "Dropping unreadable session cookie: %s: %s",
                type(exc).__name__,
                exc,
            )

        # Re-run with the cookie cleared. We mutate the scope's headers in
        # place to remove the offending cookie, then mark the response so
        # the browser drops it on the next round-trip.
        cookie_name = self.session_cookie  # type: ignore[attr-defined]
        scope = _scope_without_cookie(scope, cookie_name)

        async def send_with_clear(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                clear = (f"{cookie_name}=; Path=/; Max-Age=0; HttpOnly; SameSite=strict").encode()
                headers.append((b"set-cookie", clear))
                message = {**message, "headers": headers}
            await send(message)

        await super().__call__(scope, receive, send_with_clear)


def _scope_without_cookie(scope: Scope, cookie_name: str) -> Scope:
    """Return a new scope whose Cookie header excludes ``cookie_name``."""
    headers = list(scope.get("headers", []))
    new_headers: list[tuple[bytes, bytes]] = []
    for name, value in headers:
        if name.lower() != b"cookie":
            new_headers.append((name, value))
            continue
        # Cookie header is "k1=v1; k2=v2; ..."; drop the offender.
        try:
            decoded = value.decode("latin-1")
        except UnicodeDecodeError:
            # If even the cookie header is non-decodable, drop it entirely.
            continue
        kept = [
            part for part in decoded.split(";") if not part.strip().startswith(f"{cookie_name}=")
        ]
        if kept:
            new_headers.append((name, "; ".join(kept).encode("latin-1")))
        # else: skip the now-empty Cookie header.
    return {**scope, "headers": new_headers}
