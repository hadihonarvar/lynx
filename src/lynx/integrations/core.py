"""``ToolGuard`` — the framework-agnostic governed-tool-call primitive.

Every framework-native integration needs the same three steps for one tool call:
build an :class:`ActionRequest` from the proposed call, run the pure PDP
(``evaluate``), and enforce the verdict (``mediate``). ``ToolGuard`` is exactly
that — and nothing more. It owns no loop, no conversation, no model. A framework
shim calls :meth:`ToolGuard.check` at the framework's native tool hook and maps
the returned :class:`GovernedCall` onto the framework's control flow.

It deliberately reuses the same kernel functions the scheduler uses, so a tool
call governed through a framework integration gets identical verdict semantics,
identical executor routing, and identical audit events to one governed through
``run_agent``. Mechanism, not policy: the developer brings the tools, the policy,
the principal, and the approval handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lynx.core.mediator import mediate
from lynx.core.policy import LayeredPolicyBundle, PolicyBundle, evaluate
from lynx.core.types import (
    ActionRequest,
    ActionResult,
    AuditEvent,
    Decision,
    ExecutionContext,
    Principal,
    ToolMetadata,
    ToolSet,
    Verdict,
    new_correlation_id,
    now_utc,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lynx.approvals import ApprovalHandler
    from lynx.executors import Executor
    from lynx.sinks import Sink

__all__ = ["GovernedCall", "ToolGuard"]

# Fail-closed metadata for an unknown tool: irreversible, no shadow, costly.
_UNKNOWN_METADATA = ToolMetadata(cost="high", reversible=False, scope=(), has_shadow=False)


@dataclass(frozen=True, slots=True)
class GovernedCall:
    """The outcome of governing one tool call.

    ``decision`` is the pure PDP verdict (with layer-tagged ``matched_rules``);
    ``result`` is what enforcement produced — the real tool's output on ALLOW /
    TRANSFORM / approved, a denial on DENY / refused / timeout, or the shadow
    preview on DRY_RUN. ``request`` is the exact action that was evaluated.
    """

    decision: Decision
    result: ActionResult
    request: ActionRequest

    @property
    def allowed(self) -> bool:
        """True iff the action actually executed (or was previewed) successfully."""
        return self.result.ok


class ToolGuard:
    """Govern individual tool calls when a framework owns the agent loop.

    Construct once with the same inputs you'd pass to ``run_agent`` (tools,
    policy, principal, approval handler, executor, sinks); then call
    :meth:`check` per tool invocation from inside the framework's native hook.

    Stateless except for a monotonic ``seq`` used to order this guard's audit
    events. ``check`` is safe to call concurrently for distinct calls; the seq
    counter is the only shared mutable state and only labels events.
    """

    def __init__(
        self,
        *,
        tools: ToolSet,
        policy: PolicyBundle | LayeredPolicyBundle,
        principal: Principal = Principal(kind="user", id="anonymous"),
        environment: str = "dev",
        workspace: str = ".",
        on_approval: ApprovalHandler | None = None,
        executor: Executor | None = None,
        sinks: Sequence[Sink] = (),
        correlation_id: str | None = None,
    ) -> None:
        from lynx.approvals import auto_deny  # local import to avoid a cycle

        self._tools = tools
        self._policy = policy
        self._principal = principal
        self._environment = environment
        self._workspace = workspace
        self._on_approval = on_approval or auto_deny("no on_approval handler configured")
        self._executor = executor
        self._sinks: tuple[Sink, ...] = tuple(sinks)
        self._cid = correlation_id or new_correlation_id()
        self._seq = 0

    @property
    def correlation_id(self) -> str:
        """The id stamped on every audit event this guard emits — use it to group
        a guard's governed calls into one trace."""
        return self._cid

    async def check(
        self,
        tool_name: str,
        args: Mapping[str, object],
        *,
        extra: Mapping[str, object] | None = None,
    ) -> GovernedCall:
        """Evaluate and enforce one proposed tool call.

        Builds the request from the tool's declared metadata, runs the pure PDP,
        emits ``policy.evaluated`` + an outcome event to the sinks, and enforces
        the verdict through the mediator. An unknown tool is denied (fail-closed),
        never executed. Returns a :class:`GovernedCall`.
        """
        seq = self._next_seq()
        await self._emit("step.proposed", {"seq": seq, "tool": tool_name, "args": dict(args)})

        try:
            tool_def = self._tools.get(tool_name)
        except KeyError:
            # Fail closed: an unknown tool is denied and never executed. We still
            # run the full emit sequence (policy.evaluated + outcome) so the audit
            # stream is uniform with the known-tool path. The request we build for
            # the trail uses fail-closed metadata (irreversible, no shadow).
            reason = f"unknown tool: {tool_name!r}"
            request = self._build_request(tool_name, args, extra, seq=seq, declared=_UNKNOWN_METADATA)
            decision = Decision(
                verdict=Verdict.DENY, reason=reason, matched_rules=("<unknown_tool>",)
            )
            await self._emit_evaluated(seq, decision)
            await self._emit("action.failed", {"seq": seq, "tool": tool_name, "reason": reason})
            return GovernedCall(
                decision=decision, result=ActionResult(ok=False, error=reason), request=request
            )

        request = self._build_request(tool_name, args, extra, seq=seq, declared=tool_def.metadata)
        decision = evaluate(self._policy, request, request.context)
        await self._emit_evaluated(seq, decision)

        result = await mediate(request, decision, self._tools, self._on_approval, self._executor)
        await self._emit(
            "action.completed" if result.ok else "action.failed",
            {"seq": seq, "tool": tool_name, "verdict": decision.verdict.value, "ok": result.ok},
        )
        return GovernedCall(decision=decision, result=result, request=request)

    # -- internals ----------------------------------------------------------

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    async def _emit_evaluated(self, seq: int, decision: Decision) -> None:
        await self._emit(
            "policy.evaluated",
            {
                "seq": seq,
                "verdict": decision.verdict.value,
                "reason": decision.reason,
                "matched_rules": list(decision.matched_rules),
            },
        )

    def _build_request(
        self,
        tool_name: str,
        args: Mapping[str, object],
        extra: Mapping[str, object] | None,
        *,
        seq: int,
        declared: ToolMetadata,
    ) -> ActionRequest:
        return ActionRequest(
            tool=tool_name,
            args=dict(args),
            declared=declared,
            context=ExecutionContext(
                principal=self._principal,
                environment=self._environment,
                workspace=self._workspace,
                correlation_id=self._cid,
                step_seq=seq,
                timestamp=now_utc(),
                extra=dict(extra) if extra else {},
            ),
        )

    async def _emit(self, kind: str, body: dict[str, object]) -> None:
        if not self._sinks:
            return
        raw_seq = body.get("seq", 0)
        event = AuditEvent(
            correlation_id=self._cid,
            bundle_id=self._policy.id,
            seq=raw_seq if isinstance(raw_seq, int) else 0,
            kind=kind,
            timestamp=now_utc(),
            body=body,
        )
        for sink in self._sinks:
            await sink(event)
