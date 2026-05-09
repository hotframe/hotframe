"""
Composable SQLAlchemy mixins for model columns.

Use these when you need fine-grained control over which columns
a model gets, instead of inheriting from HubBaseModel.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Uuid, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class HubMixin:
    """Adds hub_id column for hub-scoped models."""

    @declared_attr
    def hub_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(Uuid, nullable=False, index=True)


class TimestampMixin:
    """Adds created_at and updated_at columns."""

    @declared_attr
    def created_at(cls) -> Mapped[datetime]:
        return mapped_column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        )

    @declared_attr
    def updated_at(cls) -> Mapped[datetime]:
        return mapped_column(
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
            onupdate=func.now(),
        )


class AuditMixin:
    """Adds created_by and updated_by UUID columns."""

    @declared_attr
    def created_by(cls) -> Mapped[uuid.UUID | None]:
        return mapped_column(Uuid, nullable=True)

    @declared_attr
    def updated_by(cls) -> Mapped[uuid.UUID | None]:
        return mapped_column(Uuid, nullable=True)


class SoftDeleteMixin:
    """Adds is_deleted flag and deleted_at timestamp."""

    @declared_attr
    def is_deleted(cls) -> Mapped[bool]:
        return mapped_column(
            Boolean,
            default=False,
            server_default="false",
            index=True,
        )

    @declared_attr
    def deleted_at(cls) -> Mapped[datetime | None]:
        return mapped_column(DateTime(timezone=True), nullable=True)
