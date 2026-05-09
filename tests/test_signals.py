"""Tests for hotframe.signals."""

import pytest

from hotframe.signals.builtins import (
    AUTH_LOGIN,
    AUTH_LOGOUT,
    MODEL_POST_DELETE,
    MODEL_POST_SAVE,
    MODEL_PRE_DELETE,
    MODEL_PRE_SAVE,
    MODULES_ACTIVATED,
    MODULES_DEACTIVATED,
    MODULES_INSTALLED,
    SYSTEM_SIGNALS,
    get_event_class,
)
from hotframe.signals.dispatcher import AsyncEventBus
from hotframe.signals.hooks import HookRegistry


class TestAsyncEventBus:
    @pytest.mark.asyncio
    async def test_emit_and_receive(self):
        bus = AsyncEventBus()
        received = []

        async def handler(**data):
            received.append(data)

        await bus.subscribe("test.event", handler)
        await bus.emit("test.event", key="value")
        assert len(received) == 1
        assert received[0]["key"] == "value"

    @pytest.mark.asyncio
    async def test_wildcard(self):
        bus = AsyncEventBus()
        received = []

        async def handler(**data):
            received.append(data)

        await bus.subscribe("test.*", handler)
        await bus.emit("test.one", val="a")
        await bus.emit("test.two", val="b")
        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_off(self):
        bus = AsyncEventBus()
        received = []

        async def handler(**data):
            received.append(data)

        await bus.subscribe("test.event", handler)
        await bus.unsubscribe("test.event", handler)
        await bus.emit("test.event", val="data")
        assert len(received) == 0


class TestHookRegistry:
    @pytest.mark.asyncio
    async def test_action(self):
        hooks = HookRegistry()
        results = []

        async def my_action(*args, **kwargs):
            results.append("called")

        hooks.add_action("test.action", my_action)
        await hooks.do_action("test.action")
        assert results == ["called"]

    @pytest.mark.asyncio
    async def test_filter(self):
        hooks = HookRegistry()

        async def add_exclaim(value, **kwargs):
            return value + "!"

        hooks.add_filter("test.filter", add_exclaim)
        result = await hooks.apply_filters("test.filter", "hello")
        assert result == "hello!"


class TestBuiltinSignals:
    def test_model_lifecycle_signals(self):
        assert MODEL_PRE_SAVE == "model.pre_save"
        assert MODEL_POST_SAVE == "model.post_save"
        assert MODEL_PRE_DELETE == "model.pre_delete"
        assert MODEL_POST_DELETE == "model.post_delete"

    def test_auth_signals(self):
        assert AUTH_LOGIN == "auth.login"
        assert AUTH_LOGOUT == "auth.logout"

    def test_module_signals(self):
        assert MODULES_INSTALLED == "modules.installed"
        assert MODULES_ACTIVATED == "modules.activated"
        assert MODULES_DEACTIVATED == "modules.deactivated"

    def test_system_signals_dict(self):
        assert isinstance(SYSTEM_SIGNALS, dict)
        assert "MODEL_PRE_SAVE" in SYSTEM_SIGNALS
        assert SYSTEM_SIGNALS["MODEL_PRE_SAVE"] == "model.pre_save"

    def test_get_event_class(self):
        cls = get_event_class("model.post_save")
        assert cls is not None
        assert cls.event_name == "model.post_save"


class TestBaseEvent:
    def test_create_event(self):
        from hotframe.signals.catalog import ModelPostSaveEvent

        event = ModelPostSaveEvent(model_name="User", created=True)
        assert event.model_name == "User"
        assert event.created is True
        assert event.event_name == "model.post_save"
