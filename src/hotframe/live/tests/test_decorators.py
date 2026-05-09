# SPDX-License-Identifier: Apache-2.0
"""Tests for ``@event`` decorator metadata."""

from __future__ import annotations

import pytest

from hotframe.live.decorators import event, get_event_name


def test_event_marks_function_with_wire_name() -> None:
    @event("toggle")
    async def handler(self) -> None:
        pass

    assert get_event_name(handler) == "toggle"


def test_event_returns_same_callable_object() -> None:
    """The decorator should not wrap; it should stamp and return as-is."""

    async def original(self) -> None:
        pass

    decorated = event("name")(original)
    assert decorated is original


def test_event_rejects_empty_name() -> None:
    with pytest.raises(ValueError):
        event("")  # type: ignore[arg-type]


def test_event_rejects_non_string_name() -> None:
    with pytest.raises(ValueError):
        event(None)  # type: ignore[arg-type]


def test_get_event_name_on_undecorated_returns_none() -> None:
    async def plain(self) -> None:
        pass

    assert get_event_name(plain) is None
