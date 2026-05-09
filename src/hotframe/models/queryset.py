"""
HubQuery — a query builder that auto-filters by hub_id and excludes soft-deleted records.

Usage:
    q = HubQuery(MyModel, session, hub_id)
    items = await q.filter(MyModel.status == "active").order_by(MyModel.name).all()
    item = await q.get(item_id)
    total = await q.filter(MyModel.category == "A").count()
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from sqlalchemy import Select, func, select

if TYPE_CHECKING:
    from hotframe.db.protocols import ISession


class HubQuery[T]:
    """
    Chainable async query builder scoped to a hub_id.

    Automatically filters by hub_id and excludes soft-deleted records
    (if the model has is_deleted). Call .with_deleted() to include them.
    """

    def __init__(self, model: type[T], session: ISession, hub_id: UUID) -> None:
        # ``model`` carries SQLAlchemy column descriptors that mypy cannot
        # see through ``type[T]``. We keep the public ``T`` for the return
        # types of ``all()``/``first()``/``get()`` but store the class as
        # ``Any`` internally so column accesses (Model.hub_id, Model.id)
        # type-check.
        self._model: Any = model
        self._session = session
        self._hub_id = hub_id
        self._conditions: list[Any] = []
        self._order: list[Any] = []
        self._load_options: list[Any] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._include_deleted: bool = False

    def _base_query(self) -> Select[tuple[T]]:
        stmt = select(self._model)

        # Auto-filter by hub_id if the model has it
        if hasattr(self._model, "hub_id"):
            stmt = stmt.where(self._model.hub_id == self._hub_id)

        # Exclude soft-deleted unless explicitly requested
        if not self._include_deleted and hasattr(self._model, "is_deleted"):
            stmt = stmt.where(self._model.is_deleted == False)  # noqa: E712

        for cond in self._conditions:
            stmt = stmt.where(cond)

        for opt in self._load_options:
            stmt = stmt.options(opt)

        if self._order:
            stmt = stmt.order_by(*self._order)

        if self._limit is not None:
            stmt = stmt.limit(self._limit)

        if self._offset is not None:
            stmt = stmt.offset(self._offset)

        return stmt

    def filter(self, *conditions: Any) -> Self:
        """Apply a WHERE clause."""
        self._conditions.extend(conditions)
        return self

    def order_by(self, *columns: Any) -> Self:
        """Set ORDER BY clause."""
        self._order.extend(columns)
        return self

    def options(self, *opts: Any) -> Self:
        """Add SQLAlchemy query options (e.g. selectinload)."""
        self._load_options.extend(opts)
        return self

    def limit(self, n: int) -> Self:
        """Set LIMIT."""
        self._limit = n
        return self

    def offset(self, n: int) -> Self:
        """Set OFFSET."""
        self._offset = n
        return self

    def with_deleted(self) -> Self:
        """Include soft-deleted records."""
        self._include_deleted = True
        return self

    async def all(self) -> list[T]:
        """Execute query and return all results."""
        result = await self._session.execute(self._base_query())
        return list(result.scalars().all())

    async def first(self) -> T | None:
        """Execute query and return the first result or None."""
        stmt = self._base_query().limit(1)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def get(self, id: UUID) -> T | None:
        """Get a single record by primary key."""
        stmt = self._base_query().where(self._model.id == id)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    def _filtered_select(self, *columns: Any) -> Select:
        """Build a SELECT with hub_id, soft-delete, and user filters applied directly (no subquery)."""
        stmt = select(*columns)

        if hasattr(self._model, "hub_id"):
            stmt = stmt.where(self._model.hub_id == self._hub_id)

        if not self._include_deleted and hasattr(self._model, "is_deleted"):
            stmt = stmt.where(self._model.is_deleted == False)  # noqa: E712

        for cond in self._conditions:
            stmt = stmt.where(cond)

        return stmt

    async def count(self) -> int:
        """Return the count of matching records."""
        stmt = self._filtered_select(func.count(self._model.id))
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def sum(self, column: Any) -> Decimal:
        """Return the sum of a column."""
        col = getattr(self._model, column) if isinstance(column, str) else column
        stmt = self._filtered_select(func.coalesce(func.sum(col), 0))
        result = await self._session.execute(stmt)
        return Decimal(str(result.scalar_one()))

    async def exists(self) -> bool:
        """Return True if any matching record exists."""
        stmt = self._filtered_select(self._model.id).limit(1)
        result = await self._session.execute(stmt)
        return result.first() is not None

    async def get_or_create(self, **defaults: Any) -> tuple[T, bool]:
        """
        Get an existing instance matching the current filters, or create one.

        The `defaults` dict provides values for creating a new instance
        (merged with hub_id and any filter column values).
        Returns (instance, created) where created is True if a new row was inserted.

        Handles the race condition where another request may insert the same
        row between our SELECT and INSERT by catching IntegrityError.
        """
        from sqlalchemy.exc import IntegrityError

        instance = await self.first()
        if instance is not None:
            return instance, False

        # Build creation kwargs
        create_kwargs: dict[str, Any] = {"hub_id": self._hub_id, **defaults}
        instance = self._model(**create_kwargs)
        self._session.add(instance)
        try:
            await self._session.flush()
        except IntegrityError:
            # Another request won the race — rollback and re-query
            await self._session.rollback()
            instance = await self.first()
            if instance is not None:
                return instance, False
            raise  # Genuine constraint violation, not a race
        return instance, True

    async def delete(self, id: UUID) -> bool:
        """Soft-delete a record by id. Returns True if found and deleted."""
        instance = await self.get(id)
        if instance is None:
            return False

        if hasattr(instance, "is_deleted"):
            instance.is_deleted = True  # type: ignore[attr-defined]
            instance.deleted_at = datetime.now(UTC)  # type: ignore[attr-defined]
            await self._session.flush()
            return True

        # Model doesn't support soft delete — fall back to hard delete
        import logging

        _logger = logging.getLogger(__name__)
        _logger.warning(
            "Hard delete on %s (id=%s) — consider adding SoftDeleteMixin",
            type(instance).__name__,
            id,
        )
        await self._session.delete(instance)
        await self._session.flush()
        return True

    async def hard_delete(self, id: UUID) -> bool:
        """Permanently delete a record by id. Returns True if found and deleted."""
        # Use with_deleted to find even soft-deleted records
        stmt = select(self._model).where(self._model.id == id)
        if hasattr(self._model, "hub_id"):
            stmt = stmt.where(self._model.hub_id == self._hub_id)
        result = await self._session.execute(stmt)
        instance = result.scalars().first()
        if instance is None:
            return False

        await self._session.delete(instance)
        await self._session.flush()
        return True
