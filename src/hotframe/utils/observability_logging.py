"""
Structured logging via structlog.

- JSON output in production (LOG_FORMAT=json or DEPLOYMENT_MODE=web)
- Colored console output in development
- Auto-binds request_id, hub_id, user_id, module_id from contextvars
- Redirects all stdlib logging through structlog processors
- Processors: ISO8601 timestamp, log level, caller info, stack traces on errors
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from hotframe.utils.observability_context import request_context


def _add_request_context(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Structlog processor: inject request-scoped context from contextvars."""
    ctx = request_context.get()
    if ctx.request_id:
        event_dict.setdefault("request_id", ctx.request_id)
    if ctx.hub_id:
        event_dict.setdefault("hub_id", ctx.hub_id)
    if ctx.user_id:
        event_dict.setdefault("user_id", ctx.user_id)
    if ctx.module_id:
        event_dict.setdefault("module_id", ctx.module_id)
    if ctx.trace_id:
        event_dict.setdefault("trace_id", ctx.trace_id)
    return event_dict


def _add_caller_info(
    logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Structlog processor: add caller filename and line number."""
    # structlog's CallsiteParameterAdder handles this more robustly,
    # but we configure it in the processor chain instead.
    return event_dict


def setup_logging(
    *,
    log_level: str = "INFO",
    json_output: bool = False,
) -> None:
    """
    Configure structlog + stdlib logging integration.

    Args:
        log_level: Python log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        json_output: If True, emit JSON lines. If False, emit colored console output.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Shared processors for both structlog and stdlib
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_request_context,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        structlog.processors.CallsiteParameterAdder(
            parameters=[
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.LINENO,
                structlog.processors.CallsiteParameter.FUNC_NAME,
            ],
        ),
    ]

    if json_output:
        # Production: JSON lines to stdout
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        # Development: colored, human-readable console
        renderer = structlog.dev.ConsoleRenderer(
            colors=sys.stderr.isatty(),
        )

    # Configure structlog
    structlog.configure(
        processors=[
            *shared_processors,
            # Format exceptions as dicts in JSON, as strings in console
            structlog.processors.format_exc_info,
            # Final renderer
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure stdlib logging to route through structlog's formatter
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    # Root handler — all stdlib loggers go through this
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    # Suppress noisy third-party loggers
    for noisy in ("uvicorn.access", "watchfiles", "httpcore", "httpx", "hpack"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    # uvicorn.error should propagate to root (structlog formatter)
    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.handlers.clear()
    uvicorn_logger.propagate = True
    uvicorn_logger.setLevel(level)

    # uvicorn root logger — prevent duplicate output
    uvicorn_root = logging.getLogger("uvicorn")
    uvicorn_root.handlers.clear()
    uvicorn_root.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """
    Get a structlog logger.

    Drop-in replacement for ``logging.getLogger(name)``. Returns a structlog
    BoundLogger that automatically includes request context.
    """
    return structlog.get_logger(name)
