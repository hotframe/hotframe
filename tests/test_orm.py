"""Tests for hotframe.orm."""

from hotframe.orm.events import setup_orm_events
from hotframe.orm.transactions import atomic


class TestORM:
    def test_atomic_importable(self):
        assert callable(atomic)

    def test_setup_orm_events_importable(self):
        assert callable(setup_orm_events)
