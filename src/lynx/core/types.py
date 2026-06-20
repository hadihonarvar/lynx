"""Core immutable types for Lynx.

Every type here is ``frozen=True, slots=True``. No mutation. No globals.
Pure values that flow through the kernel and out to sinks.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

__all__ = [
    "ActionRequest",
    "ActionResult",
    "ApprovalDecision",
    "ApprovalRequest",
    "AuditEvent",
    "Budget",
    "Decision",
    "ExecutionContext",
    "FinalAnswer",
    "Message",
    "Principal",
    "RunResult",
    "ToolCall",
    "ToolDef",
    "ToolMetadata",
    "ToolSet",
    "Usage",
    "Verdict",
    "canonical_json",
    "new_correlation_id",
    "now_utc",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    DRY_RUN = "dry_run"
    APPROVE_REQUIRED = "approve_required"
    TRANSFORM = "transform"


# ---------------------------------------------------------------------------
# Time / IDs
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(UTC)


def new_correlation_id() -> str:
    """A UUID4 string. Used to group all events from one ``run_agent`` call."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Principal / Budget / Context — all frozen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Principal:
    kind: Literal["user", "service", "agent"]
    id: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts for one model step, as reported by the provider.

    Adapters populate this from the API response and attach it to the
    ``ToolCall`` / ``FinalAnswer`` they return; the scheduler accumulates it,
    emits ``step.usage`` events, and enforces ``Budget`` token caps. All
    fields optional — an agent that reports nothing is simply unmetered.
    Field names align with the OpenTelemetry GenAI conventions
    (``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens``).

    The kernel never converts tokens to money — multiply these counts by
    your own rates in a sink.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class Budget:
    """Hard caps enforced by the scheduler, all checked between steps.

    **Safe by default.** ``Budget()`` ships sensible caps (``steps`` and
    ``duration_seconds``) so an agent that never returns a ``FinalAnswer``
    cannot loop forever and exhaust memory — the same fail-closed stance as the
    policy (``on_no_match: deny``) and the executor (no route → blocked). A cap
    set to ``None`` is unlimited; an individual field you set overrides its
    default. To run with **no** caps at all, say so out loud:
    ``Budget.unlimited()`` — a deliberate, readable opt-out, never a silent one.
    The scheduler also stamps the effective budget onto the ``run.started``
    audit event and emits ``run.unbounded`` when a run truly has no caps, so the
    setting is always visible.

    ``duration_seconds`` uses a monotonic clock so wall-clock jumps do not
    exhaust it; a single hung tool call is not interrupted (use a tool-level
    timeout for that).

    Token caps (``input_tokens`` / ``output_tokens`` / ``tokens`` = combined)
    are enforced against adapter-reported ``Usage`` counts. Like every
    in-loop limiter, they stop the *next* model call — the step that crossed
    the cap has already happened. Agents that report no usage are not
    metered; the caps simply never trigger.

    ``step_timeout_seconds`` is the exception to "checked between steps": it
    wraps each ``agent.step()`` call itself, so a hung provider connection
    fails the run instead of hanging it forever. It does NOT cover tool
    execution — bound tools at the executor seam
    (``inline_executor(timeout_seconds=...)`` / ``subprocess_executor``).
    """

    # Default caps: bound every run unless explicitly opted out. ~10 minutes /
    # 50 steps is generous for most tasks and a hard ceiling against runaways.
    duration_seconds: int | None = 600
    steps: int | None = 50
    input_tokens: int | None = None
    output_tokens: int | None = None
    tokens: int | None = None  # combined input + output
    step_timeout_seconds: float | None = None  # per agent.step() model call
    # Trip the run if the same (tool, args) action is proposed more than this
    # many times — the classic "agent calls the same tool in a loop" failure.
    # None = no limit. Identical calls are keyed by tool + canonical args, so a
    # genuinely different argument resets the streak.
    max_repeated_calls: int | None = None

    @classmethod
    def unlimited(cls) -> Budget:
        """A budget with **no** caps — the explicit, readable opt-out.

        Use only when you genuinely want an unbounded run (and own the risk of
        an agent looping forever). The scheduler flags it with ``run.unbounded``.
        """
        return cls(duration_seconds=None, steps=None)

    def is_unbounded(self) -> bool:
        """True when nothing bounds the run length: no step, duration, or token cap."""
        return self.steps is None and self.duration_seconds is None and self.tokens is None


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    principal: Principal
    environment: str
    workspace: str
    correlation_id: str
    step_seq: int
    timestamp: datetime
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


# ---------------------------------------------------------------------------
# Tool metadata + ToolDef + ToolSet (immutable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    cost: Literal["low", "medium", "high"]
    reversible: bool
    scope: tuple[str, ...]
    blast_radius_hint: int | None = None
    has_shadow: bool = False
    # Routing hint for route_executor: which executor should run this tool
    # ("subprocess", "container", ... — your vocabulary). None = default
    # route. A hint, not a guarantee — enforcement is the executor's job.
    isolation: str | None = None
    # Routing hint for route_compressor: which result compressor (if any)
    # should shrink this tool's output before it enters the model context
    # ("verbose", "logs", ... — your vocabulary). None = default route. A
    # hint, not a guarantee — a missing route simply means no compression.
    compress: str | None = None


