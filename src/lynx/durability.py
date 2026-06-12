"""Durability — journal an agent run into a user-owned store.

Lynx ships **no storage**. A ``RunStore`` is any object the user implements
over their own infrastructure (Redis, Postgres, DynamoDB, a dict, a file)
with two methods. When ``run_agent`` is given a store and a ``run_id``, the
kernel journals each model output and write-ahead action intent as the run
progresses. A crashed run re-invoked with the same ``run_id`` resumes at the
first incomplete step: completed model calls are not re-sent (no re-burned
tokens) and actions whose results are already journaled are not re-executed
(no double side effects).

The concurrency contract — the load-bearing sentence:

    ``append`` MUST atomically reject a record whose ``(run_id, seq)``
    already exists by raising ``DuplicateRecord``.

That single uniqueness guarantee is what makes concurrent re-dispatch safe.
The write-ahead intent *is* the claim: two workers racing the same run
compete for the same next ``seq``; the store admits exactly one; the loser
exits with a ``superseded`` result instead of double-executing. There are no
leases, no TTLs, and nothing to clean up when a worker dies — a dead worker
holds nothing, and the next resume finds any intent-without-result and
routes it through policy as an uncertain action (``context.extra.
uncertain_retry`` is set so rules can match it).

Lynx does not restart dead processes. Your supervisor (systemd, k8s, a queue
consumer) restarts; Lynx makes the restart cheap and safe. Durability needs
no database — *distributed* durability needs *your* database.

Journal fidelity caveat: tool args and results should be JSON-serializable
(LLM tool calls always are). Non-JSON values degrade to ``repr()`` on
serialization, which makes replayed args drift from the originals and makes
``idempotency_key`` unstable across processes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from lynx.core.types import FinalAnswer, ToolCall, canonical_json

__all__ = [
    "DuplicateRecord",
    "RunStore",
    "RunView",
    "StepRecord",
    "StepView",
    "idempotency_key",
    "replay",
    "step_record_from_json",
    "step_record_to_json",
]


# Verdicts whose intent-without-result means the action MAY have produced a
# side effect. ``deny`` never executes; ``dry_run`` runs only the
# (side-effect-free by contract) shadow. Shared by the scheduler's resume
# logic and replay() so the kernel and the inspection tools never disagree
# about which orphans are uncertain.
UNCERTAIN_VERDICTS = frozenset({"allow", "transform", "approve_required"})


# ---------------------------------------------------------------------------
# The journal record + store protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One journal record.

    ``seq`` is monotonic **per record** within a run — a log offset, not a
    step number. ``(run_id, seq)`` is the uniqueness key the store must
    enforce. The agent-loop step number lives in ``body["step"]``.

    Kinds the kernel writes today: ``run.started``, ``run.resumed`` (the
    fail-fast claim appended before any model call on resume),
    ``model.output``, ``action.intent`` (the write-ahead claim, journaled
    BEFORE the action executes), ``action.result``, ``final``. The kind
    space is open — future releases add kinds without a schema migration,
    so stores must persist kinds they don't recognize and consumers must
    skip them.
    """

    run_id: str
    seq: int
    kind: str
    idempotency_key: str  # set on action.intent / action.result; "" otherwise
    body: Mapping[str, Any]
    timestamp: datetime


class DuplicateRecord(Exception):
    """Raised by a ``RunStore`` when ``(run_id, seq)`` already exists.

    This is the signal — not an error — that another worker owns the run.
    The kernel converts it into a ``superseded`` run result.
    """


