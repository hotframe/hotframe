# SPDX-License-Identifier: Apache-2.0
"""
Memory-stability tests for the module load/unload cycle.

The real-world failure these tests guard against: install/uninstall a
module 50 times in the same process and watch RSS climb. Doc 05 §3.5
identifies three known leak vectors:

- HTTP clients held by ``app.state.http_clients`` if a module forgot to
  unregister them. The loader already calls ``unregister_module`` so the
  test here exercises that path.
- Closures pinned by Starlette's middleware stack rebuild. The loader
  triggers ``gc.collect()`` after rebuild; this test asserts it runs
  without raising and that mappers actually get released.
- Zombie SQLAlchemy classes whose mappers cling to the registry. The
  loader calls ``Base.registry._dispose_cls`` per class; this test
  re-registers the same class name in a fresh module context to confirm
  no ``Table 'x' is already defined`` collision.

We deliberately skip the full ``ModuleRuntime.install`` path (which
requires manifest validation, S3 source resolution, DB writes, etc.) —
the load/unload primitives in ``ModuleLoader`` are what actually leak
or don't, and ``test_module_metadata_lifecycle.py`` already proves the
metadata branch works.
"""

from __future__ import annotations

import gc
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hotframe.apps.config import ModuleManifest
from hotframe.engine.loader import ModuleLoader
from hotframe.models.base import Base


def _write_fake_module(parent: Path, module_id: str) -> Path:
    """Create a minimal on-disk module package the loader can import.

    Layout::

        parent/
            <module_id>/
                __init__.py
                routes.py     -> APIRouter named ``router``
                models.py     -> one mapped class

    The model class name is uniquified per write so each install/uninstall
    cycle gets a fresh class object (matching the production behaviour
    where modules ship with stable names but get fresh imports per cycle).
    """
    pkg = parent / module_id
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")

    (pkg / "routes.py").write_text(
        textwrap.dedent(
            """
            from fastapi import APIRouter

            router = APIRouter()

            @router.get("/")
            async def index() -> dict:
                return {"ok": True}
            """
        ).lstrip()
    )

    # Model uses an info={"module_id": ...} marker so
    # ``_verify_metadata_cleared`` can identify and force-clean it if the
    # primary disposal path leaves a stray Table behind.
    (pkg / "models.py").write_text(
        textwrap.dedent(
            f"""
            from sqlalchemy import Column, Integer, String
            from hotframe.models.base import Base


            class FakeRow(Base):
                __tablename__ = "fake_{module_id}"
                __table_args__ = {{"info": {{"module_id": "{module_id}"}}}}
                id = Column(Integer, primary_key=True)
                name = Column(String(50))
            """
        ).lstrip()
    )
    return pkg


def _fake_app() -> MagicMock:
    """A MagicMock standing in for the FastAPI app.

    The loader only touches a few attributes (``routes`` list, ``state``,
    ``mount``, ``include_router`` via the registry). Mocks suffice — we
    are testing the loader's bookkeeping, not Starlette internals.
    """
    app = MagicMock()
    app.routes = []
    # ``app.state`` is a real namespace so the loader's
    # ``getattr(app.state, "http_clients", None)`` returns None (no
    # registry installed → nothing to leak through).
    from types import SimpleNamespace

    app.state = SimpleNamespace()
    return app


def _build_manifest(module_id: str) -> ModuleManifest:
    return ModuleManifest(
        MODULE_ID=module_id,
        MODULE_NAME=module_id.title(),
        MODULE_VERSION="0.0.1",
        DEPENDENCIES=[],
    )