@dataclass(frozen=True, slots=True)
class ToolDef:
    """A tool ready to be passed to ``run_agent``.

    Holds the (real) function, an optional shadow, and the declared metadata.
    All references; no execution state.
    """

    name: str
    description: str
    fn: Callable[..., Awaitable[Any]]
    shadow_fn: Callable[..., Awaitable[Any]] | None
    metadata: ToolMetadata


@dataclass(frozen=True, slots=True)
class ToolSet:
    """An immutable mapping of tool name to ToolDef.

    Build with ``ToolSet.from_functions(*fns)``; operations return new sets.
    """

    tools: Mapping[str, ToolDef] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_functions(cls, *fns: Callable[..., Awaitable[Any]]) -> ToolSet:
        """Build a ToolSet from functions decorated with ``@tool``.

        Each function must carry ``__lynx_meta__`` (set by the decorator).
        Functions without that attribute raise ``TypeError``. Two tools with the
        same name raise ``ValueError`` — silent overwrite would be a footgun.
        """
        out: dict[str, ToolDef] = {}
        for fn in fns:
            meta = getattr(fn, "__lynx_meta__", None)
            if meta is None:
                raise TypeError(
                    f"{fn.__name__} is not decorated with @tool — cannot include in ToolSet"
                )
            if meta.name in out:
                raise ValueError(f"Duplicate tool name {meta.name!r} in ToolSet.from_functions")
            out[meta.name] = meta
        return cls(tools=MappingProxyType(out))

    def with_tool(self, t: ToolDef) -> ToolSet:
        if t.name in self.tools:
            raise ValueError(
                f"ToolSet already contains a tool named {t.name!r}; "
                "use without_tool first or build a new ToolSet"
            )
        return ToolSet(tools=MappingProxyType({**self.tools, t.name: t}))

    def without_tool(self, name: str) -> ToolSet:
        new = dict(self.tools)
        new.pop(name, None)
        return ToolSet(tools=MappingProxyType(new))

    def union(self, other: ToolSet) -> ToolSet:
        overlap = set(self.tools) & set(other.tools)
        if overlap:
            raise ValueError(f"ToolSet.union collision on names: {sorted(overlap)}")
        return ToolSet(tools=MappingProxyType({**self.tools, **other.tools}))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.tools.keys()))

    def get(self, name: str) -> ToolDef:
        if name not in self.tools:
            raise KeyError(f"Unknown tool: {name}")
        return self.tools[name]

    def __len__(self) -> int:
        return len(self.tools)


# ---------------------------------------------------------------------------
# Agent conversation primitives — frozen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    # When set on an assistant message, this records the args of a tool call
    # the assistant emitted. Adapters use this to reconstruct provider-specific
    # tool_use / tool_calls blocks on subsequent turns, so providers see a
    # well-formed assistant→tool→assistant alternation.
    tool_call_args: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool: str
    args: Mapping[str, Any]
    call_id: str = ""
    usage: Usage | None = None  # set by adapters from the provider response


@dataclass(frozen=True, slots=True)
class FinalAnswer:
    text: str
    usage: Usage | None = None  # set by adapters from the provider response


# ---------------------------------------------------------------------------
# Request / Decision / Result — frozen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionRequest:
    tool: str
    args: Mapping[str, Any]
    declared: ToolMetadata
    context: ExecutionContext


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    reason: str = ""
    matched_rules: tuple[str, ...] = ()
    approvers: tuple[str, ...] = ()
    transform_args: Mapping[str, Any] | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    value: Any | None = None
    error: str | None = None
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Approval types — frozen, sync-handler-only
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request: ActionRequest
    decision: Decision
    correlation_id: str


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    granted: bool
    approver: str
    reason: str = ""


# ---------------------------------------------------------------------------
# Audit event — sinks consume this; no hash chain
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditEvent:
    correlation_id: str
    bundle_id: str
    seq: int
    kind: str
    timestamp: datetime
    body: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Final result of a run — frozen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunResult:
    correlation_id: str
    bundle_id: str
    final_answer: str | None = None
    error: str | None = None
    steps_taken: int = 0
    # Lifetime token totals (includes journal-replayed steps on resume).
    # None when the agent reported no usage at all.
    usage: Usage | None = None


# ---------------------------------------------------------------------------
# Canonical JSON (still needed for bundle_id hashing in policy module)
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """Sorted-keys, no-whitespace JSON. RFC 8785 / JCS-ish."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if isinstance(o, Mapping):
        return dict(o)
    if hasattr(o, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(o)
    # Fallback: a sink getting a tool-returned ``bytes``/``Path``/``Decimal`` must
    # never crash the run. Degrade to repr() so audit lines stay readable.
    return repr(o)
