"""
BaseRepository — typed CRUD layer on top of HubQuery.

Provides standard list/get/create/update/delete operations with
automatic hub_id scoping, soft-delete, text search, and serialization.

Usage::

    repo = BaseRepository(Product, db, hub_id, search_fields=["name", "sku"])
    result = await repo.list(search="cam", limit=20, is_active=True)
    product = await repo.create(name="Camiseta", sku="CAM-001", price=19.99)
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import or_

if TYPE_CHECKING:
    from hotframe.db.protocols import ISession

from hotframe.models.queryset import HubQuery


class BaseRepository[T]:
    """
    Generic repository for hub-scoped models.

    Wraps HubQuery with higher-level CRUD methods and
    consistent serialization for AI tool responses.
    """

    def __init__(
        self,
        model: type[T],
        db: ISession,
        hub_id: UUID,
        *,
        search_fields: list[str] | None = None,
        default_order: str = "created_at",
    ) -> None:
        self.model = model
        self.db = db
        self.hub_id = hub_id
        self.search_fields = search_fields or []
        self.default_order = default_order

    def q(self) -> HubQuery[T]:
        """Create a HubQuery for the repository's model."""
        return HubQuery(self.model, self.db, self.hub_id)

    async def list(
        self,
        *,
        search: str | None = None,
        order_by: str | Any | None = None,
        limit: int = 50,
        offset: int = 0,
        options: Sequence[Any] | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        """
        List records with optional text search and field filters.

        Returns ``{"items": [...], "total": int}``.
        """
        query = self.q()

        if search and self.search_fields:
            conditions = [
                getattr(self.model, f).ilike(f"%{search}%")
                for f in self.search_fields
                if hasattr(self.model, f)
            ]
            if conditions:
                query = query.filter(or_(*conditions))

        for field, value in filters.items():
            if hasattr(self.model, field) and value is not None:
                query = query.filter(getattr(self.model, field) == value)

        if options:
            query = query.options(*options)

        total = await query.count()

        order = order_by or self.default_order
        if isinstance(order, str):
            col = getattr(self.model, order, None)
            if col is not None:
                query = query.order_by(col)
        else:
            query = query.order_by(order)

        items = await query.offset(offset).limit(limit).all()
        return {"items": items, "total": total}

    async def get(self, id: UUID, *, options: Sequence[Any] | None = None) -> T | None:
        """Get a single record by primary key."""
        query = self.q()
        if options:
            query = query.options(*options)
        return await query.get(id)

    async def create(self, **kwargs: Any) -> T:
        """Create a new record."""
        # ``self.model`` is a SQLAlchemy declarative class — its ``__init__``
        # is dynamic and accepts column kwargs. Mypy sees the abstract
        # ``type[T]`` only.
        instance = self.model(hub_id=self.hub_id, **kwargs)  # type: ignore[call-arg]
        self.db.add(instance)
        await self.db.flush()
        return instance

    async def update(self, id: UUID, **kwargs: Any) -> T | None:
        """Update a record by primary key."""
        instance = await self.get(id)
        if instance is None:
            return None
        for key, value in kwargs.items():
            if hasattr(instance, key):
                setattr(instance, key, value)
        await self.db.flush()
        return instance

    async def delete(self, id: UUID) -> bool:
        """Soft-delete a record by primary key."""
        return await self.q().delete(id)

    async def hard_delete(self, id: UUID) -> bool:
        """Permanently delete a record by primary key."""
        return await self.q().hard_delete(id)

    async def count(self, **filters: Any) -> int:
        """Return the count of matching records."""
        query = self.q()
        for field, value in filters.items():
            if hasattr(self.model, field) and value is not None:
                query = query.filter(getattr(self.model, field) == value)
        return await query.count()

    async def exists(self, **filters: Any) -> bool:
        """Return True if any matching record exists."""
        query = self.q()
        for field, value in filters.items():
            if hasattr(self.model, field) and value is not None:
                query = query.filter(getattr(self.model, field) == value)
        return await query.exists()


def serialize(
    obj: Any, *, fields: list[str] | None = None, exclude: set[str] | None = None
) -> dict[str, Any]:
    """
    Serialize an ORM object to a dict suitable for AI tool responses.

    Handles UUID→str, Decimal→str, datetime→isoformat, date→isoformat.
    """
    exclude = exclude or set()

    if fields is None:
        if hasattr(obj, "__table__"):
            fields = [c.key for c in obj.__table__.columns if c.key not in exclude]
        else:
            return {}

    result: dict[str, Any] = {}
    for attr in fields:
        if attr in exclude:
            continue
        val = getattr(obj, attr, None)
        if isinstance(val, (uuid.UUID, Decimal)):
            val = str(val)
        elif isinstance(val, (datetime, date)):
            val = val.isoformat()
        result[attr] = val
    return result


def serialize_list(
    items: list[Any],
    *,
    fields: list[str] | None = None,
    exclude: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Serialize a list of ORM objects."""
    return [serialize(item, fields=fields, exclude=exclude) for item in items]
