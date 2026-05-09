"""Tests for hotframe.testing utilities."""

import pytest

from hotframe.testing import FakeEventBus, FakeHookRegistry


class TestFakeEventBus:
    @pytest.mark.asyncio
    async def test_emit(self):
        bus = FakeEventBus()
        await bus.emit("test.event", {"key": "value"})
        assert len(bus.events) == 1
        assert bus.events[0] == ("test.event", {"key": "value"})

    @pytest.mark.asyncio
    async def test_emit_typed(self):
        bus = FakeEventBus()
        await bus.emit_typed({"type": "test"})
        assert len(bus.typed_events) == 1

    @pytest.mark.asyncio
    async def test_reset(self):
        bus = FakeEventBus()
        await bus.emit("a", 1)
        await bus.emit_typed("b")
        bus.reset()
        assert len(bus.events) == 0
        assert len(bus.typed_events) == 0


class TestFakeHookRegistry:
    @pytest.mark.asyncio
    async def test_action(self):
        hooks = FakeHookRegistry()
        called = []

        async def my_action(*args, **kwargs):
            called.append(True)

        hooks.add_action("test", my_action)
        await hooks.do_action("test")
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_filter(self):
        hooks = FakeHookRegistry()

        async def double(value, **kwargs):
            return value * 2

        hooks.add_filter("multiply", double)
        result = await hooks.apply_filters("multiply", 5)
        assert result == 10
