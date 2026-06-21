"""OpenTelemetry sink contract tests.

Uses the OTel SDK's in-memory exporter so no network/backend is involved.
Skipped entirely when opentelemetry is not installed (it's an optional extra).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lynx import AuditEvent, multi_sink, otel_sink, stdout_sink

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


def _event(seq: int = 0, kind: str = "policy.evaluated", body: dict | None = None) -> AuditEvent:
    return AuditEvent(
        correlation_id="corr-1",
        bundle_id="bundle-1",
        seq=seq,
        kind=kind,
        timestamp=datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC),
        body=body if body is not None else {"tool": "shell", "verdict": "deny"},
    )


def _wired_tracer() -> tuple[object, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


async def test_emits_one_span_per_event_named_by_kind() -> None:
    tracer, exporter = _wired_tracer()
    sink = otel_sink(tracer=tracer)
    await sink(_event(seq=0, kind="run.started"))
    await sink(_event(seq=1, kind="policy.evaluated"))
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["run.started", "policy.evaluated"]


async def test_event_fields_become_attributes() -> None:
    tracer, exporter = _wired_tracer()
    sink = otel_sink(tracer=tracer)
    await sink(_event(seq=3, body={"tool": "shell", "verdict": "deny"}))
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["lynx.correlation_id"] == "corr-1"
    assert attrs["lynx.bundle_id"] == "bundle-1"
    assert attrs["lynx.seq"] == 3
    assert attrs["lynx.kind"] == "policy.evaluated"
    assert attrs["lynx.body.tool"] == "shell"
    assert attrs["lynx.body.verdict"] == "deny"


async def test_non_scalar_body_is_json_encoded_not_dropped() -> None:
    tracer, exporter = _wired_tracer()
    sink = otel_sink(tracer=tracer)
    await sink(_event(body={"scope": ["fs:read", "net:read"], "meta": None}))
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    # Sequences/None aren't valid OTel scalars — must be canonical JSON, no crash.
    assert attrs["lynx.body.scope"] == '["fs:read","net:read"]'
    assert attrs["lynx.body.meta"] == "null"


async def test_span_timestamp_matches_event() -> None:
    tracer, exporter = _wired_tracer()
    sink = otel_sink(tracer=tracer)
    await sink(_event())
    (span,) = exporter.get_finished_spans()
    expected_ns = int(datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC).timestamp() * 1_000_000_000)
    assert span.start_time == expected_ns
    assert span.end_time == expected_ns  # point-in-time event


async def test_all_spans_end_no_state_retained() -> None:
    # Every span must be ended immediately — nothing accumulates across a run.
    tracer, exporter = _wired_tracer()
    sink = otel_sink(tracer=tracer)
    for i in range(50):
        await sink(_event(seq=i))
    spans = exporter.get_finished_spans()
    assert len(spans) == 50
    assert all(s.end_time is not None for s in spans)


async def test_composes_with_multi_sink() -> None:
    import io

    tracer, exporter = _wired_tracer()
    mirror = io.StringIO()
    sink = multi_sink(otel_sink(tracer=tracer), stdout_sink(stream=mirror))
    await sink(_event())
    assert len(exporter.get_finished_spans()) == 1
    assert mirror.getvalue()  # other sink still fired
