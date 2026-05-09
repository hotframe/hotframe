"""
OpenTelemetry setup — traces and metrics export.

- Auto-instruments FastAPI (all incoming requests)
- Auto-instruments SQLAlchemy (all DB queries)
- Auto-instruments httpx (all outgoing HTTP calls)
- Custom span helpers for events, hooks, module operations
- OTLP exporter: env-based (OTEL_EXPORTER_OTLP_ENDPOINT → collector, else console/noop)
- Zero-config for dev: console exporter when no endpoint is set and DEBUG=True

Environment variables (standard OTEL):
    OTEL_EXPORTER_OTLP_ENDPOINT — gRPC endpoint (e.g., http://localhost:4317)
    OTEL_SERVICE_NAME — defaults to "hub"
    OTEL_TRACES_EXPORTER — "otlp" or "console" (auto-detected)
"""

from __future__ import annotations

import logging
import os
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.semconv.resource import ResourceAttributes

if TYPE_CHECKING:
    from opentelemetry.trace import Span, Tracer

logger = logging.getLogger(__name__)

_tracer: Tracer | None = None


def setup_telemetry(
    *,
    service_name: str = "hub",
    debug: bool = False,
    hub_id: str = "",
) -> None:
    """
    Initialize OpenTelemetry tracing and metrics.

    Args:
        service_name: The OTEL service name. Defaults to "hub".
        debug: If True and no OTLP endpoint, use ConsoleSpanExporter.
        hub_id: Hub identifier to attach to all spans as a resource attribute.
    """
    global _tracer

    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    otel_service = os.environ.get("OTEL_SERVICE_NAME", service_name)

    # Build resource with service metadata
    resource_attrs: dict[str, Any] = {
        ResourceAttributes.SERVICE_NAME: otel_service,
        ResourceAttributes.SERVICE_VERSION: "0.1.0",
    }
    if hub_id:
        resource_attrs["hub.id"] = hub_id

    resource = Resource.create(resource_attrs)

    # Create TracerProvider
    provider = TracerProvider(resource=resource)

    # Choose exporter
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info("OTLP trace exporter configured: %s", otlp_endpoint)
        except ImportError:
            logger.warning(
                "opentelemetry-exporter-otlp-proto-grpc not installed — "
                "OTLP endpoint %s configured but unavailable",
                otlp_endpoint,
            )
    elif debug:
        # Dev mode: console exporter for visibility
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        logger.info("OpenTelemetry console trace exporter configured (dev mode)")

    # Set global provider
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("hub", "0.1.0")

    # --- Metrics provider ---
    _setup_metrics_provider(otlp_endpoint, resource, debug)

    # --- Auto-instrumentation ---
    _auto_instrument_fastapi()
    _auto_instrument_sqlalchemy()
    _auto_instrument_httpx()


def _setup_metrics_provider(
    otlp_endpoint: str,
    resource: Resource,
    debug: bool,
) -> None:
    """Configure OpenTelemetry metrics export."""
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )

    readers = []

    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (  # type: ignore[import-not-found]
                OTLPMetricExporter,
            )

            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=otlp_endpoint),
                export_interval_millis=30_000,
            )
            readers.append(reader)
        except ImportError:
            pass
    elif debug:
        reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(),
            export_interval_millis=60_000,  # Less noisy in dev
        )
        readers.append(reader)

    if readers:
        provider = MeterProvider(resource=resource, metric_readers=readers)
        metrics.set_meter_provider(provider)


def _auto_instrument_fastapi() -> None:
    """Auto-instrument FastAPI if the instrumentation package is available."""
    try:
        from opentelemetry.instrumentation.fastapi import (  # type: ignore[import-not-found]
            FastAPIInstrumentor,
        )

        # BaseInstrumentor.instrument is an instance method — call it on
        # an instance, not on the class itself.
        FastAPIInstrumentor().instrument()
        logger.debug("FastAPI auto-instrumentation enabled")
    except ImportError:
        logger.debug("opentelemetry-instrumentation-fastapi not installed — skipping")


def _auto_instrument_sqlalchemy() -> None:
    """Auto-instrument SQLAlchemy if the instrumentation package is available."""
    try:
        from opentelemetry.instrumentation.sqlalchemy import (  # type: ignore[import-not-found]
            SQLAlchemyInstrumentor,
        )

        SQLAlchemyInstrumentor().instrument()
        logger.debug("SQLAlchemy auto-instrumentation enabled")
    except ImportError:
        logger.debug("opentelemetry-instrumentation-sqlalchemy not installed — skipping")


def _auto_instrument_httpx() -> None:
    """Auto-instrument httpx if the instrumentation package is available."""
    try:
        from opentelemetry.instrumentation.httpx import (  # type: ignore[import-not-found]
            HTTPXClientInstrumentor,
        )

        HTTPXClientInstrumentor().instrument()
        logger.debug("httpx auto-instrumentation enabled")
    except ImportError:
        logger.debug("opentelemetry-instrumentation-httpx not installed — skipping")


# -----------------------------------------------------------------------
# Span helpers
# -----------------------------------------------------------------------


def get_tracer() -> Tracer:
    """Return the Hub tracer. Falls back to a no-op tracer if not configured."""
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer("hub", "0.1.0")
    return _tracer


def start_span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
) -> AbstractContextManager[Span]:
    """
    Start a new span as a context manager.

    Usage::

        with start_span("event.emit", attributes={"event.name": "sale.created"}) as span:
            ...
            span.set_attribute("event.handler_count", 5)
    """
    tracer = get_tracer()
    return tracer.start_as_current_span(
        name,
        attributes=attributes or {},
        kind=kind,
    )


def create_event_span(event_name: str) -> AbstractContextManager[Span]:
    """Create a span for an event emission."""
    return start_span(
        f"event.emit:{event_name}",
        attributes={
            "event.name": event_name,
            "event.system": "event_bus",
        },
    )


def create_hook_span(hook_name: str, hook_type: str = "action") -> AbstractContextManager[Span]:
    """Create a span for a hook execution."""
    return start_span(
        f"hook.{hook_type}:{hook_name}",
        attributes={
            "hook.name": hook_name,
            "hook.type": hook_type,
        },
    )


def create_module_span(operation: str, module_id: str) -> AbstractContextManager[Span]:
    """Create a span for a module operation (install, activate, etc.)."""
    return start_span(
        f"module.{operation}:{module_id}",
        attributes={
            "module.id": module_id,
            "module.operation": operation,
        },
    )
