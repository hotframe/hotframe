"""
db — low-level database primitives and abstract persistence protocols.

Provides ``SingletonMixin`` (ensures exactly one DB row per class via an
upsert pattern), custom SQLAlchemy column types (``EncryptedString``,
``EncryptedText``), and the abstract persistence protocols (``ISession``,
``IQueryBuilder``, ``IRepository``) that decouple application code from
any specific ORM.

Key exports::

    from hotframe.db.protocols import ISession, IQueryBuilder, IRepository
    from hotframe.db.singletons import SingletonMixin
    from hotframe.db.types import EncryptedString, EncryptedText
"""

from hotframe.db.protocols import (
    IExecuteResult,
    IQueryBuilder,
    IRepository,
    IScalarResult,
    ISession,
)

__all__ = [
    "IExecuteResult",
    "IQueryBuilder",
    "IRepository",
    "IScalarResult",
    "ISession",
]