@runtime_checkable
class RunStore(Protocol):
    """User-implemented journal storage. Lynx ships no implementation.

    Contract:
      * ``append`` MUST atomically reject a record whose ``(run_id, seq)``
        already exists by raising ``DuplicateRecord``. A record acknowledged
        by ``append`` must be durable to whatever failure domain you care
        about. (Postgres: ``PRIMARY KEY (run_id, seq)`` + catch the unique
        violation. Redis: ``HSETNX`` keyed by ``seq`` — plain ``RPUSH`` does
        NOT satisfy this contract. DynamoDB: conditional put. Tests: a dict.)
      * ``load`` returns every record for ``run_id``, ordered by ``seq``
        (the kernel re-sorts defensively).
    """

    async def append(self, record: StepRecord) -> None: ...

    async def load(self, run_id: str) -> Sequence[StepRecord]: ...


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------


def idempotency_key(run_id: str, step: int, tool: str, args: Mapping[str, Any]) -> str:
    """Stable identity of one proposed action: same run, step, tool, args.

    Keyed on the agent-loop ``step`` (not the record ``seq``) so a retried
    uncertain action carries the same key as its orphaned intent. Stability
    requires JSON-serializable args — non-JSON values fall back to ``repr()``
    which may embed per-process memory addresses.
    """
    payload = f"{run_id}|{step}|{tool}|{canonical_json(dict(args))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Action <-> journal body (used by the scheduler; not part of the public API)
# ---------------------------------------------------------------------------


def action_to_body(action: ToolCall | FinalAnswer, step: int) -> dict[str, Any]:
    if isinstance(action, FinalAnswer):
        return {"step": step, "type": "final_answer", "text": action.text}
    return {
        "step": step,
        "type": "tool_call",
        "tool": action.tool,
        "args": dict(action.args),
        "call_id": action.call_id,
    }


def action_from_body(body: Mapping[str, Any]) -> ToolCall | FinalAnswer:
    if body.get("type") == "final_answer":
        return FinalAnswer(text=body["text"])
    return ToolCall(
        tool=body["tool"],
        args=dict(body.get("args", {})),
        call_id=body.get("call_id", ""),
    )


# ---------------------------------------------------------------------------
# Journal index — the one shared fold over a record sequence.
# Used by both the scheduler's resume path and replay(), so the kernel and
# the inspection tools always agree about what a journal means.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JournalIndex:
    """Everything the kernel or an inspector derives from a journal."""

    records: int
    attempts: int  # 1 + number of run.resumed markers
    proposals: Mapping[int, Mapping[str, Any]]  # step -> model.output body
    orphan_intents: Mapping[int, StepRecord]  # step -> intent with no result yet
    results: Mapping[int, Mapping[str, Any]]  # step -> action.result body
    final_body: Mapping[str, Any] | None
    last_bundle_id: str | None  # from the latest run.started / run.resumed
    next_seq: int


def index_journal(records: Sequence[StepRecord]) -> JournalIndex:
    """Fold a record sequence into the per-step view. Pure function."""
    ordered = sorted(records, key=lambda r: r.seq)
    attempts = 1
    proposals: dict[int, Mapping[str, Any]] = {}
    orphan_intents: dict[int, StepRecord] = {}
    results: dict[int, Mapping[str, Any]] = {}
    final_body: Mapping[str, Any] | None = None
    last_bundle_id: str | None = None

    for rec in ordered:
        body = rec.body
        if rec.kind in ("run.started", "run.resumed"):
            if rec.kind == "run.resumed":
                attempts += 1
            last_bundle_id = body.get("bundle_id", last_bundle_id)
        elif rec.kind == "model.output":
            proposals[int(body["step"])] = body
        elif rec.kind == "action.intent":
            orphan_intents[int(body["step"])] = rec
        elif rec.kind == "action.result":
            step = int(body["step"])
            results[step] = body
            orphan_intents.pop(step, None)
        elif rec.kind == "final":
            final_body = body

    return JournalIndex(
        records=len(ordered),
        attempts=attempts,
        proposals=proposals,
        orphan_intents=orphan_intents,
        results=results,
        final_body=final_body,
        last_bundle_id=last_bundle_id,
        next_seq=ordered[-1].seq + 1 if ordered else 0,
    )


