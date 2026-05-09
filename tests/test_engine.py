"""Tests for hotframe.engine."""

import inspect

import pytest

from hotframe.engine.dependency import (
    DependencyManager,
    _parse_dep,
    _version_satisfies,
)
from hotframe.engine.import_manager import ImportManager
from hotframe.engine.module_runtime import ModuleRuntime
from hotframe.engine.pipeline import HotMountPipeline
from hotframe.engine.state import ModuleStateDB


class TestDependencyParsing:
    def test_simple_module(self):
        mid, op, ver = _parse_dep("sales")
        assert mid == "sales"
        assert op is None
        assert ver is None

    def test_with_version(self):
        mid, op, ver = _parse_dep("sales>=1.0.0")
        assert mid == "sales"
        assert op == ">="
        assert ver == "1.0.0"

    def test_exact_version(self):
        mid, op, ver = _parse_dep("inventory==2.1.0")
        assert mid == "inventory"
        assert op == "=="
        assert ver == "2.1.0"


class TestVersionSatisfies:
    def test_gte(self):
        assert _version_satisfies("1.2.0", ">=", "1.0.0") is True
        assert _version_satisfies("1.0.0", ">=", "1.0.0") is True
        assert _version_satisfies("0.9.0", ">=", "1.0.0") is False

    def test_eq(self):
        assert _version_satisfies("1.0.0", "==", "1.0.0") is True
        assert _version_satisfies("1.0.1", "==", "1.0.0") is False

    def test_lt(self):
        assert _version_satisfies("0.9.0", "<", "1.0.0") is True
        assert _version_satisfies("1.0.0", "<", "1.0.0") is False


class TestDependencyManager:
    def test_resolve_load_order_no_deps(self):
        dm = DependencyManager()
        modules = [
            {"module_id": "a", "manifest": {"dependencies": []}},
            {"module_id": "b", "manifest": {"dependencies": []}},
        ]
        ordered = dm.resolve_load_order(modules)
        assert len(ordered) == 2

    def test_resolve_load_order_with_deps(self):
        dm = DependencyManager()
        modules = [
            {"module_id": "b", "manifest": {"dependencies": ["a"]}},
            {"module_id": "a", "manifest": {"dependencies": []}},
        ]
        ordered = dm.resolve_load_order(modules)
        ids = [m["module_id"] for m in ordered]
        assert ids.index("a") < ids.index("b")

    def test_resolve_load_order_missing_dep(self):
        dm = DependencyManager()
        modules = [
            {"module_id": "a", "manifest": {"dependencies": ["missing"]}},
        ]
        ordered = dm.resolve_load_order(modules)
        assert len(ordered) == 0  # excluded because dep is missing

    def test_cycle_detection(self):
        dm = DependencyManager()
        modules = [
            {"module_id": "a", "manifest": {"dependencies": ["b"]}},
            {"module_id": "b", "manifest": {"dependencies": ["a"]}},
        ]
        ordered = dm.resolve_load_order(modules)
        assert len(ordered) == 0  # both excluded due to cycle


class TestEngineImports:
    def test_pipeline(self):
        assert HotMountPipeline is not None

    def test_import_manager(self):
        assert ImportManager is not None


class TestModuleStateDBSignature:
    """
    Regression: ``ModuleRuntime`` callers used to invoke
    ``state.get_module(session, hub_id, module_id)`` — ModuleStateDB.get_module
    only accepts ``(session, module_id, **filters)`` so that pattern raised
    ``TypeError: get_module() takes 3 positional arguments but 4 were given``
    and broke marketplace install. Lock the contract.
    """

    def test_get_module_takes_two_required_positionals(self):
        sig = inspect.signature(ModuleStateDB.get_module)
        params = list(sig.parameters.values())[1:]  # drop ``self``
        # session + module_id are required positional params; everything
        # else (e.g. hub_id) must be passed as a keyword argument.
        assert params[0].name == "session"
        assert params[1].name == "module_id"
        # The trailing **filters slot is what receives ``hub_id``.
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params)

    def test_runtime_callers_use_keyword_for_hub_id(self):
        """``module_runtime.py`` must never pass ``hub_id`` positionally."""
        from pathlib import Path

        runtime_src = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "hotframe"
            / "engine"
            / "module_runtime.py"
        ).read_text(encoding="utf-8")
        # The exact pattern that previously triggered the TypeError.
        assert "self.state.get_module(session, hub_id, module_id)" not in runtime_src, (
            "module_runtime.py reverted to the broken positional get_module() call"
        )


