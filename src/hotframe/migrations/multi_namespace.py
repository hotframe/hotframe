"""
Multi-namespace Alembic runner.

Each app (and each dynamic module) keeps its own migrations/ directory
with its own alembic_<name>_version table. This module orchestrates
running them in the correct order:

  core (migrations/)
  apps/<app1>/migrations/
  apps/<app2>/migrations/
  modules/<mod1>/migrations/   (handled by ModuleMigrationRunner)
  ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

logger = logging.getLogger(__name__)


@dataclass
class MigrationNamespace:
    """One Alembic migration namespace."""

    name: str  # e.g. "core" or "accounts"
    script_location: Path  # path to migrations/ dir
    version_table: str  # alembic version table name

    @classmethod
    def core(cls, root: Path) -> MigrationNamespace:
        return cls(
            name="core",
            script_location=root / "migrations",
            version_table="alembic_version",
        )

    @classmethod
    def for_app(cls, apps_root: Path, app_name: str) -> MigrationNamespace:
        return cls(
            name=app_name,
            script_location=apps_root / app_name / "migrations",
            version_table=f"alembic_{app_name}_version",
        )


@dataclass
class MigrationReport:
    namespace: str
    applied: bool = False
    skipped: bool = False
    reason: str | None = None
    error: str | None = None


class MultiNamespaceRunner:
    """Run Alembic migrations across multiple namespaces."""

    def __init__(self, db_url: str, project_root: Path) -> None:
        self.db_url = db_url
        self.project_root = project_root

    def discover_namespaces(self) -> list[MigrationNamespace]:
        """Return core + one namespace per apps/<name>/migrations/ found."""
        namespaces = [MigrationNamespace.core(self.project_root)]
        apps_root = self.project_root / "apps"
        if apps_root.exists():
            for sub in sorted(apps_root.iterdir()):
                if not sub.is_dir():
                    continue
                env_py = sub / "migrations" / "env.py"
                versions_dir = sub / "migrations" / "versions"
                if env_py.exists() and versions_dir.is_dir():
                    namespaces.append(MigrationNamespace.for_app(apps_root, sub.name))
        return namespaces

    def build_alembic_config(self, ns: MigrationNamespace) -> AlembicConfig:
        cfg = AlembicConfig()
        cfg.set_main_option("script_location", str(ns.script_location))
        cfg.set_main_option("sqlalchemy.url", self.db_url)
        # Pass version_table via attributes (read by env.py)
        cfg.attributes["version_table"] = ns.version_table
        cfg.attributes["namespace_name"] = ns.name
        return cfg

    def upgrade(
        self, namespace: str | None = None, revision: str = "head"
    ) -> list[MigrationReport]:
        """Upgrade one namespace (or all if None)."""
        namespaces = self.discover_namespaces()
        if namespace is not None:
            namespaces = [n for n in namespaces if n.name == namespace]
            if not namespaces:
                return [MigrationReport(namespace=namespace, error="namespace not found")]

        reports: list[MigrationReport] = []
        for ns in namespaces:
            cfg = self.build_alembic_config(ns)
            try:
                alembic_command.upgrade(cfg, revision)
                reports.append(MigrationReport(namespace=ns.name, applied=True))
            except Exception as e:
                logger.exception("Migration failed for namespace %s", ns.name)
                reports.append(MigrationReport(namespace=ns.name, error=str(e)))
        return reports

    def current(self, namespace: str | None = None) -> dict[str, str | None]:
        """Return current revision for each namespace."""
        from sqlalchemy import create_engine, text

        result: dict[str, str | None] = {}
        namespaces = self.discover_namespaces()
        if namespace is not None:
            namespaces = [n for n in namespaces if n.name == namespace]

        # Use sync engine for introspection
        sync_url = self.db_url.replace("+aiosqlite", "").replace("+asyncpg", "")
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                for ns in namespaces:
                    try:
                        row = conn.execute(
                            text(f"SELECT version_num FROM {ns.version_table} LIMIT 1")
                        ).fetchone()
                        result[ns.name] = row[0] if row else None
                    except Exception:
                        result[ns.name] = None  # table doesn't exist yet
        finally:
            engine.dispose()
        return result
