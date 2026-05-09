# SPDX-License-Identifier: Apache-2.0
"""
Built-in module state model.

Used when ``settings.MODULE_STATE_MODEL`` is not set.
Applications can provide their own model (e.g. with tenant fields).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from hotframe.models.base import Base


class Module(Base):
    """Tracks installed module state in the database."""

    __tablename__ = "hotframe_module"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    module_id: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        index=True,
    )
    version: Mapped[str] = mapped_column(String(50), default="0.0.0")
    status: Mapped[str] = mapped_column(
        String(20),
        default="installing",
        index=True,
    )
    checksum_sha256: Mapped[str] = mapped_column(String(64), default="")
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(default=False)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
