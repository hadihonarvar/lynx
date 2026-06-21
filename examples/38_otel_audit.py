"""
================================================================
EXAMPLE 38 — "Governance decisions in your tracing backend" (OBSERVABILITY)
================================================================

SCENARIO:
    You already run OpenTelemetry — traces flow to Datadog / Honeycbox /
    Grafana Tempo / Jaeger. Lynx's audit stream should land there too, so an
    agent's tool call and the policy verdict that gated it show up right next to
    the rest of your telemetry. `otel_sink` does exactly that: each `AuditEvent`
    becomes one short span named after `event.kind`, with the event fields as
    `lynx.*` attributes.

    It's stateless — every span is ended immediately, so nothing accumulates
    over a long run — and it nests under the ambient trace automatically when
    the agent runs inside an already-instrumented request.

WHAT THIS EXAMPLE SHOWS:
    - Wiring `otel_sink` to a tracer (here: the SDK's console exporter so you
      can see the spans without a backend).
    - Each governance event arriving as a span with `lynx.*` attributes.

REQUIRES:
    pip install lynx-agent[otel]      # opentelemetry-api
    pip install opentelemetry-sdk     # for the exporter used in this demo

RUN WITH:
    python examples/38_otel_audit.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from lynx import AuditEvent, otel_sink


def _event(seq: int, kind: str, **body: object) -> AuditEvent:
    return AuditEvent(
        correlation_id="run-42",
        bundle_id="bundle-abc",
        seq=seq,
        kind=kind,
        timestamp=datetime.now(UTC),
        body=body,
    )


async def main() -> None:
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )
    except ImportError:
        print("This demo needs the OTel SDK: pip install opentelemetry-sdk", file=sys.stderr)
        raise SystemExit(1) from None

    # In real use you'd configure your own provider/exporter once at startup;
    # here we print spans to the console so the demo is self-contained.
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    tracer = provider.get_tracer("lynx-demo")

    sink = otel_sink(tracer=tracer)

    print("Emitting 3 governance events as OTel spans:\n")
    await sink(_event(0, "policy.evaluated", tool="shell", verdict="allow"))
    await sink(_event(1, "policy.evaluated", tool="shell", verdict="deny",
                      reason="rm -rf / is never allowed"))
    await sink(_event(2, "tool.called", tool="http_get", url="https://api.example.com"))

    print("\nEach span above carries lynx.correlation_id / lynx.kind / lynx.body.* —")
    print("point your real exporter at Datadog/Honeycomb/Tempo and they appear there.")


if __name__ == "__main__":
    asyncio.run(main())
