# SPDX-License-Identifier: Apache-2.0
"""
:class:`HttpClientRegistry` — typed named registry for HTTP clients.

Lives on ``app.state.http_clients``. Keeps track of which module
registered each client so a module unload can drop all of its
clients without leaking connections or leaving stale names behind.

Also owns the optional "ambient" interceptor pool discovered by
:func:`hotframe.http.loader.discover_interceptors` — every newly
registered client is automatically wrapped in the interceptors whose
``applies_to`` matcher picks its name.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from hotframe.http.client import AuthenticatedClient
from hotframe.http.interceptors import Interceptor, InterceptorBase

logger = logging.getLogger(__name__)


class HttpClientRegistry:
    """Named registry of :class:`AuthenticatedClient` instances.

    The registry is created once by :func:`hotframe.create_app` and
    exposed as ``app.state.http_clients``. Projects register
    process-wide clients during startup; modules register their own
    clients when they activate. On shutdown the registry closes every
    client it still owns.

    Module ownership is tracked so :meth:`unregister_module` can drop
    all clients contributed by a given module in a single call — the
    safety net used by :class:`~hotframe.engine.module_runtime.ModuleRuntime`
    when a module is deactivated or uninstalled.

    Args:
        ambient_interceptors: Optional shared list of interceptors that
            will be auto-applied to every client registered without
            explicit ``interceptors=``. The registry keeps a reference
            to the list, so bootstrap code can mutate the discovered
            pool at any time (add custom interceptors, strip defaults)
            and new registrations will pick up the change.
    """

    def __init__(
        self,
        ambient_interceptors: list[Interceptor] | None = None,
    ) -> None:
        self._clients: dict[str, AuthenticatedClient] = {}
        # Name → owning module id (or ``None`` for app-level clients).
        # Kept as a parallel dict so the registry stays a simple mapping
        # for the common read paths (``__getitem__``, ``get``, iteration).
        self._owners: dict[str, str | None] = {}
        self._ambient_interceptors = ambient_interceptors

    # ------------------------------------------------------------------
    # Ambient interceptors
    # ------------------------------------------------------------------

    def set_ambient_interceptors(self, interceptors: list[Interceptor] | None) -> None:
        """Replace the ambient interceptor pool.

        Subsequent ``register()`` calls without an explicit
        ``interceptors=`` will be wrapped by the new pool.
        """
        self._ambient_interceptors = interceptors

    @property
    def ambient_interceptors(self) -> list[Interceptor]:
        """Return a shallow copy of the ambient interceptor pool."""
        return list(self._ambient_interceptors or [])

    def _match_ambient(self, name: str) -> list[Interceptor]:
        """Return ambient interceptors that apply to client ``name``."""
        pool = self._ambient_interceptors or []
        out: list[Interceptor] = []
        for interceptor in pool:
            if isinstance(interceptor, InterceptorBase):
                if interceptor.applies_to_client(name):
                    out.append(interceptor)
                continue
            # Fallback matcher for bare Interceptor protocol instances.
            matcher = getattr(interceptor, "applies_to", "*")
            if callable(matcher):
                try:
                    if matcher(name):
                        out.append(interceptor)
                except Exception:
                    logger.exception(
                        "Interceptor %r applies_to callable raised; skipping",
                        getattr(interceptor, "name", "?"),
                    )
                continue
            if isinstance(matcher, str):
                if matcher == "*" or matcher == name:
                    out.append(interceptor)
                continue
            if isinstance(matcher, list | tuple | set) and name in matcher:
                out.append(interceptor)
        return out

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        client: AuthenticatedClient,
        owner_module_id: str | None = None,
        interceptors: list[Interceptor] | None = None,
    ) -> None:
        """Register a client under ``name``.

        When ``interceptors`` is provided, it is appended to whatever
        interceptors the client already carries and the combined list
        is re-sorted by ``order``. When ``interceptors`` is omitted the
        registry consults its ambient pool (see
        :meth:`set_ambient_interceptors`) and auto-applies anything
        that matches the client's name.

        Args:
            name: Unique name used to look up the client.
            client: The :class:`AuthenticatedClient` to store.
            owner_module_id: Optional module id that owns the client.
                When provided, the client is dropped by
                :meth:`unregister_module` on module deactivation.
            interceptors: Optional explicit interceptor list. Mutually
                exclusive with ambient auto-apply — passing this
                disables ambient matching for this call.

        Raises:
            ValueError: If ``name`` is empty.
            KeyError: If ``name`` is already registered. Use
                :meth:`replace` to overwrite silently.
        """
        self._validate_name(name)
        if not isinstance(client, AuthenticatedClient):
            raise TypeError(
                f"HttpClientRegistry.register expected AuthenticatedClient, "
                f"got {type(client).__name__}"
            )
        if name in self._clients:
            raise KeyError(
                f"HTTP client {name!r} is already registered "
                f"(owner={self._owners.get(name)!r}). Use replace() to overwrite."
            )
        self._apply_interceptors(client, name, interceptors)
        self._clients[name] = client
        self._owners[name] = owner_module_id

    def replace(
        self,
        name: str,
        client: AuthenticatedClient,
        owner_module_id: str | None = None,
        interceptors: list[Interceptor] | None = None,
    ) -> None:
        """Register ``name`` → ``client``, overwriting any existing entry.

        The previously registered client — if any — is closed
        asynchronously in the background so the new client is
        immediately usable without awaiting teardown.

        Args:
            name: Unique name used to look up the client.
            client: The replacement :class:`AuthenticatedClient`.
            owner_module_id: Optional module id that owns the client.
            interceptors: Optional explicit interceptor list (see
                :meth:`register`).
        """
        self._validate_name(name)
        if not isinstance(client, AuthenticatedClient):
            raise TypeError(
                f"HttpClientRegistry.replace expected AuthenticatedClient, "
                f"got {type(client).__name__}"
            )
        self._apply_interceptors(client, name, interceptors)
        previous = self._clients.get(name)
        self._clients[name] = client
        self._owners[name] = owner_module_id
        if previous is not None and previous is not client:
            self._close_in_background(previous, name)

    def _apply_interceptors(
        self,
        client: AuthenticatedClient,
        name: str,
        explicit: list[Interceptor] | None,
    ) -> None:
        """Merge explicit + ambient interceptors onto ``client``.

        Explicit interceptors always win — if any are provided the
        ambient pool is skipped for this registration.
        """
        if explicit is not None:
            combined = list(client.interceptors) + list(explicit)
        else:
            combined = list(client.interceptors) + self._match_ambient(name)
        if not combined:
            return
        # Deduplicate by ``name`` — first win — and re-order. The dedup key
        # is either the interceptor's declared ``name`` (str) or, lacking
        # one, ``id(...)`` (int). Both compare-and-hash safely in a set.
        seen: set[Any] = set()
        deduped: list[Interceptor] = []
        for interceptor in combined:
            iname = getattr(interceptor, "name", None) or id(interceptor)
            if iname in seen:
                continue
            seen.add(iname)
            deduped.append(interceptor)
        deduped.sort(key=lambda i: getattr(i, "order", 100))
        client.set_interceptors(deduped)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> AuthenticatedClient | None:
        """Return the client registered under ``name`` or ``None``."""
        return self._clients.get(name)

    def __getitem__(self, name: str) -> AuthenticatedClient:
        """Return the client registered under ``name``.

        Raises:
            KeyError: If ``name`` is not registered.
        """
        try:
            return self._clients[name]
        except KeyError as exc:
            raise KeyError(
                f"HTTP client {name!r} is not registered. "
                f"Registered clients: {sorted(self._clients)}"
            ) from exc

    def __contains__(self, name: object) -> bool:
        return name in self._clients

    def __len__(self) -> int:
        return len(self._clients)

    def list_registered(self) -> list[str]:
        """Return the list of registered client names in insertion order."""
        return list(self._clients)

    def owner_of(self, name: str) -> str | None:
        """Return the owner module id of ``name`` or ``None`` if not owned.

        Raises:
            KeyError: If ``name`` is not registered.
        """
        if name not in self._clients:
            raise KeyError(f"HTTP client {name!r} is not registered")
        return self._owners.get(name)

    # ------------------------------------------------------------------
    # Deregistration
    # ------------------------------------------------------------------

    async def unregister(self, name: str) -> None:
        """Close and drop the client registered under ``name``.

        Raises:
            KeyError: If ``name`` is not registered.
        """
        if name not in self._clients:
            raise KeyError(
                f"HTTP client {name!r} is not registered. "
                f"Registered clients: {sorted(self._clients)}"
            )
        client = self._clients.pop(name)
        self._owners.pop(name, None)
        await self._aclose_silent(client, name)

    async def unregister_module(self, module_id: str) -> None:
        """Close and drop every client owned by ``module_id``.

        Called by :class:`~hotframe.engine.module_runtime.ModuleRuntime`
        as a safety net when a module is deactivated or uninstalled.
        No-op when the module owns no clients.
        """
        names = [n for n, owner in self._owners.items() if owner == module_id]
        for name in names:
            client = self._clients.pop(name, None)
            self._owners.pop(name, None)
            if client is not None:
                await self._aclose_silent(client, name)

    async def aclose_all(self) -> None:
        """Close every registered client. Called on application shutdown."""
        # Copy to avoid mutating the dict during iteration.
        items = list(self._clients.items())
        self._clients.clear()
        self._owners.clear()
        for name, client in items:
            await self._aclose_silent(client, name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("HTTP client name must be a non-empty string")

    @staticmethod
    async def _aclose_silent(client: AuthenticatedClient, name: str) -> None:
        """Close a client, swallowing errors so cleanup always runs."""
        try:
            await client.aclose()
        except Exception:
            logger.exception("Failed to close HTTP client %r during teardown", name)

    @staticmethod
    def _close_in_background(client: AuthenticatedClient, name: str) -> None:
        """Fire-and-forget close for a replaced client.

        When called outside a running event loop (e.g. during test
        teardown) the close is attempted synchronously via
        ``asyncio.run`` on a throwaway loop so no connection leaks.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — try a throwaway loop.
            try:
                asyncio.run(HttpClientRegistry._aclose_silent(client, name))
            except Exception:
                logger.exception(
                    "Failed to close replaced HTTP client %r without a running loop", name
                )
            return
        loop.create_task(HttpClientRegistry._aclose_silent(client, name))

    def __repr__(self) -> str:
        return f"<HttpClientRegistry clients={len(self._clients)}>"


__all__ = ["HttpClientRegistry"]
