"""Core data types. Frozen dataclasses, no I/O.

These six types are the entire vocabulary of the kernel. Every other module
operates on them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from ulid import ULID

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    DRY_RUN = "dry_run"
    APPROVE_REQUIRED = "approve_required"
    TRANSFORM = "transform"


TERMINAL_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})


# ---------------------------------------------------------------------------
# Identifier helpers
# ---------------------------------------------------------------------------


def new_id(prefix: str) -> str:
    """Return a ULID with the given single-letter prefix, e.g. T-01HF..."""
    return f"{prefix}-{ULID()}"


def now_utc() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Principal:
    """Who the agent is acting on behalf of."""

    kind: Literal["user", "service", "agent"]
    id: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Budget:
    """Hard caps enforced by the scheduler."""

    usd: float | None = None
    duration_seconds: int | None = None
    tokens: int | None = None
    steps: int | None = None


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """Declared properties of a tool. Used by the PDP for policy matching."""

    cost: Literal["low", "medium", "high"]
    reversible: bool
    scope: tuple[str, ...]
    blast_radius_hint: int | None = None
    has_shadow: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Per-action context. Set by the kernel, not the tool."""

    principal: Principal
    environment: str
    workspace: str
    run_id: str
    step_seq: int
    timestamp: datetime
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelCall:
    """Record of one LLM invocation. Used for cost and replay determinism."""

    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int
    prompt_hash: str


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Outcome of executing (or shadow-executing) a tool."""

    ok: bool
    value: Any | None = None
    error: str | None = None
    duration_ms: int = 0
    side_effects: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# The six core types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Task:
    """The user's stated goal."""

    id: str
    goal: str
    created_at: datetime
    created_by: Principal
    policy_bundle_id: str
    budget: Budget
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        goal: str,
        created_by: Principal,
        policy_bundle_id: str,
        budget: Budget | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        return cls(
            id=new_id("T"),
            goal=goal,
            created_at=now_utc(),
            created_by=created_by,
            policy_bundle_id=policy_bundle_id,
            budget=budget or Budget(),
            metadata=metadata or {},
        )


@dataclass
class Run:
    """One execution attempt of a Task."""

    id: str
    task_id: str
    status: RunStatus
    started_at: datetime
    ended_at: datetime | None = None
    resume_token: str | None = None
    last_step_seq: int = -1
    error: str | None = None

    @classmethod
    def create(cls, task_id: str) -> Run:
        return cls(
            id=new_id("R"),
            task_id=task_id,
            status=RunStatus.PENDING,
            started_at=now_utc(),
        )

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """A proposed tool call. Input to the PDP."""

    tool: str
    args: dict[str, Any]
    declared: ToolMetadata
    context: ExecutionContext
    idempotency_key: str

    @classmethod
    def build(
        cls,
        tool: str,
        args: dict[str, Any],
        declared: ToolMetadata,
        context: ExecutionContext,
    ) -> ActionRequest:
        key = compute_idempotency_key(
            run_id=context.run_id,
            seq=context.step_seq,
            tool=tool,
            args=args,
        )
        return cls(
            tool=tool,
            args=args,
            declared=declared,
            context=context,
            idempotency_key=key,
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """The PDP's verdict on an ActionRequest."""

    verdict: Verdict
    reason: str = ""
    matched_rules: tuple[str, ...] = ()
    approvers: tuple[str, ...] = ()
    transform_args: dict[str, Any] | None = None
    timeout_seconds: int | None = None


@dataclass
class Step:
    """One iteration of the agent loop."""

    id: str
    run_id: str
    seq: int
    started_at: datetime
    ended_at: datetime
    checkpoint_blob: bytes
    model_call: ModelCall | None = None
    action: ActionRequest | None = None
    decision: Decision | None = None
    result: ActionResult | None = None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Append-only, hash-chained event in the run's audit log."""

    id: str
    prev: str
    run_id: str
    seq: int
    kind: str
    timestamp: datetime
    body: dict[str, Any]
    signature: bytes | None = None

    @classmethod
    def build(
        cls,
        prev: str,
        run_id: str,
        seq: int,
        kind: str,
        body: dict[str, Any],
    ) -> AuditEvent:
        timestamp = now_utc()
        normalized = {
            "prev": prev,
            "run_id": run_id,
            "seq": seq,
            "kind": kind,
            "timestamp": timestamp.isoformat(),
            "body": body,
        }
        event_id = hashlib.sha256(canonical_json(normalized).encode()).hexdigest()
        return cls(
            id=event_id,
            prev=prev,
            run_id=run_id,
            seq=seq,
            kind=kind,
            timestamp=timestamp,
            body=body,
        )


GENESIS_HASH = "0" * 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """Sorted-keys, no-whitespace JSON. Approximates RFC 8785 / JCS."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    raise TypeError(f"Cannot canonicalize {type(o).__name__}")


def compute_idempotency_key(run_id: str, seq: int, tool: str, args: dict[str, Any]) -> str:
    """Deterministic idempotency key for an action.

    Same (run_id, seq, tool, args) always produces the same key.
    """
    payload = canonical_json({"run_id": run_id, "seq": seq, "tool": tool, "args": args})
    return hashlib.sha256(payload.encode()).hexdigest()
