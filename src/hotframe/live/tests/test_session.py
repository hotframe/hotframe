# SPDX-License-Identifier: Apache-2.0
"""Tests for ``LiveSession`` — message dispatch, event invocation, isolation.

The session normally drives a real WebSocket; tests use a small fake
that records every ``send_json`` call.
"""

from __future__ import annotations

import pytest
from jinja2 import DictLoader, Environment

from hotframe.components.entry import ComponentEntry
from hotframe.components.registry import ComponentRegistry
from hotframe.live import LiveComponent, event
from hotframe.live.runtime import LiveRuntime
from hotframe.live.session import LiveSession

# ---------------------------------------------------------------------------
# Fixtures: minimal Counter component + Jinja env wired to a string template.
# ---------------------------------------------------------------------------


class Counter(LiveComponent):
    start: int = 0
    value: int = 0

    async def on_mount(self) -> None:
        self.value = self.start

    @event("inc")
    async def inc(self) -> None:
        self.value += 1

    @event("set")
    async def set_value(self, value: int) -> None:
        self.value = int(value)


class FailMount(LiveComponent):
    async def on_mount(self) -> None:
        raise RuntimeError("nope")


class FailEvent(LiveComponent):
    @event("explode")
    async def explode(self) -> None:
        raise ValueError("kaboom")


def _make_env() -> Environment:
    """Tiny env with a template per registered component."""
    return Environment(
        loader=DictLoader(
            {
                "counter/template.html": "<span>{{ value }}</span>",
                "fail_mount/template.html": "<span>x</span>",
                "fail_event/template.html": "<span>x</span>",
            }
        )
    )


def _registry_for(*pairs) -> ComponentRegistry:
    """Build a registry from (name, cls) tuples."""
    reg = ComponentRegistry()
    for name, cls in pairs:
        reg.register(
            ComponentEntry(
                name=name,
                template=f"{name}/template.html",
                props_cls=cls,
                is_live=True,
            )
        )
    return reg


