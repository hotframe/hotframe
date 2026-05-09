"""
Atomic middleware stack rebuild for hot-mount.

Starlette builds ``app._middleware_stack`` lazily on the first request and
**caches it for the rest of the process lifetime**. Mutating
``app.user_middleware`` afterwards has no effect because the closures of
the cached stack already capture the previous middleware list.

This module exposes :class:`MiddlewareStackManager`, an async-safe wrapper
that can:

* clear the cache and force Starlette to rebuild the stack on the next
  request;
* add or remove middleware classes from ``app.user_middleware`` and
  trigger a rebuild atomically;
* serialize concurrent rebuild attempts via an ``asyncio.Lock`` so two
  modules installing simultaneously do not race.

The "double-buffer" name comes from the design intent: the *swap* of the
stack reference is atomic under the GIL (a single attribute assignment),
so requests that started against the old stack run to completion using
the captured closure while new requests pick up the rebuilt stack.
Starlette does not support a strict double-buffered build (the build is
synchronous and replaces in place), so this manager achieves "atomic
swap" by setting ``_middleware_stack`` to ``None`` and letting the next
request rebuild it. Under CPython this is safe — the assignment is a
single bytecode op.

Layering: the middleware layer sits between ``apps`` and
``routing/views/templating/auth/forms``. This file may import from
``fastapi`` / ``starlette`` (third-party) and stays inside the
middleware package.

This file does **not** modify ``hotframe/middleware/stack.py`` (the
existing builder used at boot). It is a sibling primitive used by the
hot-mount pipeline at runtime.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


class MiddlewareStackManager:
    """
    Rebuild the Starlette middleware stack atomically.

    Typical usage from the hot-mount pipeline::

        manager = MiddlewareStackManager(app)
        await manager.add_and_rebuild(MyModuleMiddleware, option="x")

    Or, when the caller wants to mutate ``app.user_middleware`` directly::

        async def compose() -> None:
            app.user_middleware.insert(0, Middleware(MyMw))

        await manager.rebuild(compose)

    Concurrency:
        * The internal :class:`asyncio.Lock` prevents two rebuilds from
          interleaving on the same event loop.
        * Requests in flight against the previous stack continue to use
          the closure captured at request entry — they are not aborted.
        * New requests that arrive after :meth:`rebuild` returns observe
          the new stack on their first middleware lookup.

    The manager intentionally keeps no extra state; ``app.user_middleware``
    remains the sole source of truth for what is installed.
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Core rebuild
    # ------------------------------------------------------------------ #

    async def rebuild(
        self,
        compose_stack: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """
        Force Starlette to rebuild ``app._middleware_stack``.

        Args:
            compose_stack: Optional async hook invoked **inside the lock**
                before the rebuild. Use it to mutate
                ``app.user_middleware`` so the mutation and the rebuild
                happen as a single critical section.

        Algorithm:
            1. Acquire the lock (serialize concurrent rebuilds).
            2. Invalidate the cached stack (``middleware_stack = None``).
               This is **required** before mutating
               ``app.user_middleware`` because Starlette's
               ``add_middleware`` refuses to run while a stack is built.
            3. ``await compose_stack()`` if provided. Hooks may now call
               ``app.add_middleware(...)`` or splice
               ``app.user_middleware`` directly.
            4. Force a rebuild via ``app.build_middleware_stack()``.
               Starlette caches the result back onto
               ``middleware_stack``.
            5. Release the lock.

        The reset-then-rebuild sequence is *effectively* atomic on CPython
        because (a) the attribute assignment is a single bytecode op and
        (b) ``build_middleware_stack`` is synchronous. Requests that
        observed the previous stack via Starlette's ``__call__`` keep
        executing against their captured closure.
        """
        async with self._lock:
            # Step 1: invalidate cache FIRST. Starlette's add_middleware
            # raises if middleware_stack is not None, so we must clear it
            # before allowing user code to mutate user_middleware.
            self._app.middleware_stack = None  # type: ignore[attr-defined]

            # Step 2: let the caller mutate user_middleware atomically.
            if compose_stack is not None:
                await compose_stack()

            # Step 3: force the rebuild now (eager) so the new stack is
            # ready when the next request arrives instead of paying the
            # cost on the first request.
            self._app.middleware_stack = self._app.build_middleware_stack()

            logger.debug(
                "middleware stack rebuilt count=%d",
                len(self._app.user_middleware),
            )

    # ------------------------------------------------------------------ #
    # Convenience helpers
    # ------------------------------------------------------------------ #

    async def add_and_rebuild(
        self,
        middleware_class: type,
        **options: Any,
    ) -> None:
        """
        Append ``middleware_class`` to ``app.user_middleware`` and rebuild.

        New middleware is added at the **end** of ``user_middleware``.
        Per Starlette's convention (last added = outermost on the
        request), this means the new middleware will run **first** on
        incoming requests and **last** on outgoing responses.

        Args:
            middleware_class: The Starlette/FastAPI middleware class.
            **options: Keyword arguments forwarded to the middleware
                constructor.
        """

        async def compose() -> None:
            # ``middleware_class`` is the dynamic ASGI middleware class users
            # pass in. Starlette's typing wants a precise factory; runtime
            # validation happens inside ``add_middleware`` itself.
            self._app.add_middleware(middleware_class, **options)  # type: ignore[arg-type]

        await self.rebuild(compose)

    async def remove_and_rebuild(self, middleware_class: type) -> None:
        """
        Remove every entry of ``middleware_class`` from
        ``app.user_middleware`` and rebuild.

        Removal is by class identity (``mw.cls is middleware_class``).
        Multiple instances of the same class are all removed. If the
        class is not present, the rebuild still happens — callers can
        treat the operation as idempotent.
        """

        async def compose() -> None:
            self._app.user_middleware = [
                mw
                for mw in self._app.user_middleware
                if getattr(mw, "cls", None) is not middleware_class
            ]

        await self.rebuild(compose)
