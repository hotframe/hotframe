# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the BaseModuleService convenience helpers.

These helpers (``success``, ``error``, ``parse_uuid``, ``parse_date``,
``parse_decimal``, ``get_or_none``, ``get_or_error``, ``self.atomic``)
are added to shrink the per-module ``services.py`` boilerplate. They are
intentionally pure-Python where possible so we can test them without a
running app or database — the DB-touching ``get_or_*`` helpers use a
fake query builder.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from hotframe.apps.service_facade import ModuleService


def _service() -> ModuleService:
    """Build a ModuleService with mocked dependencies."""
    db = MagicMock()
    return ModuleService(db=db, hub_id=uuid4())


# ---------------------------------------------------------------------------
# Response shape helpers
# ---------------------------------------------------------------------------


class TestResponseHelpers:
    def test_success_includes_ok_true(self):
        assert ModuleService.success() == {"ok": True}

    def test_success_merges_fields(self):
        result = ModuleService.success(id="abc", created=True)
        assert result == {"ok": True, "id": "abc", "created": True}

    def test_error_minimal(self):
        assert ModuleService.error("bad") == {"ok": False, "error": "bad"}

    def test_error_with_code(self):
        assert ModuleService.error("nope", code="not_found") == {
            "ok": False,
            "error": "nope",
            "code": "not_found",
        }

    def test_error_passes_extra_fields(self):
        result = ModuleService.error("bad", code="x", field="duration_minutes")
        assert result == {
            "ok": False,
            "error": "bad",
            "code": "x",
            "field": "duration_minutes",
        }


# ---------------------------------------------------------------------------
# parse_uuid
# ---------------------------------------------------------------------------


class TestParseUuid:
    def test_none_returns_none(self):
        assert ModuleService.parse_uuid(None) is None

    def test_empty_string_returns_none(self):
        assert ModuleService.parse_uuid("") is None

    def test_valid_string_returns_uuid(self):
        u = uuid4()
        assert ModuleService.parse_uuid(str(u)) == u

    def test_uuid_passthrough(self):
        u = uuid4()
        assert ModuleService.parse_uuid(u) == u

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            ModuleService.parse_uuid("not-a-uuid")


# ---------------------------------------------------------------------------
# parse_date
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_none_returns_none(self):
        assert ModuleService.parse_date(None) is None

    def test_empty_returns_none(self):
        assert ModuleService.parse_date("") is None

    def test_iso_date(self):
        assert ModuleService.parse_date("2026-04-21") == date(2026, 4, 21)

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            ModuleService.parse_date("21/04/2026")

    def test_custom_format(self):
        assert ModuleService.parse_date("21/04/2026", fmt="%d/%m/%Y") == date(
            2026,
            4,
            21,
        )


# ---------------------------------------------------------------------------
# parse_decimal
# ---------------------------------------------------------------------------


class TestParseDecimal:
    def test_none_returns_none(self):
        assert ModuleService.parse_decimal(None) is None

    def test_empty_returns_none(self):
        assert ModuleService.parse_decimal("") is None

    def test_valid(self):
        assert ModuleService.parse_decimal("9.99") == Decimal("9.99")

    def test_negative(self):
        assert ModuleService.parse_decimal("-3.5") == Decimal("-3.5")

    def test_invalid_raises(self):
        from decimal import InvalidOperation

        with pytest.raises(InvalidOperation):
            ModuleService.parse_decimal("nope")


# ---------------------------------------------------------------------------
# get_or_none + get_or_error
# ---------------------------------------------------------------------------


class _FakeQuery:
    """Minimal ``IQueryBuilder`` stand-in: ``get(uuid)`` returns ``self._row``."""

    def __init__(self, row):
        self._row = row

    async def get(self, uid: UUID):
        # Sanity-check the helper passed a real UUID, not a string.
        assert isinstance(uid, UUID)
        return self._row


class _ServiceWithFakeQuery(ModuleService):
    def __init__(self, row) -> None:
        super().__init__(db=MagicMock(), hub_id=uuid4())
        self._fake_row = row

    def q(self, model: type) -> _FakeQuery:  # type: ignore[override]
        return _FakeQuery(self._fake_row)


@pytest.mark.asyncio
async def test_get_or_none_returns_row():
    row = object()
    svc = _ServiceWithFakeQuery(row)
    found = await svc.get_or_none(MagicMock(), str(uuid4()))
    assert found is row


@pytest.mark.asyncio
async def test_get_or_none_empty_id_skips_query():
    svc = _ServiceWithFakeQuery("never-returned")
    assert await svc.get_or_none(MagicMock(), "") is None
    assert await svc.get_or_none(MagicMock(), None) is None


@pytest.mark.asyncio
async def test_get_or_error_success_returns_row_and_no_error():
    row = object()
    svc = _ServiceWithFakeQuery(row)
    found, err = await svc.get_or_error(MagicMock(), str(uuid4()))
    assert found is row
    assert err is None


@pytest.mark.asyncio
async def test_get_or_error_miss_returns_error_dict():
    svc = _ServiceWithFakeQuery(None)
    found, err = await svc.get_or_error(
        MagicMock(),
        str(uuid4()),
        not_found_message="Custom missing",
        code="custom_code",
    )
    assert found is None
    assert err == {"ok": False, "error": "Custom missing", "code": "custom_code"}
