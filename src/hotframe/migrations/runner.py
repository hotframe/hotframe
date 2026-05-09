"""
Per-module Alembic migration runner.

Each module with ``HAS_MODELS=True`` can ship its own Alembic migrations
directory. The version table is namespaced as ``alembic_{module_id}`` to
avoid collisions between modules and the core schema.

Migration directory structure inside a module::

    modules/{module_id}/
    └── migrations/
        ├── __init__.py
        ├── env.py
        └── versions/
            └── 001_initial.py
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)


class ModuleMigrationRunner:
    """Run Alembic migrations for individual modules."""

    async def upgrade(
        self,
        module_id: str,
        module_path: Path,
        db_url: str,
    ) -> None:
        """
        Run ``alembic upgrade head`` for a module.

        The version table is named ``alembic_{module_id}`` so each module
        tracks its own migration history independently.

        Args:
            module_id: The module identifier.
            module_path: Path to the module directory (must contain ``migrations/``).
            db_url: Synchronous database URL for Alembic
                    (e.g. ``postgresql://...`` without ``+asyncpg``).
        """
        alembic_dir = module_path / "migrations"
        if not alembic_dir.exists():
            logger.debug("No migrations/ directory in %s — skipping migrations", module_path)
            return

        version_table = f"alembic_{module_id}"
        config = self._build_config(module_id, module_path, db_url, version_table)

        logger.info(
            "Running alembic upgrade head for %s (version_table=%s)",
            module_id,
            version_table,
        )

        try:
            # Ensure module parent is in sys.path so env.py can import module models
            import asyncio
            import sys

            parent = str(module_path.parent)
            if parent not in sys.path:
                sys.path.insert(0, parent)

            # Create a sync connection and pass it to env.py via config attributes
            # This avoids env.py trying to create its own engine (which may use
            # async_engine_from_config and fail with sync psycopg2).
            from sqlalchemy import create_engine

            def _run_upgrade():
                from sqlalchemy.pool import NullPool

                engine = create_engine(db_url, poolclass=NullPool)
                # Pass engine (not connection) — env.py does connectable.connect()
                config.attributes["connection"] = engine
                command.upgrade(config, "head")
                engine.dispose()

            await asyncio.to_thread(_run_upgrade)
            logger.info("Migrations complete for %s", module_id)
        except Exception:
            logger.exception("Migration failed for %s", module_id)
            raise

    async def downgrade(
        self,
        module_id: str,
        module_path: Path,
        db_url: str,
    ) -> None:
        """
        Run ``alembic downgrade base`` for a module.

        Reverts ALL migrations for the module. Used during uninstall.

        Args:
            module_id: The module identifier.
            module_path: Path to the module directory.
            db_url: Synchronous database URL for Alembic.
        """
        alembic_dir = module_path / "migrations"
        if not alembic_dir.exists():
            logger.debug("No migrations/ directory in %s — skipping downgrade", module_path)
            return

        version_table = f"alembic_{module_id}"
        config = self._build_config(module_id, module_path, db_url, version_table)

        logger.info(
            "Running alembic downgrade base for %s (version_table=%s)",
            module_id,
            version_table,
        )

        try:
            import asyncio

            await asyncio.to_thread(command.downgrade, config, "base")
            logger.info("Downgrade complete for %s", module_id)
        except Exception:
            logger.exception("Downgrade failed for %s", module_id)
            raise

    def has_migrations(self, module_path: Path) -> bool:
        """Check if a module has a ``migrations/`` directory with migration scripts."""
        alembic_dir = module_path / "migrations"
        if not alembic_dir.exists():
            return False
        versions_dir = alembic_dir / "versions"
        if not versions_dir.exists():
            return False
        # Check for at least one .py migration file
        return any(versions_dir.glob("*.py"))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _build_config(
        module_id: str,
        module_path: Path,
        db_url: str,
        version_table: str,
    ) -> Config:
        """Build an Alembic Config for a specific module."""
        migrations_dir = module_path / "migrations"
        ini_path = migrations_dir / "alembic.ini"

        if ini_path.exists():
            config = Config(str(ini_path))
        else:
            config = Config()

        config.set_main_option("script_location", str(migrations_dir))
        config.set_main_option("sqlalchemy.url", db_url)
        config.set_main_option("version_table", version_table)

        # Make the module path and version_table available to env.py
        # so legacy env.py implementations that read config.attributes can
        # also resolve the per-module Alembic version table.
        config.attributes["module_id"] = module_id
        config.attributes["module_path"] = str(module_path)
        config.attributes["version_table"] = version_table

        return config

    @staticmethod
    def get_sync_db_url(async_url: str) -> str:
        """
        Convert an async DB URL to sync for Alembic.

        ``postgresql+asyncpg://...`` → ``postgresql://...``
        ``sqlite+aiosqlite://...`` → ``sqlite://...``
        """
        url = async_url
        url = url.replace("+asyncpg", "")
        url = url.replace("+aiosqlite", "")
        return url
