"""Sinks — the streaming home for audit events.

A Sink is anything callable that takes an ``AuditEvent`` and returns an
awaitable ``None``. Lynx never opens a file or holds an event buffer — the
user passes in the sinks they want; Lynx fires events at them as they happen.

Built-in factories return fresh ``Sink`` instances (closures); the runtime
holds no global sink state.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import IO, Any, Protocol, runtime_checkable

from lynx.core.types import AuditEvent, canonical_json

__all__ = [
    "GENESIS_HASH",
    "Sink",
    "VerifyResult",
    "callback_sink",
    "hash_chained_sink",
    "jsonl_sink",
    "multi_sink",
    "noop_sink",
    "otel_sink",
    "stdout_sink",
    "verify_chain",
]

# Seed for the hash chain. A fixed, versioned label so an empty file and a
# tampered-to-empty file are distinguishable, and so the genesis link can never
# collide with a real event hash.
GENESIS_HASH = "lynx-audit-genesis-v1"


@runtime_checkable
class Sink(Protocol):
    """A callable that receives one ``AuditEvent`` at a time."""

    async def __call__(self, event: AuditEvent) -> None: ...


# ---------------------------------------------------------------------------
# Built-in sinks
# ---------------------------------------------------------------------------


def noop_sink() -> Sink:
    """Discard every event. For tests where we don't want output."""

    async def sink(event: AuditEvent) -> None:
        return None

    return sink


def stdout_sink(*, stream: IO[str] | None = None) -> Sink:
    """Pretty-print events to stdout (or any text stream).

    The stream is the user's; we don't open or close it.
    """
    target = stream if stream is not None else sys.stdout

    async def sink(event: AuditEvent) -> None:
        line = (
            f"[{event.timestamp.isoformat()}] "
            f"#{event.seq:>3} {event.kind:<22} "
            f"corr={event.correlation_id[:8]} "
            f"body={canonical_json(dict(event.body))[:120]}"
        )
        target.write(line + "\n")
        target.flush()

    return sink


def jsonl_sink(handle: IO[str]) -> Sink:
    """Write one canonical-JSON event per line. User owns the file handle.

    Flushes after every record so a crash mid-run does not lose buffered
    events. If you need higher throughput, wrap your own buffered sink.
    """

    async def sink(event: AuditEvent) -> None:
        record: dict[str, Any] = {
            "correlation_id": event.correlation_id,
            "bundle_id": event.bundle_id,
            "seq": event.seq,
            "kind": event.kind,
            "timestamp": event.timestamp.isoformat(),
            "body": dict(event.body),
        }
        handle.write(canonical_json(record) + "\n")
        try:
            handle.flush()
        except (AttributeError, ValueError):
            # In-memory handles (StringIO) may not need or support flush.
            pass

    return sink


def _chain_record(event: AuditEvent) -> dict[str, Any]:
    """The canonical event payload that the hash chain commits to.

    Identical field set to ``jsonl_sink`` so the two formats stay aligned and a
    chained file is still a readable superset of a plain audit file.
    """
    return {
        "correlation_id": event.correlation_id,
        "bundle_id": event.bundle_id,
        "seq": event.seq,
        "kind": event.kind,
        "timestamp": event.timestamp.isoformat(),
        "body": dict(event.body),
    }


