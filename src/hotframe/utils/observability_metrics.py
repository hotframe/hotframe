"""
Custom application metrics via OpenTelemetry.

Provides pre-defined instruments for Hub observability:
- Request latency (histogram, by endpoint/method/status)
- Module load time (histogram)
- Event emit counter (by event name)
- Hook execution duration (histogram, by hook name)
- Active modules gauge
- Error counter (by module, type)
- Background task counters (pending, running, completed, failed)

All metrics are lazy-initialized — zero overhead if OpenTelemetry SDK is not
configured (api-only mode returns no-op instruments).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry import metrics

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram, UpDownCounter

_meter: metrics.Meter | None = None


def _get_meter() -> metrics.Meter:
    """Return the singleton Meter, creating it on first call."""
    global _meter
    if _meter is None:
        _meter = metrics.get_meter("hotframe", version="0.1.0")
    return _meter


# -----------------------------------------------------------------------
# Request metrics
# -----------------------------------------------------------------------

_request_duration: Histogram | None = None


def get_request_duration_histogram() -> Histogram:
    """HTTP request duration in milliseconds, by endpoint/method/status."""
    global _request_duration
    if _request_duration is None:
        _request_duration = _get_meter().create_histogram(
            name="http.server.request.duration",
            description="HTTP request duration in milliseconds",
            unit="ms",
        )
    return _request_duration


# -----------------------------------------------------------------------
# Module metrics
# -----------------------------------------------------------------------

_module_load_duration: Histogram | None = None
_active_modules: UpDownCounter | None = None


def get_module_load_duration_histogram() -> Histogram:
    """Module load time in milliseconds."""
    global _module_load_duration
    if _module_load_duration is None:
        _module_load_duration = _get_meter().create_histogram(
            name="hotframe.module.load.duration",
            description="Module load time in milliseconds",
            unit="ms",
        )
    return _module_load_duration


def get_active_modules_counter() -> UpDownCounter:
    """Gauge-like counter tracking number of active modules."""
    global _active_modules
    if _active_modules is None:
        _active_modules = _get_meter().create_up_down_counter(
            name="hotframe.modules.active",
            description="Number of currently active modules",
        )
    return _active_modules


# -----------------------------------------------------------------------
# Event metrics
# -----------------------------------------------------------------------

_event_emit_counter: Counter | None = None


def get_event_emit_counter() -> Counter:
    """Counter for event emissions, labelled by event name."""
    global _event_emit_counter
    if _event_emit_counter is None:
        _event_emit_counter = _get_meter().create_counter(
            name="hotframe.events.emitted",
            description="Number of events emitted",
        )
    return _event_emit_counter


_event_handler_duration: Histogram | None = None


def get_event_handler_duration_histogram() -> Histogram:
    """Event handler execution duration in milliseconds."""
    global _event_handler_duration
    if _event_handler_duration is None:
        _event_handler_duration = _get_meter().create_histogram(
            name="hotframe.events.handler.duration",
            description="Event handler execution time in milliseconds",
            unit="ms",
        )
    return _event_handler_duration


# -----------------------------------------------------------------------
# Hook metrics
# -----------------------------------------------------------------------

_hook_duration: Histogram | None = None


def get_hook_duration_histogram() -> Histogram:
    """Hook execution duration in milliseconds, by hook name."""
    global _hook_duration
    if _hook_duration is None:
        _hook_duration = _get_meter().create_histogram(
            name="hotframe.hooks.duration",
            description="Hook execution time in milliseconds",
            unit="ms",
        )
    return _hook_duration


_hook_callback_counter: Counter | None = None


def get_hook_callback_counter() -> Counter:
    """Counter for hook callback invocations."""
    global _hook_callback_counter
    if _hook_callback_counter is None:
        _hook_callback_counter = _get_meter().create_counter(
            name="hotframe.hooks.callbacks.invoked",
            description="Number of hook callbacks invoked",
        )
    return _hook_callback_counter


# -----------------------------------------------------------------------
# Error metrics
# -----------------------------------------------------------------------

_error_counter: Counter | None = None


def get_error_counter() -> Counter:
    """Error counter, labelled by module and error type."""
    global _error_counter
    if _error_counter is None:
        _error_counter = _get_meter().create_counter(
            name="hotframe.errors",
            description="Number of errors by module and type",
        )
    return _error_counter


# -----------------------------------------------------------------------
# Background task metrics
# -----------------------------------------------------------------------

_tasks_pending: UpDownCounter | None = None
_tasks_running: UpDownCounter | None = None
_tasks_completed: Counter | None = None
_tasks_failed: Counter | None = None


def get_tasks_pending_counter() -> UpDownCounter:
    """Pending background tasks gauge."""
    global _tasks_pending
    if _tasks_pending is None:
        _tasks_pending = _get_meter().create_up_down_counter(
            name="hotframe.tasks.pending",
            description="Number of pending background tasks",
        )
    return _tasks_pending


def get_tasks_running_counter() -> UpDownCounter:
    """Running background tasks gauge."""
    global _tasks_running
    if _tasks_running is None:
        _tasks_running = _get_meter().create_up_down_counter(
            name="hotframe.tasks.running",
            description="Number of running background tasks",
        )
    return _tasks_running


def get_tasks_completed_counter() -> Counter:
    """Completed background tasks counter."""
    global _tasks_completed
    if _tasks_completed is None:
        _tasks_completed = _get_meter().create_counter(
            name="hotframe.tasks.completed",
            description="Number of completed background tasks",
        )
    return _tasks_completed


def get_tasks_failed_counter() -> Counter:
    """Failed background tasks counter."""
    global _tasks_failed
    if _tasks_failed is None:
        _tasks_failed = _get_meter().create_counter(
            name="hotframe.tasks.failed",
            description="Number of failed background tasks",
        )
    return _tasks_failed


# -----------------------------------------------------------------------
# Reset (for testing)
# -----------------------------------------------------------------------


def reset_metrics() -> None:
    """Reset all cached metric instruments. For testing only."""
    global _meter
    global _request_duration, _module_load_duration, _active_modules
    global _event_emit_counter, _event_handler_duration
    global _hook_duration, _hook_callback_counter
    global _error_counter
    global _tasks_pending, _tasks_running, _tasks_completed, _tasks_failed
    _meter = None
    _request_duration = None
    _module_load_duration = None
    _active_modules = None
    _event_emit_counter = None
    _event_handler_duration = None
    _hook_duration = None
    _hook_callback_counter = None
    _error_counter = None
    _tasks_pending = None
    _tasks_running = None
    _tasks_completed = None
    _tasks_failed = None
