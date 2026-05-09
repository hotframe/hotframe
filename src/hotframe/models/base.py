"""
SQLAlchemy declarative base and abstract model classes.

Provides Base, Model, TimeStampedModel, and ActiveModel.
All use SQLAlchemy 2.0 mapped_column style with UUID primary keys.

Projects extend ``Model`` in their ``apps/shared/models.py`` to add
project-specific fields (e.g. tenant_id, audit fields, soft delete).

Usage::

    from hotframe import Model

    class Product(Model):
        __tablename__ = "products"
        name: Mapped[str] = mapped_column(String(200))
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Root declarative base for all models."""

    pass


class Model(Base):
    """Generic abstract base model with UUID primary key and timestamps.

    This is the recommended base for all models. Projects that need
    additional fields (tenant_id, audit, soft-delete) should create
    their own base in ``apps/shared/models.py`` extending this class.

    Usage::

        from hotframe import Model

        class Article(Model):
            __tablename__ = "articles"
            title: Mapped[str] = mapped_column(String(200))
    """

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


# Backward compatibility alias
HubBaseModel = Model


class TimeStampedModel(Base):
    """Abstract base with only timestamps (created_at, updated_at)."""

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ActiveModel(Base):
    """Abstract base with timestamps + is_active flag."""

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
    )
