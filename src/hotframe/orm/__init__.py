"""
orm — SQLAlchemy session utilities, ORM event hooks, and PG NOTIFY bridge.

``atomic`` is an async context manager that wraps a savepoint-aware
transaction with automatic rollback. ``on_commit`` schedules a callback
to run after the current transaction commits. ``setup_orm_events`` wires
SQLAlchemy ``after_flush`` listeners so that model mutations automatically
emit events onto the ``AsyncEventBus``. ``PgNotifyBridge`` listens on
PostgreSQL LISTEN/NOTIFY channels and forwards payloads to the bus.

Key exports::

    from hotframe.orm.transactions import atomic, on_commit
    from hotframe.orm.events import setup_orm_events
    from hotframe.orm.listeners import PgNotifyBridge

Usage::

    async with atomic(session):
        session.add(obj)
"""
