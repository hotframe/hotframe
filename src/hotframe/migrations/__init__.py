"""
migrations — Alembic migration runners for Hub's multi-namespace schema.

``ModuleMigrationRunner`` runs Alembic migrations for a single module
namespace (its own ``versions/`` directory). ``MultiNamespaceRunner``
orchestrates migrations across all installed modules in the correct order,
collecting per-namespace ``MigrationReport`` results. Used by the
``hub migrate`` CLI command and by ``ModuleRuntime.install``.

Key exports::

    from hotframe.migrations.runner import ModuleMigrationRunner
    from hotframe.migrations.multi_namespace import MultiNamespaceRunner, MigrationReport

Usage::

    runner = MultiNamespaceRunner(engine, modules_dir=Path("/app/modules"))
    reports = await runner.run_all()
"""