class TestBootAllActiveModules:
    """
    Regression: ``ModuleRuntime`` was built with ``boot(session, hub_id)`` but
    never wired into the FastAPI lifespan. As a result, modules persisted with
    ``status='active'`` in the DB survived container restarts in data only —
    their routes at ``/m/<module_id>/`` returned 404 until the user manually
    re-activated them from the marketplace UI.

    ``boot_all_active_modules`` is the lifespan-facing entrypoint that loops
    over every distinct ``hub_id`` with active rows and calls ``boot`` for
    each. Keep the public contract of that method locked down.
    """

    def test_method_exists(self):
        assert hasattr(ModuleRuntime, "boot_all_active_modules"), (
            "ModuleRuntime must expose boot_all_active_modules so the "
            "FastAPI lifespan can re-mount routers for DB-active modules"
        )

    def test_method_is_async_and_takes_session(self):
        method = ModuleRuntime.boot_all_active_modules
        assert inspect.iscoroutinefunction(method)
        sig = inspect.signature(method)
        params = list(sig.parameters.values())[1:]  # drop ``self``
        assert params[0].name == "session", (
            "boot_all_active_modules(session) signature changed — the lifespan "
            "caller in bootstrap.py passes a positional session"
        )

    def test_bootstrap_lifespan_calls_boot_all_active_modules(self):
        """The bootstrap lifespan must invoke boot_all_active_modules.

        Without this call, DB-active modules persist but their routes are
        never mounted after a restart. This guard fails loud if somebody
        ever removes the wiring while refactoring bootstrap.py.
        """
        from pathlib import Path

        bootstrap_src = (
            Path(__file__).resolve().parent.parent / "src" / "hotframe" / "bootstrap.py"
        ).read_text(encoding="utf-8")
        assert "boot_all_active_modules" in bootstrap_src, (
            "bootstrap.py lifespan no longer calls "
            "runtime.boot_all_active_modules — active modules will return 404"
        )

    @pytest.mark.asyncio
    async def test_no_active_modules_returns_zero(self):
        """With an empty DB the method must short-circuit to zero, not crash."""
        from unittest.mock import AsyncMock, MagicMock

        runtime = ModuleRuntime.__new__(ModuleRuntime)
        runtime.state = MagicMock()
        runtime.state._model = MagicMock(return_value=type("M", (), {"__dict__": {}}))
        runtime.state.get_active_modules = AsyncMock(return_value=[])
        session = MagicMock()

        count = await runtime.boot_all_active_modules(session)
        assert count == 0


