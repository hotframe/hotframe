# SPDX-License-Identifier: Apache-2.0
"""
Authentication strategies for :class:`AuthenticatedClient`.

An ``Auth`` strategy knows *how* to authenticate an outgoing
``httpx.Request``. It exposes a single async method:

.. code-block:: python

    class Auth:
        async def apply(self, request: httpx.Request) -> None: ...

Every strategy in this module is stateless except for the resolved
credential source it holds, and is designed to be re-evaluated on
every request. Callers that want token rotation simply pass a callable
as ``source`` — sync or async — and the strategy re-reads it before
each call, so rotating a secret never requires a restart.

Built-in strategies:

- :class:`BearerAuth` — ``Authorization: Bearer <token>``
- :class:`ApiKeyAuth` — arbitrary header carrying a key
- :class:`QueryApiKeyAuth` — query-string carrying a key
- :class:`BasicAuth` — RFC 7617 basic auth
- :class:`HmacAuth` — HMAC-signed body authorization
- :class:`CustomAuth` — arbitrary user-supplied async callable
- :class:`NoAuth` — explicit no-op
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
from collections.abc import Awaitable, Callable

import httpx

# Type alias for credential sources accepted by most strategies.
#
# Either a plain string, a sync callable returning a string, or an
# async callable returning a string. Callables are re-evaluated on
# every request.
CredentialSource = str | Callable[[], str] | Callable[[], Awaitable[str]]


async def _resolve_source(source: CredentialSource) -> str:
    """Resolve a credential source to a plain string.

    Accepts a string, a sync callable, or an async callable. Sync and
    async callables are invoked on every call to ``apply`` so rotating
    a credential source works without restarting the process.

    Raises:
        TypeError: If ``source`` is neither a string nor callable, or
            if the callable returns a non-string value.
    """
    if isinstance(source, str):
        return source
    if not callable(source):
        raise TypeError(
            f"Auth credential source must be a string or callable, got {type(source).__name__}"
        )
    value = source()
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, str):
        raise TypeError(
            f"Auth credential source callable must return a string, got {type(value).__name__}"
        )
    return value


class Auth:
    """Abstract base for authentication strategies.

    Subclasses implement :meth:`apply` to mutate the outgoing
    ``httpx.Request`` in place (adding headers, query params, signing
    the body, etc.). ``apply`` is awaited by :class:`AuthenticatedClient`
    before every dispatched request.
    """

    async def apply(self, request: httpx.Request) -> None:
        """Mutate ``request`` in place to apply authentication."""
        raise NotImplementedError


class BearerAuth(Auth):
    """Attach an ``Authorization: Bearer <token>`` header."""

    def __init__(self, source: CredentialSource) -> None:
        self._source = source

    async def apply(self, request: httpx.Request) -> None:
        token = await _resolve_source(self._source)
        request.headers["Authorization"] = f"Bearer {token}"


class ApiKeyAuth(Auth):
    """Attach an API key in a configurable header (default ``X-Api-Key``)."""

    def __init__(self, source: CredentialSource, header: str = "X-Api-Key") -> None:
        if not header:
            raise ValueError("ApiKeyAuth header name cannot be empty")
        self._source = source
        self._header = header

    async def apply(self, request: httpx.Request) -> None:
        value = await _resolve_source(self._source)
        request.headers[self._header] = value


class QueryApiKeyAuth(Auth):
    """Attach an API key as a query-string parameter."""

    def __init__(self, source: CredentialSource, param: str = "api_key") -> None:
        if not param:
            raise ValueError("QueryApiKeyAuth param name cannot be empty")
        self._source = source
        self._param = param

    async def apply(self, request: httpx.Request) -> None:
        value = await _resolve_source(self._source)
        # ``httpx.URL.copy_merge_params`` appends the param without
        # clobbering existing entries. We overwrite just our own key so
        # key rotation mid-flight doesn't leave a stale query pair.
        new_url = request.url.copy_merge_params({self._param: value})
        request.url = new_url


class BasicAuth(Auth):
    """RFC 7617 HTTP Basic authentication.

    The header is recomputed per request to stay consistent with the
    rest of the strategies — even though username/password are fixed
    at construction time.
    """

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password

    async def apply(self, request: httpx.Request) -> None:
        token = base64.b64encode(f"{self._username}:{self._password}".encode()).decode("ascii")
        request.headers["Authorization"] = f"Basic {token}"


class HmacAuth(Auth):
    """HMAC-signed authorization.

    Signs the request body (or the empty bytes for bodies-less
    requests) with ``secret`` under the configured hash algorithm and
    writes::

        Authorization: HMAC-<ALGO> KeyId=<key_id>, Signature=<hex>

    Args:
        key_id: Public key identifier sent as part of the header.
        secret: Shared secret used as the HMAC key.
        algorithm: Hash algorithm name accepted by ``hashlib.new``.
            Defaults to ``"sha256"``.

    Raises:
        ValueError: If ``algorithm`` is not supported by ``hashlib``.
    """

    def __init__(self, key_id: str, secret: str, algorithm: str = "sha256") -> None:
        if not key_id:
            raise ValueError("HmacAuth key_id cannot be empty")
        if not secret:
            raise ValueError("HmacAuth secret cannot be empty")
        if algorithm not in hashlib.algorithms_available:
            raise ValueError(
                f"HmacAuth algorithm {algorithm!r} is not supported by hashlib "
                f"(available: {sorted(hashlib.algorithms_guaranteed)})"
            )
        self._key_id = key_id
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._algorithm = algorithm

    async def apply(self, request: httpx.Request) -> None:
        # ``httpx.Request.content`` is already the bytes on the wire
        # for bodies we set ourselves; for streaming bodies it returns
        # ``b""`` — which still produces a deterministic signature,
        # albeit one that does not cover the streamed payload. Callers
        # that stream bodies should provide a stronger strategy via
        # :class:`CustomAuth`.
        body = request.content or b""
        signature = hmac.new(self._secret, body, self._algorithm).hexdigest()
        header = f"HMAC-{self._algorithm.upper()} KeyId={self._key_id}, Signature={signature}"
        request.headers["Authorization"] = header


class CustomAuth(Auth):
    """Delegate authentication to a user-supplied async callable.

    The callable receives the ``httpx.Request`` and is expected to
    mutate it in place (adding headers, signing, rewriting the URL,
    etc.). Sync callables are rejected — async is required because
    some real-world auth flows (OAuth refresh, SigV4 with async AWS
    metadata lookups) must await I/O.
    """

    def __init__(self, apply: Callable[[httpx.Request], Awaitable[None]]) -> None:
        if not callable(apply):
            raise TypeError("CustomAuth requires a callable")
        if not inspect.iscoroutinefunction(apply):
            raise TypeError(
                "CustomAuth requires an async callable (use 'async def'). "
                "Wrap synchronous logic in an async function if needed."
            )
        self._apply = apply

    async def apply(self, request: httpx.Request) -> None:
        await self._apply(request)


class NoAuth(Auth):
    """Explicit no-op authentication — the default for unauthenticated clients."""

    async def apply(self, request: httpx.Request) -> None:
        return None


__all__ = [
    "ApiKeyAuth",
    "Auth",
    "BasicAuth",
    "BearerAuth",
    "CredentialSource",
    "CustomAuth",
    "HmacAuth",
    "NoAuth",
    "QueryApiKeyAuth",
]