class FakeWebSocket:
    """Captures send_json calls; satisfies the duck-typed dependency."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, payload) -> None:
        self.sent.append(payload)


@pytest.fixture
def runtime() -> LiveRuntime:
    return LiveRuntime(
        registry=_registry_for(
            ("counter", Counter),
            ("fail_mount", FailMount),
            ("fail_event", FailEvent),
        ),
        env=_make_env(),
    )


@pytest.fixture
def session(runtime: LiveRuntime) -> tuple[LiveSession, FakeWebSocket]:
    ws = FakeWebSocket()
    s = LiveSession("test-session", ws, runtime)
    return s, ws


# ---------------------------------------------------------------------------
# Attach
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_runs_on_mount_and_sends_initial_patch(session) -> None:
    s, ws = session
    await s.handle_message({"t": "attach", "cid": "c-1", "name": "counter", "props": {"start": 5}})
    assert "c-1" in s.components
    assert s.components["c-1"].value == 5
    assert ws.sent[-1] == {"t": "patch", "cid": "c-1", "html": "<span>5</span>"}


@pytest.mark.asyncio
async def test_attach_unknown_component_returns_err(session) -> None:
    s, ws = session
    await s.handle_message({"t": "attach", "cid": "c-x", "name": "does_not_exist", "props": {}})
    assert "c-x" not in s.components
    assert ws.sent[-1]["t"] == "err"
    assert ws.sent[-1]["code"] == "not_found"


@pytest.mark.asyncio
async def test_attach_invalid_props_returns_err(session) -> None:
    s, ws = session
    await s.handle_message(
        {"t": "attach", "cid": "c-1", "name": "counter", "props": {"start": "no"}}
    )
    assert "c-1" not in s.components
    assert ws.sent[-1]["t"] == "err"
    assert ws.sent[-1]["code"] == "props"


@pytest.mark.asyncio
async def test_attach_on_mount_failure_returns_err(session) -> None:
    s, ws = session
    await s.handle_message({"t": "attach", "cid": "c-fail", "name": "fail_mount", "props": {}})
    assert ws.sent[-1]["t"] == "err"
    assert ws.sent[-1]["code"] == "mount"


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_runs_handler_and_sends_patch(session) -> None:
    s, ws = session
    await s.handle_message({"t": "attach", "cid": "c-1", "name": "counter", "props": {"start": 0}})
    ws.sent.clear()

    await s.handle_message({"t": "event", "cid": "c-1", "n": "inc"})
    assert s.components["c-1"].value == 1
    assert ws.sent[-1] == {"t": "patch", "cid": "c-1", "html": "<span>1</span>"}


@pytest.mark.asyncio
async def test_event_with_payload(session) -> None:
    s, ws = session
    await s.handle_message({"t": "attach", "cid": "c-1", "name": "counter", "props": {}})
    ws.sent.clear()

    await s.handle_message({"t": "event", "cid": "c-1", "n": "set", "p": "42"})
    assert s.components["c-1"].value == 42


@pytest.mark.asyncio
async def test_event_unknown_returns_err(session) -> None:
    s, ws = session
    await s.handle_message({"t": "attach", "cid": "c-1", "name": "counter", "props": {}})
    ws.sent.clear()

    await s.handle_message({"t": "event", "cid": "c-1", "n": "ghost"})
    assert ws.sent[-1]["t"] == "err"
    assert ws.sent[-1]["code"] == "not_found"


@pytest.mark.asyncio
async def test_event_handler_failure_returns_err(session) -> None:
    s, ws = session
    await s.handle_message({"t": "attach", "cid": "c-1", "name": "fail_event", "props": {}})
    ws.sent.clear()

    await s.handle_message({"t": "event", "cid": "c-1", "n": "explode"})
    assert ws.sent[-1]["t"] == "err"
    assert ws.sent[-1]["code"] == "handler"


@pytest.mark.asyncio
async def test_event_on_unattached_cid_returns_err(session) -> None:
    s, ws = session
    await s.handle_message({"t": "event", "cid": "ghost", "n": "inc"})
    assert ws.sent[-1]["t"] == "err"
    assert ws.sent[-1]["code"] == "not_attached"


# ---------------------------------------------------------------------------
# Bind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_updates_state_without_render(session) -> None:
    s, ws = session
    await s.handle_message({"t": "attach", "cid": "c-1", "name": "counter", "props": {}})
    ws.sent.clear()

    await s.handle_message({"t": "bind", "cid": "c-1", "f": "value", "v": 7})
    assert s.components["c-1"].value == 7
    assert ws.sent == []  # bind never re-renders


# ---------------------------------------------------------------------------
# Detach + shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detach_runs_on_unmount_and_drops_instance(session) -> None:
    unmount_called: list[str] = []

    class WithUnmount(LiveComponent):
        async def on_unmount(self) -> None:
            unmount_called.append("yes")

    s, _ = session
    s.runtime.registry.register(
        ComponentEntry(
            name="with_unmount",
            template="counter/template.html",  # reuse, content irrelevant
            props_cls=WithUnmount,
            is_live=True,
        )
    )

    await s.handle_message({"t": "attach", "cid": "c-u", "name": "with_unmount", "props": {}})
    await s.handle_message({"t": "detach", "cid": "c-u"})
    assert "c-u" not in s.components
    assert unmount_called == ["yes"]


@pytest.mark.asyncio
async def test_two_sessions_isolated(runtime: LiveRuntime) -> None:
    """Same component, two sessions — state never bleeds across."""
    a = LiveSession("A", FakeWebSocket(), runtime)
    b = LiveSession("B", FakeWebSocket(), runtime)

    await a.handle_message({"t": "attach", "cid": "c", "name": "counter", "props": {"start": 1}})
    await b.handle_message({"t": "attach", "cid": "c", "name": "counter", "props": {"start": 100}})

    await a.handle_message({"t": "event", "cid": "c", "n": "inc"})
    assert a.components["c"].value == 2
    assert b.components["c"].value == 100  # untouched


@pytest.mark.asyncio
async def test_shutdown_runs_unmount_for_every_component(session) -> None:
    s, _ = session
    await s.handle_message({"t": "attach", "cid": "c-1", "name": "counter", "props": {"start": 1}})
    await s.handle_message({"t": "attach", "cid": "c-2", "name": "counter", "props": {"start": 2}})
    assert len(s.components) == 2
    await s.shutdown()
    assert s.components == {}