class TestBootAdvisoryLock:
    """
    Regression: when uvicorn runs with ``--workers N``, every worker races
    to ``UPDATE hub_module SET manifest=…`` during boot and Postgres raises
    ``DeadlockDetectedError``, flipping random modules to ``status='error'``.

    ``boot_all_active_modules`` now guards the DB-write side with a Postgres
    session-level advisory lock per hub — only the worker that acquires the
    lock performs DB writes, everyone else still mounts routes locally but
    skips the ``UPDATE``/``set_error`` calls. Lock these invariants.
    """

    def test_boot_accepts_skip_db_writes_flag(self):
        """boot() must expose the skip_db_writes knob used by followers."""
        sig = inspect.signature(ModuleRuntime.boot)
        assert "skip_db_writes" in sig.parameters, (
            "ModuleRuntime.boot(skip_db_writes=...) is required so losing "
            "workers can mount routes without racing into a Postgres "
            "deadlock on hub_module.manifest"
        )
        # Must default to False so single-worker deployments keep writing.
        assert sig.parameters["skip_db_writes"].default is False

    def test_try_acquire_boot_lock_is_async(self):
        assert inspect.iscoroutinefunction(ModuleRuntime._try_acquire_boot_lock)

    @pytest.mark.asyncio
    async def test_lock_noop_on_non_postgres(self):
        """On SQLite / fake sessions the lock helper must return True (no-op)."""
        from unittest.mock import MagicMock

        runtime = ModuleRuntime.__new__(ModuleRuntime)
        session = MagicMock()
        fake_bind = MagicMock()
        fake_bind.dialect.name = "sqlite"
        session.get_bind.return_value = fake_bind

        acquired = await runtime._try_acquire_boot_lock(session, hub_id=None)
        assert acquired is True

    @pytest.mark.asyncio
    async def test_lock_follower_skips_db_writes(self):
        """
        When pg_try_advisory_xact_lock returns False (another worker holds
        the lock), boot() must be invoked with skip_db_writes=True so this
        worker mounts routes locally but never issues an UPDATE against
        ``hub_module`` — the exact pattern that was deadlocking Postgres.
        """
        from unittest.mock import AsyncMock, MagicMock

        runtime = ModuleRuntime.__new__(ModuleRuntime)

        # Single-tenant model (no hub_id column), one active module.
        Model = type("M", (), {"__dict__": {}})
        runtime.state = MagicMock()
        runtime.state._model = MagicMock(return_value=Model)
        runtime.state.get_active_modules = AsyncMock(return_value=[MagicMock()])

        # Simulate pg_try_advisory_xact_lock returning False (lock lost).
        runtime._try_acquire_boot_lock = AsyncMock(return_value=False)
        runtime.boot = AsyncMock()

        await runtime.boot_all_active_modules(MagicMock())

        assert runtime.boot.await_count == 1
        call_kwargs = runtime.boot.await_args.kwargs
        assert call_kwargs.get("skip_db_writes") is True, (
            "Follower worker must pass skip_db_writes=True to boot() so it "
            "does not race the leader worker's UPDATE on hub_module"
        )

    @pytest.mark.asyncio
    async def test_lock_leader_writes_to_db(self):
        """When the lock is acquired, boot() runs with skip_db_writes=False."""
        from unittest.mock import AsyncMock, MagicMock

        runtime = ModuleRuntime.__new__(ModuleRuntime)
        Model = type("M", (), {"__dict__": {}})
        runtime.state = MagicMock()
        runtime.state._model = MagicMock(return_value=Model)
        runtime.state.get_active_modules = AsyncMock(return_value=[MagicMock()])
        runtime._try_acquire_boot_lock = AsyncMock(return_value=True)
        runtime.boot = AsyncMock()

        await runtime.boot_all_active_modules(MagicMock())

        call_kwargs = runtime.boot.await_args.kwargs
        assert call_kwargs.get("skip_db_writes") is False

    def test_advisory_key_is_stable_across_processes(self):
        """
        The advisory key MUST be deterministic across worker processes —
        Python's built-in hash() is salted per-process, which would give
        each worker a different key and defeat the lock. Guard against
        regressing back to hash().
        """
        from hotframe.engine.module_runtime import _hub_id_to_advisory_key

        k1 = _hub_id_to_advisory_key("00000000-0000-0000-0000-000000000001")
        k2 = _hub_id_to_advisory_key("00000000-0000-0000-0000-000000000001")
        assert k1 == k2
        # Different hub ids → different keys (overwhelmingly likely).
        k3 = _hub_id_to_advisory_key("00000000-0000-0000-0000-000000000002")
        assert k1 != k3
        # Must fit signed 64-bit range (Postgres bigint).
        assert -(2**63) <= k1 < 2**63