def _chain_hash(prev_hash: str, record: dict[str, Any]) -> str:
    """sha256 over (previous fingerprint + this event's canonical JSON).

    Chaining the previous hash in is what makes edits, deletions, and reorders
    detectable: changing any line changes its hash, which changes every hash
    after it.
    """
    payload = prev_hash + canonical_json(record)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_chained_sink(handle: IO[str]) -> Sink:
    """Tamper-evident ``jsonl_sink``: each line carries a fingerprint of itself
    chained to the line before it.

    Per record we write the same fields as :func:`jsonl_sink` plus two more:

    * ``prev_hash`` — the fingerprint of the previous line (``GENESIS_HASH`` for
      the first).
    * ``hash`` — ``sha256(prev_hash + canonical_json(record))``.

    Because every fingerprint folds in the one before it, editing a body,
    deleting a line, or reordering lines breaks every fingerprint downstream —
    detectable with :func:`verify_chain` or ``lynx verify``. This is
    tamper-*evident*: anyone who can rewrite the whole file can also recompute
    the chain, so it proves "nobody altered the log", not "nobody could forge
    it". (Signing — tamper-*proof* — is a deferred follow-up.)

    The only state is one ``prev_hash`` variable captured in the closure, the
    same kind of state ``jsonl_sink`` already holds in its file handle. Composes
    with :func:`multi_sink`. The user owns the handle.
    """
    prev_hash = GENESIS_HASH

    async def sink(event: AuditEvent) -> None:
        nonlocal prev_hash
        record = _chain_record(event)
        h = _chain_hash(prev_hash, record)
        line = {**record, "prev_hash": prev_hash, "hash": h}
        handle.write(canonical_json(line) + "\n")
        try:
            handle.flush()
        except (AttributeError, ValueError):
            # In-memory handles (StringIO) may not need or support flush.
            pass
        prev_hash = h

    return sink


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of :func:`verify_chain`.

    ``intact`` is True only when every line's fingerprint recomputes and links
    to the previous one. On failure, ``broken_at`` is the 1-based line number of
    the first bad link and ``reason`` explains it.
    """

    intact: bool
    lines: int
    broken_at: int | None = None
    reason: str | None = None


def verify_chain(path: str) -> VerifyResult:
    """Re-walk a :func:`hash_chained_sink` file and confirm the chain is intact.

    Recomputes each line's fingerprint from its payload and the previous line's
    recorded hash, and checks the link matches. Detects edited bodies (hash
    mismatch), deletions and reorders (``prev_hash`` no longer equals the prior
    line's ``hash``), and truncated/garbled lines.
    """
    import json

    prev_hash = GENESIS_HASH
    count = 0
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            count += 1
            try:
                obj = json.loads(raw)
            except ValueError as exc:
                return VerifyResult(False, count, lineno, f"not JSON: {exc}")
            if not isinstance(obj, dict) or "hash" not in obj or "prev_hash" not in obj:
                return VerifyResult(
                    False, count, lineno, "missing hash/prev_hash (not a chained file?)"
                )
            recorded_hash = obj["hash"]
            recorded_prev = obj["prev_hash"]
            if recorded_prev != prev_hash:
                return VerifyResult(
                    False,
                    count,
                    lineno,
                    "prev_hash does not match previous line (line deleted or reordered)",
                )
            record = {k: v for k, v in obj.items() if k not in ("hash", "prev_hash")}
            expected = _chain_hash(recorded_prev, record)
            if expected != recorded_hash:
                return VerifyResult(
                    False, count, lineno, "hash mismatch (line was modified)"
                )
            prev_hash = recorded_hash
    return VerifyResult(True, count)


def multi_sink(*sinks: Sink) -> Sink:
    """Fan out to several sinks concurrently. Exceptions in one don't kill others.

    Failures are reported to stderr so operators can see a sink that's
    consistently broken instead of silently losing every event.
    """

    async def sink(event: AuditEvent) -> None:
        results = await asyncio.gather(
            *(s(event) for s in sinks),
            return_exceptions=True,
        )
        for sub, outcome in zip(sinks, results, strict=True):
            if isinstance(outcome, BaseException):
                sink_name = getattr(sub, "__qualname__", repr(sub))
                print(
                    f"[lynx] sink {sink_name} failed on event "
                    f"{event.kind!r} seq={event.seq}: "
                    f"{type(outcome).__name__}: {outcome}",
                    file=sys.stderr,
                )

    return sink


def callback_sink(fn: Callable[[AuditEvent], Awaitable[None]]) -> Sink:
    """Wrap any awaitable-returning callable as a Sink."""

    async def sink(event: AuditEvent) -> None:
        await fn(event)

    return sink


def otel_sink(tracer: Any | None = None) -> Sink:
    """Emit each ``AuditEvent`` as an OpenTelemetry span — governance decisions
    show up in your existing tracing backend (Datadog, Honeycomb, Grafana/Tempo,
    Jaeger, …) with no custom plumbing.

    Each event becomes one short, point-in-time span (started and ended at the
    event's own timestamp) named after ``event.kind``, with the event fields as
    ``lynx.*`` attributes. The span is created against the *ambient* OTel
    context, so when an agent runs inside an already-instrumented request its
    governance events nest under that request's trace automatically; with no
    ambient span they're roots correlated by the ``lynx.correlation_id``
    attribute.

    **Stateless and leak-free by construction:** each span is ended immediately,
    so the sink holds no per-run or per-event state — nothing accumulates across
    a long run. (Any buffering is your exporter's bounded queue, not ours.)
    Requires ``opentelemetry-api`` (``pip install lynx-agent[otel]``); without an
    OTel *SDK* configured, ``get_tracer`` returns a no-op tracer and events are
    silently dropped, exactly like upstream OTel.

    Pass your own ``tracer`` to control the instrumentation scope; by default we
    use ``trace.get_tracer("lynx")``.
    """
    try:
        from opentelemetry import trace
    except ImportError as exc:  # pragma: no cover - exercised via packaging extra
        raise RuntimeError(
            "otel_sink requires opentelemetry-api: pip install lynx-agent[otel]"
        ) from exc

    active_tracer = tracer if tracer is not None else trace.get_tracer("lynx")

    async def sink(event: AuditEvent) -> None:
        ts_ns = int(event.timestamp.timestamp() * 1_000_000_000)
        span = active_tracer.start_span(name=event.kind, start_time=ts_ns)
        try:
            span.set_attribute("lynx.correlation_id", event.correlation_id)
            span.set_attribute("lynx.bundle_id", event.bundle_id)
            span.set_attribute("lynx.seq", event.seq)
            span.set_attribute("lynx.kind", event.kind)
            for key, value in event.body.items():
                # OTel attributes accept only str/bool/int/float (+ sequences);
                # anything else (dicts, None, nested) becomes canonical JSON so
                # nothing is lost and set_attribute never raises.
                if isinstance(value, (str, bool, int, float)):
                    span.set_attribute(f"lynx.body.{key}", value)
                else:
                    span.set_attribute(f"lynx.body.{key}", canonical_json(value))
        finally:
            span.end(end_time=ts_ns)

    return sink


# Note on further optional sinks: prometheus_sink, http_sink, kafka_sink are
# deferred — they each depend on optional packages and can be added without
# breaking the public API.

# Re-export to silence linters about unused imports above when applicable.
_ = asdict
