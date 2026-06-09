"""Observability hooks: Prometheus metrics + OpenTelemetry traces.

Off by default. Enable with::

    from gazelle.observability import enable_prometheus, enable_otel

    enable_prometheus(port=9100)
    enable_otel(service_name="my-agent")

Both are optional. Without them, the kernel has zero observability overhead.
"""

from __future__ import annotations

import contextlib
from typing import Any

# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------


_prometheus_started = False


def enable_prometheus(port: int = 9100) -> None:
    """Start a Prometheus metrics HTTP server on the given port.

    Requires `pip install gazelle[prometheus]` (or `pip install prometheus-client`).

    Exposes the following metrics:
        gazelle_steps_total{verdict="allow|deny|dry_run|approve_required|transform"}
        gazelle_step_duration_seconds{tool=...}
        gazelle_runs_total{status="succeeded|failed|paused|cancelled"}
        gazelle_actions_dropped_total{reason="denied|approval_timeout"}
    """
    global _prometheus_started
    if _prometheus_started:
        return
    try:
        from prometheus_client import Counter, Histogram, start_http_server
    except ImportError as exc:
        raise ImportError(
            "enable_prometheus requires 'prometheus-client'. "
            "Install with: pip install prometheus-client"
        ) from exc

    global STEPS_TOTAL, STEP_DURATION, RUNS_TOTAL, ACTIONS_DROPPED
    STEPS_TOTAL = Counter("gazelle_steps_total", "Total steps by verdict", ["verdict"])
    STEP_DURATION = Histogram("gazelle_step_duration_seconds", "Step duration", ["tool"])
    RUNS_TOTAL = Counter("gazelle_runs_total", "Total runs by terminal status", ["status"])
    ACTIONS_DROPPED = Counter("gazelle_actions_dropped_total", "Actions not executed", ["reason"])
    start_http_server(port)
    _prometheus_started = True


# ---------------------------------------------------------------------------
# OpenTelemetry
# ---------------------------------------------------------------------------


_otel_tracer: Any | None = None


def enable_otel(service_name: str = "gazelle") -> None:
    """Initialize OpenTelemetry tracing.

    Requires `pip install gazelle[otel]`. Honors the standard OTEL_EXPORTER_*
    env vars (OTLP HTTP by default).
    """
    global _otel_tracer
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise ImportError(
            "enable_otel requires 'opentelemetry-sdk' + the OTLP exporter. "
            "Install with: pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http"
        ) from exc

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _otel_tracer = trace.get_tracer("gazelle")


@contextlib.contextmanager
def trace_step(run_id: str, seq: int, tool: str):
    """Context manager that wraps a step execution in an OTel span if enabled."""
    if _otel_tracer is None:
        yield
        return
    with _otel_tracer.start_as_current_span(
        "gzl.step",
        attributes={"gzl.run_id": run_id, "gzl.step_seq": seq, "gzl.tool": tool},
    ):
        yield


__all__ = ["enable_otel", "enable_prometheus", "trace_step"]
