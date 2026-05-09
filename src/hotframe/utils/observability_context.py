"""
Request-scoped context via contextvars.

Provides a single ``RequestContext`` that carries request_id, hub_id, user_id,
module_id, and trace_id through async call chains. Automatically propagated
by Python's contextvars mechanism — no manual threading required.

Usage::

    from hotframe.utils.observability_context import request_context, bind_context

    # In middleware (per-request):
    with bind_context(request_id="abc-123", hub_id="...", user_id="..."):
        ...  # all downstream code sees the context

    # Anywhere in the call chain:
    ctx = request_context.get()
    print(ctx.request_id)
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class RequestContext:
    """Immutable-ish container for request-scoped identifiers."""

    request_id: str = ""
    hub_id: str = ""
    user_id: str = ""
    module_id: str = ""
    trace_id: str = ""

    def bind_dict(self) -> dict[str, str]:
        """Return non-empty fields for structlog context binding."""
        result: dict[str, str] = {}
        if self.request_id:
            result["request_id"] = self.request_id
        if self.hub_id:
            result["hub_id"] = self.hub_id
        if self.user_id:
            result["user_id"] = self.user_id
        if self.module_id:
            result["module_id"] = self.module_id
        if self.trace_id:
            result["trace_id"] = self.trace_id
        return result


# The single context variable — one per async task / thread.
request_context: ContextVar[RequestContext] = ContextVar(
    "request_context",
    default=RequestContext(),
)


@contextmanager
def bind_context(**kwargs: str) -> Generator[RequestContext, None, None]:
    """
    Context manager that sets request-scoped values for the duration of a block.

    Restores the previous context on exit (safe for nested usage).

    Example::

        with bind_context(request_id="abc", hub_id="hub-1"):
            # code here sees request_id="abc"
            ...
        # original context restored
    """
    previous = request_context.get()
    new_ctx = RequestContext(
        request_id=kwargs.get("request_id", previous.request_id),
        hub_id=kwargs.get("hub_id", previous.hub_id),
        user_id=kwargs.get("user_id", previous.user_id),
        module_id=kwargs.get("module_id", previous.module_id),
        trace_id=kwargs.get("trace_id", previous.trace_id),
    )
    token = request_context.set(new_ctx)
    try:
        yield new_ctx
    finally:
        request_context.reset(token)


def update_context(**kwargs: str) -> None:
    """
    Update specific fields in the current context without a context manager.

    Useful when you learn information mid-request (e.g., user_id after auth).
    Creates a new RequestContext and sets it as current.
    """
    current = request_context.get()
    new_ctx = RequestContext(
        request_id=kwargs.get("request_id", current.request_id),
        hub_id=kwargs.get("hub_id", current.hub_id),
        user_id=kwargs.get("user_id", current.user_id),
        module_id=kwargs.get("module_id", current.module_id),
        trace_id=kwargs.get("trace_id", current.trace_id),
    )
    request_context.set(new_ctx)
