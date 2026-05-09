"""
Hot-mount pipeline with explicit phases and LIFO rollback.

Models the install / mount sequence of a module as a state machine. Each
phase performs one well-defined step (download, extract, validate, ...)
and returns a :class:`PhaseResult` carrying a :class:`RollbackHandle`
that can undo it.

The pipeline records every successful phase. If a later phase raises,
the caller invokes :meth:`HotMountPipeline.rollback`, which executes the
recorded rollback handles in **LIFO order** and returns the list of
exceptions encountered (best-effort: a failing rollback does not stop
later rollbacks).

Layering: lives in ``hotframe/engine/`` and depends only on stdlib so it
remains at the engine layer of the architecture.

This primitive is independent of the rest of the pipeline; it does not
download S3, run Alembic or import anything by itself. Callers compose
the phase functions and pass them to :meth:`run_phase`. Integration with
``ModuleRuntime`` / ``HotMountPipeline`` orchestration is left to the
caller.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class RollbackHandle(Protocol):
    """
    Protocol for a single phase's rollback action.

    Implementations should be **idempotent and best-effort**: rollbacks
    run during error recovery and may themselves fail; they should not
    raise on "already undone" conditions.
    """

    async def undo(self) -> None: ...


@dataclass(slots=True)
class PhaseResult:
    """
    Outcome of a successfully executed phase.

    Attributes:
        phase_name: One of :attr:`HotMountPipeline.PHASES`.
        rollback: Handle invoked LIFO if a later phase fails.
        payload: Arbitrary data the phase wants to publish to the next
            phase or to observability layers.
    """

    phase_name: str
    rollback: RollbackHandle
    payload: dict = field(default_factory=dict)


class PhaseStatus(str, Enum):
    """Coarse-grained lifecycle of the pipeline."""

    PENDING = "pending"
    RUNNING = "running"
    ACTIVE = "active"
    ERROR = "error"


@dataclass(slots=True)
class PipelineState:
    """
    Mutable state held by a :class:`HotMountPipeline`.

    Attributes:
        module_id: Identifier of the module being mounted.
        current_phase: Name of the most recently *attempted* phase.
        completed_phases: Names of phases that finished successfully, in
            order.
        rollback_stack: Rollback handles recorded for completed phases,
            in execution order. :meth:`HotMountPipeline.rollback`
            consumes them LIFO.
        status: Coarse status (PENDING / RUNNING / ACTIVE / ERROR).
        error: The exception that aborted the pipeline, if any.
    """

    module_id: str
    current_phase: str | None = None
    completed_phases: list[str] = field(default_factory=list)
    rollback_stack: list[RollbackHandle] = field(default_factory=list)
    status: PhaseStatus = PhaseStatus.PENDING
    error: Exception | None = None


class HotMountPipeline:
    """
    Phased pipeline with LIFO rollback for module hot-mount.

    Phases (declared in :attr:`PHASES`) represent the canonical sequence
    of a hot-mount install. Callers do not need to use *all* of them and
    may use them in any order, but :meth:`run_phase` rejects unknown
    names so typos surface early.

    Usage::

        pipeline = HotMountPipeline(module_id="invoice")
        try:
            await pipeline.run_phase("DOWNLOADING", download_fn)
            await pipeline.run_phase("EXTRACTING", extract_fn)
            await pipeline.run_phase("MOUNTING", mount_fn)
            await pipeline.commit()
        except Exception:
            errors = await pipeline.rollback()
            # ... log errors, mark module as 'error' in DB, etc.
            raise

    The pipeline does not enforce phase ordering itself — it is a
    bookkeeping primitive. Callers compose the actual install logic.
    """

    PHASES: list[str] = [
        "INIT",
        "DOWNLOADING",
        "EXTRACTING",
        "VALIDATING",
        "MIGRATING",
        "IMPORTING",
        "MOUNTING",
        "STACK_REBUILD",
        "ACTIVE",
    ]

    def __init__(self, module_id: str) -> None:
        self._state = PipelineState(module_id=module_id)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def run_phase(
        self,
        phase_name: str,
        fn: Callable[..., Awaitable[PhaseResult]],
        *args: Any,
        **kwargs: Any,
    ) -> PhaseResult:
        """
        Execute a single phase and record its rollback handle.

        Args:
            phase_name: Must be one of :attr:`PHASES`.
            fn: Async callable returning a :class:`PhaseResult`. Receives
                ``*args, **kwargs`` unchanged.

        Returns:
            The :class:`PhaseResult` produced by ``fn``.

        Raises:
            ValueError: If ``phase_name`` is not in :attr:`PHASES`.
            Exception: Whatever ``fn`` raises is propagated unchanged
                after marking the pipeline as ``ERROR``.
        """
        if phase_name not in self.PHASES:
            raise ValueError(f"Unknown phase {phase_name!r}; valid phases: {self.PHASES}")

        self._state.current_phase = phase_name
        self._state.status = PhaseStatus.RUNNING

        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            self._state.status = PhaseStatus.ERROR
            self._state.error = exc
            logger.warning(
                "pipeline phase failed module_id=%s phase=%s error=%s",
                self._state.module_id,
                phase_name,
                exc,
            )
            raise

        if not isinstance(result, PhaseResult):
            self._state.status = PhaseStatus.ERROR
            self._state.error = TypeError(
                f"Phase {phase_name!r} must return PhaseResult, got {type(result)!r}"
            )
            raise self._state.error

        self._state.rollback_stack.append(result.rollback)
        self._state.completed_phases.append(phase_name)
        return result

    async def commit(self) -> None:
        """
        Mark the pipeline as :attr:`PhaseStatus.ACTIVE`.

        Call after every phase has run successfully. The state machine
        does not enforce this — callers are free to commit at any point —
        but the convention is that ACTIVE means "module is live".
        """
        self._state.status = PhaseStatus.ACTIVE
        self._state.current_phase = "ACTIVE"
        logger.info(
            "pipeline committed module_id=%s phases=%s",
            self._state.module_id,
            self._state.completed_phases,
        )

    async def rollback(self) -> list[Exception]:
        """
        Execute every recorded rollback handle in LIFO order.

        Best-effort: a rollback that raises is collected and the
        remaining handles are still attempted. The state is left at
        :attr:`PhaseStatus.ERROR` regardless of outcome.

        Returns:
            List of exceptions raised during rollback (empty if all
            succeeded).
        """
        errors: list[Exception] = []
        # Pop in LIFO order so each rollback sees the state prior to
        # the next one being undone.
        while self._state.rollback_stack:
            handle = self._state.rollback_stack.pop()
            try:
                await handle.undo()
            except Exception as exc:
                errors.append(exc)
                logger.exception(
                    "rollback handle failed module_id=%s handle=%r",
                    self._state.module_id,
                    handle,
                )

        self._state.status = PhaseStatus.ERROR
        if errors:
            logger.warning(
                "rollback completed with errors module_id=%s error_count=%d",
                self._state.module_id,
                len(errors),
            )
        else:
            logger.info(
                "rollback completed module_id=%s",
                self._state.module_id,
            )
        return errors

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> PipelineState:
        """Read-only access to the underlying :class:`PipelineState`."""
        return self._state
