"""
SingletonMixin — ensures one row per hub_id for configuration-style models.

Usage:
    class ShopConfig(Base, SingletonMixin, HubMixin, TimestampMixin):
        __tablename__ = "shop_config"
        shop_name: Mapped[str] = mapped_column(String(255), default="My Shop")

    config = await ShopConfig.get_config(session, hub_id)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self
from uuid import UUID

from sqlalchemy import select

if TYPE_CHECKING:
    from hotframe.db.protocols import ISession


class SingletonMixin:
    """
    Mixin that provides a get_config() classmethod returning exactly
    one instance per hub_id, creating it on first access.

    The model must also have a hub_id column (use HubMixin).
    """

    @classmethod
    async def get_config(cls, session: ISession, hub_id: UUID) -> Self:
        """
        Get or create the singleton instance for this hub_id.

        Uses SELECT first, then INSERT if not found, with a flush
        to persist immediately. Safe for concurrent access when
        combined with a unique constraint on hub_id.
        """
        stmt = select(cls).where(cls.hub_id == hub_id).limit(1)  # type: ignore[attr-defined]
        result = await session.execute(stmt)
        instance = result.scalars().first()

        if instance is not None:
            return instance  # type: ignore[return-value]

        # Create new instance with hub_id
        instance = cls(hub_id=hub_id)  # type: ignore[call-arg]
        session.add(instance)
        await session.flush()
        return instance  # type: ignore[return-value]
