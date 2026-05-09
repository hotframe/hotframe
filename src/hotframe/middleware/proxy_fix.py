# SPDX-License-Identifier: Apache-2.0
"""
Proxy fix middleware for reverse proxy deployments.

Rewrites ASGI scope host/scheme when behind a reverse proxy.
Configured via ``settings.PROXY_*`` fields.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class ProxyFixMiddleware:
    """Rewrite ASGI scope host/scheme when behind a reverse proxy."""

    def __init__(
        self,
        app: ASGIApp,
        slug: str = "",
        domain_base: str = "",
        ecs_region: str = "",
    ) -> None:
        self.app = app
        self._ecs_suffix = f".ecs.{ecs_region}.on.aws".encode() if ecs_region else b""
        self._public_host: bytes | None = (
            f"{slug}.{domain_base}".encode() if slug and domain_base else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        host_header = headers.get(b"host", b"")

        if self._public_host and self._ecs_suffix and self._ecs_suffix in host_header:
            scope["server"] = (self._public_host.decode("ascii"), 443)
            new_headers = []
            for k, v in scope["headers"]:
                if k == b"host":
                    new_headers.append((b"host", self._public_host))
                else:
                    new_headers.append((k, v))
            scope["headers"] = new_headers

        elif b"x-forwarded-host" in headers:
            fwd_host = headers[b"x-forwarded-host"]
            scope["server"] = (fwd_host.decode("latin-1").split(":")[0], 443)
            new_headers = []
            for k, v in scope["headers"]:
                if k == b"host":
                    new_headers.append((b"host", fwd_host))
                else:
                    new_headers.append((k, v))
            scope["headers"] = new_headers

        forwarded_proto = headers.get(b"x-forwarded-proto")
        if forwarded_proto:
            scope["scheme"] = forwarded_proto.decode("latin-1")
        elif self._public_host and self._ecs_suffix and self._ecs_suffix in host_header:
            scope["scheme"] = "https"

        response_started: dict = {}
        body_chunks: list[bytes] = []

        async def _buffering_send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers_list = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"content-length"
                ]
                response_started["headers"] = headers_list
                response_started["status"] = message["status"]
                response_started["extra"] = {
                    k: v for k, v in message.items() if k not in ("type", "status", "headers")
                }
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    body_chunks.append(chunk)
                if not message.get("more_body", False):
                    full_body = b"".join(body_chunks)
                    start_msg: dict = {
                        "type": "http.response.start",
                        "status": response_started["status"],
                        "headers": response_started["headers"]
                        + [(b"content-length", str(len(full_body)).encode())],
                    }
                    start_msg.update(response_started.get("extra", {}))
                    await send(start_msg)
                    await send(
                        {
                            "type": "http.response.body",
                            "body": full_body,
                            "more_body": False,
                        }
                    )
            else:
                await send(message)

        await self.app(scope, receive, _buffering_send)
