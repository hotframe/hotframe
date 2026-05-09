# SPDX-License-Identifier: Apache-2.0
"""Tests for the wire protocol envelope helpers."""

from __future__ import annotations

from hotframe.live.protocol import (
    make_err,
    make_nav,
    make_patch,
    make_toast,
)


def test_make_patch_shape() -> None:
    msg = make_patch("c-1", "<div>hi</div>")
    assert msg == {"t": "patch", "cid": "c-1", "html": "<div>hi</div>"}


def test_make_nav_shape() -> None:
    assert make_nav("/done") == {"t": "nav", "url": "/done"}


def test_make_err_default() -> None:
    msg = make_err("c-1", "boom")
    assert msg == {"t": "err", "cid": "c-1", "msg": "boom"}


def test_make_err_with_code() -> None:
    msg = make_err("c-1", "missing", code="not_found")
    assert msg == {"t": "err", "cid": "c-1", "msg": "missing", "code": "not_found"}


def test_make_toast_default_level() -> None:
    msg = make_toast("Saved")
    assert msg == {"t": "toast", "level": "info", "msg": "Saved"}


def test_make_toast_explicit_level() -> None:
    msg = make_toast("Failed", level="error")
    assert msg == {"t": "toast", "level": "error", "msg": "Failed"}
