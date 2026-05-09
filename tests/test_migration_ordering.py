# SPDX-License-Identifier: Apache-2.0
"""Regression tests for migration ordering + env_helpers.

Two bugs motivated these tests:

1. ``hf migrate`` used to iterate ``modules/`` alphabetically, causing
   modules with cross-module FKs (e.g. commissions → services_service)
   to fail because the referenced table did not exist yet. The fix
   reads ``DEPENDENCIES`` from each ``module.py`` and topologically
   sorts the module targets.

2. ``hf makemigrations <module>`` failed with ``NoReferencedTableError``
   when the module declared a FK to a table owned by an app or another
   module not yet on ``Base.metadata``. The fix (``env_helpers``) walks
   the dependency graph from ``module.py`` and imports the required
   ``apps/*/models.py`` and ``modules/<dep>/models.py`` before
   autogenerate runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hotframe.management.cli import (
    _extract_module_dependencies,
    _topo_sort_modules,
)
from hotframe.migrations.env_helpers import _read_dependencies


def _write_manifest(module_dir: Path, deps: list[str]) -> None:
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "module.py").write_text(
        f'MODULE_ID = "{module_dir.name}"\nDEPENDENCIES: list[str] = {deps!r}\n'
    )


# ---------------------------------------------------------------------------
# _extract_module_dependencies
# ---------------------------------------------------------------------------


class TestExtractModuleDependencies:
    def test_reads_annotated_assignment(self, tmp_path: Path):
        module_dir = tmp_path / "mod_a"
        _write_manifest(module_dir, ["staff", "sales"])
        assert _extract_module_dependencies(module_dir) == ["staff", "sales"]

    def test_reads_plain_assignment(self, tmp_path: Path):
        module_dir = tmp_path / "mod_b"
        module_dir.mkdir()
        (module_dir / "module.py").write_text('DEPENDENCIES = ["inventory"]\n')
        assert _extract_module_dependencies(module_dir) == ["inventory"]

    def test_missing_manifest_returns_empty(self, tmp_path: Path):
        assert _extract_module_dependencies(tmp_path / "nope") == []

    def test_malformed_manifest_returns_empty(self, tmp_path: Path):
        module_dir = tmp_path / "bad"
        module_dir.mkdir()
        (module_dir / "module.py").write_text("this is not = valid python")
        assert _extract_module_dependencies(module_dir) == []

    def test_missing_dependencies_returns_empty(self, tmp_path: Path):
        module_dir = tmp_path / "no_deps"
        module_dir.mkdir()
        (module_dir / "module.py").write_text('MODULE_ID = "no_deps"\n')
        assert _extract_module_dependencies(module_dir) == []


# ---------------------------------------------------------------------------
# _topo_sort_modules
# ---------------------------------------------------------------------------


class TestTopoSortModules:
    def test_dep_is_migrated_before_dependent(self, tmp_path: Path):
        _write_manifest(tmp_path / "services", [])
        _write_manifest(tmp_path / "commissions", ["services"])
        ordered = _topo_sort_modules(
            [
                ("commissions", tmp_path / "commissions"),
                ("services", tmp_path / "services"),
            ]
        )
        names = [name for name, _ in ordered]
        assert names.index("services") < names.index("commissions")

    def test_transitive_deps_respected(self, tmp_path: Path):
        _write_manifest(tmp_path / "staff", [])
        _write_manifest(tmp_path / "services", [])
        _write_manifest(tmp_path / "appointments", ["staff", "services"])
        _write_manifest(tmp_path / "commissions", ["appointments"])
        ordered = _topo_sort_modules(
            [
                ("commissions", tmp_path / "commissions"),
                ("appointments", tmp_path / "appointments"),
                ("services", tmp_path / "services"),
                ("staff", tmp_path / "staff"),
            ]
        )
        names = [name for name, _ in ordered]
        for dep, dependent in [
            ("staff", "appointments"),
            ("services", "appointments"),
            ("appointments", "commissions"),
        ]:
            assert names.index(dep) < names.index(dependent), (
                f"{dep} must be migrated before {dependent}"
            )

    def test_unknown_deps_are_ignored(self, tmp_path: Path):
        """Deps outside the target set (e.g. removed modules) do not block."""
        _write_manifest(tmp_path / "onlyone", ["ghost_module"])
        ordered = _topo_sort_modules(
            [
                ("onlyone", tmp_path / "onlyone"),
            ]
        )
        assert [name for name, _ in ordered] == ["onlyone"]

    def test_cycle_raises_typer_exit(self, tmp_path: Path):
        import typer

        _write_manifest(tmp_path / "a", ["b"])
        _write_manifest(tmp_path / "b", ["a"])
        with pytest.raises(typer.Exit):
            _topo_sort_modules(
                [
                    ("a", tmp_path / "a"),
                    ("b", tmp_path / "b"),
                ]
            )

    def test_empty_input(self, tmp_path: Path):
        assert _topo_sort_modules([]) == []

    def test_single_module_no_deps(self, tmp_path: Path):
        _write_manifest(tmp_path / "solo", [])
        ordered = _topo_sort_modules([("solo", tmp_path / "solo")])
        assert [name for name, _ in ordered] == ["solo"]


# ---------------------------------------------------------------------------
# env_helpers._read_dependencies (shared with cli._extract_module_dependencies
# but implemented separately to keep migrations/ decoupled from management/)
# ---------------------------------------------------------------------------


class TestReadDependencies:
    def test_matches_cli_extractor(self, tmp_path: Path):
        """The two parsers must agree — they are intentional copies to
        keep ``hotframe.migrations`` importable without pulling in typer.
        """
        module_dir = tmp_path / "mod"
        _write_manifest(module_dir, ["a", "b", "c"])
        cli_result = _extract_module_dependencies(module_dir)
        env_result = _read_dependencies(module_dir / "module.py")
        assert cli_result == env_result == ["a", "b", "c"]
