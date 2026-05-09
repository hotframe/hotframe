"""Tests for hotframe.models."""

from hotframe.models.base import ActiveModel, Base, HubBaseModel, TimeStampedModel
from hotframe.models.mixins import AuditMixin, SoftDeleteMixin, TimestampMixin


class TestBase:
    def test_base_is_declarative(self):
        assert hasattr(Base, "metadata")

    def test_hub_base_model_exists(self):
        assert HubBaseModel is not None

    def test_timestamped_model_exists(self):
        assert TimeStampedModel is not None

    def test_active_model_exists(self):
        assert ActiveModel is not None


class TestMixins:
    def test_timestamp_mixin(self):
        assert TimestampMixin is not None

    def test_soft_delete_mixin(self):
        assert SoftDeleteMixin is not None

    def test_audit_mixin(self):
        assert AuditMixin is not None
