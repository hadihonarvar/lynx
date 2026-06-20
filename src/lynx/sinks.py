"""Sinks — the streaming home for audit events.

A Sink is anything callable that takes an ``AuditEvent`` and returns an
awaitable ``None``. Lynx never opens a file or holds an event buffer — the
user passes in the sinks they want; Lynx fires events at them as they happen.

Built-in factories return fresh ``Sink`` instances (closures); the runtime
holds no global sink state.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import IO, Any, Protocol, runtime_checkable

from lynx.core.types import AuditEvent, canonical_json

__all__ = [
    "Sink",
    "callback_sink",
    "jsonl_sink",
    "multi_sink",
    "noop_sink",
    "stdout_sink",
]


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


# Note on optional sinks: otel_sink, prometheus_sink, http_sink, kafka_sink
# are deferred — they each depend on optional packages and can be
# added without breaking the public API.

# Re-export to silence linters about unused imports above when applicable.
_ = asdict