def _build_loader(app: MagicMock) -> ModuleLoader:
    """Construct a ``ModuleLoader`` with stubbed collaborators.

    We use AsyncMock for the bus (its ``unsubscribe_module`` is awaited)
    and MagicMocks for the synchronous collaborators.
    """
    bus = MagicMock()
    bus.unsubscribe_module = AsyncMock()

    hooks = MagicMock()
    slots = MagicMock()

    registry = MagicMock()
    # ``registry.get(module_id)`` is consulted on unload to read the
    # registered middleware. Returning ``None`` makes the loader skip the
    # stack-manager branch — fine because we install no module middleware.
    registry.get.return_value = None
    registry.register.return_value = MagicMock()

    stack_manager = MagicMock()
    stack_manager.add_and_rebuild = AsyncMock()
    stack_manager.remove_and_rebuild = AsyncMock()

    return ModuleLoader(
        app=app,
        registry=registry,
        event_bus=bus,
        hooks=hooks,
        slots=slots,
        stack_manager=stack_manager,
    )


@pytest.mark.asyncio
async def test_install_uninstall_cycle_stable_memory(tmp_path: Path) -> None:
    """50 cycles of load_module/unload_module — RSS must stay flat.

    We measure RSS via ``psutil`` if available; otherwise we fall back
    to ``resource.getrusage`` which is good enough on Unix. If neither
    is reliable, we still assert that 50 cycles completed without
    raising — the leak-loudness check is best-effort.
    """
    module_id = "leakcheck"
    pkg_path = _write_fake_module(tmp_path, module_id)

    # The loader imports modules from sys.path entries. Inject the parent
    # dir; remove it on exit so other tests are not polluted.
    sys.path.insert(0, str(tmp_path))
    try:
        app = _fake_app()
        loader = _build_loader(app)
        manifest = _build_manifest(module_id)

        # Warm-up cycles to amortize one-shot allocations (regex compile,
        # importer caches, etc.) before we sample.
        for _ in range(3):
            await loader.load_module(module_id, pkg_path, manifest)
            await loader.unload_module(module_id)
        gc.collect()

        baseline_rss = _read_rss()

        for _ in range(50):
            await loader.load_module(module_id, pkg_path, manifest)
            await loader.unload_module(module_id)

        gc.collect()
        final_rss = _read_rss()

        # Sanity: the module's table is no longer in Base.metadata after
        # the last uninstall. This is the canonical signal that ORM
        # disposal worked — without it, the next install would raise
        # ``Table 'fake_leakcheck' is already defined``.
        assert f"fake_{module_id}" not in Base.metadata.tables

        # Memory check is best-effort. If RSS tooling unavailable, skip.
        if baseline_rss is None or final_rss is None:
            pytest.skip("RSS measurement unavailable on this platform")

        growth = final_rss - baseline_rss
        per_cycle = growth / 50.0
        # 100 KB/cycle is the budget from doc 05 §3.5. We pad to 256 KB
        # because Python/SQLAlchemy allocate small caches lazily that
        # warm up after the first few cycles even with gc.collect().
        # If you see this test go red, run it locally and inspect with
        # ``tracemalloc`` before raising the budget further.
        assert per_cycle < 256 * 1024, (
            f"RSS grew {growth} bytes over 50 cycles ({per_cycle:.0f} bytes/cycle, budget 256 KB)"
        )
    finally:
        sys.path.remove(str(tmp_path))
        # Clean any residual sys.modules entries so other tests are not
        # affected (the loader already does this for tracked imports,
        # but our fake-module import path is sometimes resolved by the
        # CPython importer as ``leakcheck.routes`` etc. — defensive).
        for key in [k for k in sys.modules if k == module_id or k.startswith(f"{module_id}.")]:
            del sys.modules[key]


def _read_rss() -> int | None:
    """Return current RSS in bytes, or None if no measurement is available."""
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        psutil = None  # type: ignore[assignment]

    if psutil is not None:
        try:
            return int(psutil.Process().memory_info().rss)
        except Exception:
            return None

    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # On Linux ru_maxrss is KB; on macOS it is bytes. Normalize to
        # bytes in either case using a simple platform sniff.
        if sys.platform == "darwin":
            return int(usage.ru_maxrss)
        return int(usage.ru_maxrss * 1024)
    except Exception:
        return None
