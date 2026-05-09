# SPDX-License-Identifier: Apache-2.0
"""Tests for ``LiveComponent`` — events table, lifecycle, state mutation."""

from __future__ import annotations

import pytest

from hotframe.live import LiveComponent, event


class Counter(LiveComponent):
    start: int = 0  # prop with default
    value: int = 0  # state

    async def on_mount(self) -> None:
        self.value = self.start

    @event("inc")
    async def inc(self) -> None:
        self.value += 1

    @event("add")
    async def add(self, n: int) -> None:
        self.value += int(n)

    @event("set")
    async def set_value(self, value: int) -> None:
        self.value = int(value)


def test_events_table_built_at_subclass() -> None:
    assert "inc" in Counter._events
    assert "add" in Counter._events
    assert "set" in Counter._events
    # Internal Pydantic helpers should not leak in.
    assert "model_dump" not in Counter._events


def test_events_table_isolated_per_subclass() -> None:
    class OtherCounter(LiveComponent):
        @event("only")
        async def only(self) -> None:
            pass

    assert "only" in OtherCounter._events
    assert "only" not in Counter._events
    assert "inc" not in OtherCounter._events


def test_event_on_non_async_method_raises_on_class_creation() -> None:
    with pytest.raises(TypeError):

        class Broken(LiveComponent):
            @event("oops")  # type: ignore[type-var]  # intentional: not async, must blow up
            def sync_handler(self) -> None:
                pass


@pytest.mark.asyncio
async def test_on_mount_runs_and_sets_state() -> None:
    c = Counter(start=10)
    await c.on_mount()
    assert c.value == 10


@pytest.mark.asyncio
async def test_event_handler_mutates_state() -> None:
    c = Counter(start=0)
    handler = Counter._events["inc"]
    await handler(c)
    assert c.value == 1
    await handler(c)
    assert c.value == 2


@pytest.mark.asyncio
async def test_event_handler_with_payload() -> None:
    c = Counter(start=0)
    handler = Counter._events["add"]
    await handler(c, 5)
    assert c.value == 5
    await handler(c, "3")  # str payload coerced via int(...)
    assert c.value == 8


def test_render_context_includes_props_and_state() -> None:
    c = Counter(start=42)
    ctx = c.render_context()
    assert ctx["start"] == 42
    assert ctx["value"] == 0  # state default


def test_extra_context_override_appears_in_render_context() -> None:
    class WithExtras(LiveComponent):
        a: int = 1

        def extra_context(self) -> dict:
            return {"doubled": self.a * 2}

    c = WithExtras(a=5)
    ctx = c.render_context()
    assert ctx["a"] == 5
    assert ctx["doubled"] == 10


def test_validate_assignment_rejects_wrong_type() -> None:
    from pydantic import ValidationError

    c = Counter(start=0)
    with pytest.raises(ValidationError):
        # Pydantic's validate_assignment should refuse a non-int.
        c.value = "not-an-int"  # type: ignore[assignment]


def test_cid_starts_empty_and_can_be_stamped() -> None:
    c = Counter(start=0)
    assert c.cid == ""
    c._cid = "c-test"
    assert c.cid == "c-test"


def test_session_property_default_none() -> None:
    c = Counter(start=0)
    assert c.session is None