def is_uncertain(intent: StepRecord) -> bool:
    """True when an orphaned intent's action MAY have produced a side effect."""
    return intent.body.get("verdict") in UNCERTAIN_VERDICTS


# ---------------------------------------------------------------------------
# JSON round-trip helpers — so a cookbook store stays ~15 lines
# ---------------------------------------------------------------------------


def step_record_to_json(record: StepRecord) -> str:
    """One canonical-JSON line per record. Pairs with ``step_record_from_json``."""
    return canonical_json(
        {
            "run_id": record.run_id,
            "seq": record.seq,
            "kind": record.kind,
            "idempotency_key": record.idempotency_key,
            "body": dict(record.body),
            "timestamp": record.timestamp.isoformat(),
        }
    )


def step_record_from_json(line: str) -> StepRecord:
    obj = json.loads(line)
    return StepRecord(
        run_id=obj["run_id"],
        seq=obj["seq"],
        kind=obj["kind"],
        idempotency_key=obj.get("idempotency_key", ""),
        body=MappingProxyType(obj.get("body", {})),
        timestamp=datetime.fromisoformat(obj["timestamp"]),
    )


# ---------------------------------------------------------------------------
# replay — pure reconstruction of a run from its journal
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepView:
    """One agent-loop step as reconstructed from the journal."""

    step: int
    tool: str | None  # None when the step ended in a final answer
    args: Mapping[str, Any] | None
    verdict: str | None  # from the result (or orphaned intent) if present
    ok: bool | None  # None = no result journaled
    message: str | None  # the tool message fed back to the agent
    # Intent journaled, result missing, and the verdict could have executed —
    # the action MAY have run. Matches the kernel's resume semantics exactly.
    uncertain: bool = False
    # This step's result came from an uncertain retry: a prior attempt's
    # intent had no result, and policy re-decided it on resume. The original
    # attempt may still have executed even if this verdict is a deny.
    resolved_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class RunView:
    """A whole run as reconstructed from the journal. Pure data."""

    run_id: str
    records: int
    attempts: int  # 1 + number of run.resumed markers
    steps: tuple[StepView, ...] = field(default_factory=tuple)
    final_answer: str | None = None


def replay(records: Sequence[StepRecord]) -> RunView:
    """Reconstruct a run's history from its journal. Pure function, no I/O.

    Feed it ``await store.load(run_id)`` — or records parsed from a JSONL
    file via ``step_record_from_json`` — and inspect what happened at every
    step: what the model proposed, what policy decided, what executed, and
    whether any action is in the uncertain (intent-without-result) state.

    Expects the records of ONE run; mixing runs corrupts the view (steps are
    keyed by step number).
    """
    idx = index_journal(records)
    run_id = records[0].run_id if records else ""

    steps: list[StepView] = []
    for step in sorted(idx.proposals):
        prop = idx.proposals[step]
        if prop.get("type") == "final_answer":
            steps.append(
                StepView(
                    step=step, tool=None, args=None, verdict=None, ok=True, message=prop.get("text")
                )
            )
            continue
        result = idx.results.get(step)
        intent = idx.orphan_intents.get(step)
        verdict = None
        if result is not None:
            verdict = result.get("verdict")
        elif intent is not None:
            verdict = intent.body.get("verdict")
        steps.append(
            StepView(
                step=step,
                tool=prop.get("tool"),
                args=prop.get("args"),
                verdict=verdict,
                ok=None if result is None else bool(result.get("ok")),
                message=None if result is None else result.get("message"),
                uncertain=intent is not None and is_uncertain(intent),
                resolved_uncertain=bool(result.get("uncertain_retry")) if result else False,
            )
        )

    return RunView(
        run_id=run_id,
        records=idx.records,
        attempts=idx.attempts,
        steps=tuple(steps),
        final_answer=None if idx.final_body is None else idx.final_body.get("final_answer"),
    )
