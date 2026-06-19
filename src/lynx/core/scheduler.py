"""Scheduler.

A single pure-ish async function: ``run_agent``. No classes. No globals.
The agent step loop with policy enforcement, streaming audit events, and
(opt-in) durable journaling to a user-owned ``RunStore``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from lynx.core.mediator import mediate
from lynx.core.policy import PolicyBundle, evaluate
from lynx.core.types import (
    ActionRequest,
    ActionResult,
    AuditEvent,
    Budget,
    ExecutionContext,
    FinalAnswer,
    Message,
    Principal,
    RunResult,
    ToolCall,
    ToolSet,
    Usage,
    canonical_json,
    new_correlation_id,
    now_utc,
)
from lynx.durability import (
    DuplicateRecord,
    JournalIndex,
    StepRecord,
    action_from_body,
    action_to_body,
    idempotency_key,
    index_journal,
    is_uncertain,
)

if TYPE_CHECKING:
    from lynx.approvals import ApprovalHandler
    from lynx.cancel import Cancelled
    from lynx.compressors import Compressor
    from lynx.durability import RunStore
    from lynx.executors import Executor
    from lynx.sdk import Agent
    from lynx.sinks import Sink


__all__ = ["run_agent"]


class _StoreFailed(Exception):
    """Internal: the user's RunStore raised. The run must stop — executing
    side effects that cannot be journaled would silently void the
    no-double-execution guarantee."""


_EMPTY_EXTRA: Mapping[str, Any] = MappingProxyType({})
_UNCERTAIN_EXTRA: Mapping[str, Any] = MappingProxyType({"uncertain_retry": True})

_EMPTY_INDEX = JournalIndex(
    records=0,
    attempts=1,
    proposals={},
    orphan_intents={},
    results={},
    final_body=None,
    last_bundle_id=None,
    next_seq=0,
)


async def run_agent(
    agent: Agent,
    task: str,
    *,
    tools: ToolSet,
    policy: PolicyBundle,
    sinks: Sequence[Sink] = (),
    on_approval: ApprovalHandler | None = None,
    budget: Budget = Budget(),  # unlimited — only caps you set are enforced
    principal: Principal = Principal(kind="user", id="anonymous"),
    environment: str = "dev",
    workspace: str = ".",
    correlation_id: str | None = None,
    store: RunStore | None = None,
    run_id: str | None = None,
    executor: Executor | None = None,
    cancel: Cancelled | None = None,
    compressor: Compressor | None = None,
) -> RunResult:
    """Run an agent through one task. Stateless unless you pass a store.

    Args:
        agent:        Anything implementing ``async step(conversation) -> ToolCall | FinalAnswer``.
        task:         The user's goal — becomes the first user Message.
        tools:        Immutable ToolSet of @tool-decorated functions.
        policy:       Compiled PolicyBundle (use compile_policy / load_policy_file).
        sinks:        Iterable of Sink callables. Each event is fanned out.
        on_approval:  Sync handler for APPROVE_REQUIRED. Defaults to auto-deny.
        budget:       Hard caps on steps / duration / tokens / step timeout.
                      Defaults to NO caps: only what you define is enforced —
                      an unbudgeted agent that never answers runs forever, so
                      set at least steps or duration_seconds in production.
        principal:    Who the agent is acting on behalf of.
        environment:  e.g. "dev" / "staging" / "prod" — policy can match on this.
        workspace:    Filesystem context the agent works in.
        correlation_id: Optional override. Default: the ``run_id`` for a
                      fresh journaled run; ``"<run_id>#<suffix>"`` for any
                      re-invocation of a journaled run (so (correlation_id,
                      seq) stays unique across attempts while remaining
                      groupable by run_id prefix); a new UUID4 otherwise.
        store:        Optional user-owned ``RunStore``. When set, the run is
                      journaled and ``run_id`` becomes required. Re-invoking
                      with the same ``run_id`` resumes: journaled model
                      outputs are replayed (the model is not re-called) and
                      journaled action results are returned without
                      re-executing the action.
        run_id:       Stable, non-empty identifier for the journaled run.
                      Required with ``store``; pick something your
                      retry/queue layer keeps stable across attempts.
        executor:     Where approved actions execute. Default: in-process
                      (identical to pre-seam behavior). Pass
                      ``subprocess_executor()``, ``route_executor({...})``,
                      or your own Docker/microVM ``Executor`` — Lynx defines
                      the seam; the isolation behind it is yours. Dry-runs
                      always call the shadow in-process.
        cancel:       Optional kill-switch (``CancelToken`` or any object with
                      ``cancelled``/``reason``). Checked at every step boundary
                      and immediately before each tool executes; once tripped
                      the run stops with ``error="cancelled: <reason>"`` after
                      at most one in-flight model or tool call.
        compressor:   Optional ``Compressor`` (token optimization seam). Each
                      fresh *successful, string-valued* tool result is passed
                      through it before entering the conversation, so the
                      compressed text is what the model sees, what the journal
                      records, and what a resumed run replays (errors and
                      non-string values bypass it). Default: ``None`` (no
                      compression). Fails open — a compressor that raises is
                      logged via ``step.compress_failed`` and the original
                      result is used, never dropped. Replayed results are not
                      re-compressed (they were already compressed when first
                      journaled).

    Returns:
        ``RunResult`` with final_answer, error, steps_taken, correlation_id,
        bundle_id. If another worker overtakes the run (the store reports a
        duplicate journal position), ``error`` starts with ``"superseded:"``
        — that prefix is a stable, documented part of the API.

    Durability semantics (only with a store):
      * Write-ahead intent: every action is journaled before it executes.
        A crash after the intent but before the result leaves the action
        *uncertain* — on resume it is re-proposed to policy with
        ``context.extra.uncertain_retry = True`` so rules can deny it,
        require approval, or let idempotent tools re-run.
      * Budgets count replayed steps too, and ``duration_seconds`` is
        per-attempt (monotonic clock of the current process). To continue a
        run that exhausted its step budget, resume it with a larger budget.
      * Tool args and results should be JSON-serializable (LLM tool calls
        always are); non-JSON values degrade to ``repr()`` in stores that
        serialize, which makes replayed values drift.
      * Resuming with a different policy bundle than the journal was written
        with emits a ``run.bundle_changed`` warning event and continues;
        replayed results always reflect what actually happened, not what
        current policy would decide. Resuming with a different ToolSet, or
        with an agent that is not a pure function of the conversation
        (e.g. the single-shot CrewAI adapter), is out of contract.
      * Lynx does not restart dead processes — your supervisor does. Lynx
        makes the restart cheap (no re-burned tokens) and safe (no double
        side effects).

    No state is held after this function returns. Sinks are called as events
    happen and never buffered. The conversation is freed at function exit.
    """
    from lynx.approvals import auto_deny  # local import to avoid cycle

    if store is not None and not run_id:
        raise ValueError(
            "a non-empty run_id is required when store is provided — resume needs "
            "a stable identifier your retry layer keeps constant across attempts"
        )

    on_approval = on_approval or auto_deny("no on_approval handler configured")
    if executor is None:
        from lynx.executors import inline_executor  # local import to avoid cycle

        executor = inline_executor()
    cid = correlation_id or run_id or new_correlation_id()
    sinks_tuple: tuple[Sink, ...] = tuple(sinks)

    started_monotonic = time.monotonic()
    seq_counter = 0
    started_emitted = False
    step_seq = 0
    call_counts: dict[str, int] = {}  # idempotency_key -> times proposed (repetition gate)

    async def emit(kind: str, body_payload: dict) -> int:
        nonlocal seq_counter
        event = AuditEvent(
            correlation_id=cid,
            bundle_id=policy.id,
            seq=seq_counter,
            kind=kind,
            timestamp=now_utc(),
            body=body_payload,
        )
        seq_counter += 1
        if sinks_tuple:
            results = await asyncio.gather(
                *(s(event) for s in sinks_tuple),
                return_exceptions=True,
            )
            for sink_obj, outcome in zip(sinks_tuple, results, strict=True):
                if isinstance(outcome, BaseException):
                    # A sink failed. Don't let it kill the run, but don't be
                    # silent either — log to stderr so operators can see it.
                    sink_name = getattr(sink_obj, "__qualname__", repr(sink_obj))
                    print(
                        f"[lynx] sink {sink_name} failed on event "
                        f"{event.kind!r} seq={event.seq}: "
                        f"{type(outcome).__name__}: {outcome}",
                        file=sys.stderr,
                    )
        return event.seq

    # ---- token meter: lifetime totals from adapter-reported Usage.
    # Replayed steps count too (they were real spend in a prior attempt).
    used_input = 0
    used_output = 0
    used_cache_read = 0
    used_cache_write = 0
    any_usage = False

    def record_usage(u: Usage | None) -> None:
        nonlocal used_input, used_output, used_cache_read, used_cache_write, any_usage
        if u is None:
            return
        any_usage = True
        used_input += u.input_tokens or 0
        used_output += u.output_tokens or 0
        used_cache_read += u.cache_read_tokens or 0
        used_cache_write += u.cache_write_tokens or 0

    def usage_totals() -> Usage | None:
        if not any_usage:
            return None
        return Usage(
            input_tokens=used_input,
            output_tokens=used_output,
            cache_read_tokens=used_cache_read,
            cache_write_tokens=used_cache_write,
        )

    idx = _EMPTY_INDEX
    journal_seq = 0

    async def journal(kind: str, body: dict[str, Any], idem: str = "") -> None:
        """Append one record. DuplicateRecord propagates (it's the
        superseded signal); any other store failure stops the run."""
        nonlocal journal_seq
        assert store is not None and run_id is not None
        record = StepRecord(
            run_id=run_id,
            seq=journal_seq,
            kind=kind,
            idempotency_key=idem,
            body=body,
            timestamp=now_utc(),
        )
        try:
            await store.append(record)
        except (DuplicateRecord, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise _StoreFailed(f"store.append failed: {type(exc).__name__}: {exc}") from exc
        journal_seq += 1

    try:
        # ---- load + index the journal (before any event: the attempt
        # number decides the correlation id)
        if store is not None:
            try:
                prior = await store.load(run_id or "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _StoreFailed(f"store.load failed: {type(exc).__name__}: {exc}") from exc
            idx = index_journal(prior)
            journal_seq = idx.next_seq
            if idx.records and correlation_id is None:
                # A re-invocation: give this attempt its own audit identity so
                # (correlation_id, seq) never collides with a prior attempt's
                # events, while staying groupable by the run_id prefix.
                cid = f"{run_id}#{new_correlation_id()[:8]}"

        await emit("run.started", {"task": task, "principal_id": principal.id})
        started_emitted = True

        if store is not None and idx.last_bundle_id is not None and idx.last_bundle_id != policy.id:
            await emit(
                "run.bundle_changed",
                {
                    "journaled_bundle_id": idx.last_bundle_id,
                    "current_bundle_id": policy.id,
                    "detail": "resuming under a different policy than the journal was written with",
                },
            )

        # ---- a completed run resumes to the same answer, idempotently
        if idx.final_body is not None:
            answer = idx.final_body.get("final_answer")
            for body in idx.proposals.values():  # lifetime totals from the journal
                record_usage(action_from_body(body).usage)
            await emit("run.succeeded", {"final_answer": answer, "replayed": True})
            return RunResult(
                correlation_id=cid,
                bundle_id=policy.id,
                final_answer=answer,
                steps_taken=int(idx.final_body.get("step", 0)),
                usage=usage_totals(),
            )

        # ---- claim the journal before any model call, so two simultaneous
        # workers resolve here with zero wasted tokens
        if store is not None:
            if idx.records:
                await journal("run.resumed", {"records": idx.records, "bundle_id": policy.id})
                await emit("run.resumed", {"records": idx.records, "attempt": idx.attempts + 1})
            else:
                await journal("run.started", {"task": task, "bundle_id": policy.id})

        conversation: tuple[Message, ...] = (Message(role="user", content=task),)

        while True:
            # ---- kill-switch: stop before doing any more work
            if cancel is not None and cancel.cancelled:
                reason = cancel.reason or "cancelled by caller"
                await emit("run.cancelled", {"reason": reason, "at": "step_boundary"})
                return RunResult(
                    correlation_id=cid,
                    bundle_id=policy.id,
                    error=f"cancelled: {reason}",
                    steps_taken=step_seq,
                    usage=usage_totals(),
                )

            # ---- budget enforcement
            if budget.steps is not None and step_seq >= budget.steps:
                await emit("run.failed", {"reason": f"step budget {budget.steps} exhausted"})
                return RunResult(
                    correlation_id=cid,
                    bundle_id=policy.id,
                    error=f"step budget exhausted ({budget.steps})",
                    steps_taken=step_seq,
                    usage=usage_totals(),
                )
            if (
                budget.duration_seconds is not None
                and time.monotonic() - started_monotonic >= budget.duration_seconds
            ):
                await emit("run.failed", {"reason": "duration budget exhausted"})
                return RunResult(
                    correlation_id=cid,
                    bundle_id=policy.id,
                    error=f"duration budget exhausted ({budget.duration_seconds}s)",
                    steps_taken=step_seq,
                    usage=usage_totals(),
                )
            token_overage: str | None = None
            if budget.input_tokens is not None and used_input >= budget.input_tokens:
                token_overage = f"input token budget exhausted ({budget.input_tokens})"
            elif budget.output_tokens is not None and used_output >= budget.output_tokens:
                token_overage = f"output token budget exhausted ({budget.output_tokens})"
            elif budget.tokens is not None and used_input + used_output >= budget.tokens:
                token_overage = f"token budget exhausted ({budget.tokens})"
            if token_overage is not None:
                await emit("run.failed", {"reason": token_overage})
                return RunResult(
                    correlation_id=cid,
                    bundle_id=policy.id,
                    error=token_overage,
                    steps_taken=step_seq,
                    usage=usage_totals(),
                )

            # ---- next action: replay the journal if it has this step,
            # otherwise ask the agent (and journal what it said)
            replayed_proposal = idx.proposals.get(step_seq)
            if replayed_proposal is not None:
                action = action_from_body(replayed_proposal)
                # Replayed usage counts toward totals and budgets (it was real
                # spend in a prior attempt) but step.usage is NOT re-emitted —
                # the attempt that paid for it already announced it.
                record_usage(action.usage)
            else:
                try:
                    if budget.step_timeout_seconds is not None:
                        # A hung provider call fails the run instead of
                        # hanging it forever. Cancellation propagates into
                        # the adapter's HTTP client; nothing has been
                        # journaled for this step, so resume re-asks cleanly.
                        action = await asyncio.wait_for(
                            agent.step(conversation), timeout=budget.step_timeout_seconds
                        )
                    else:
                        action = await agent.step(conversation)
                except TimeoutError:
                    reason = f"agent.step timed out after {budget.step_timeout_seconds}s"
                    await emit("run.failed", {"reason": reason})
                    return RunResult(
                        correlation_id=cid,
                        bundle_id=policy.id,
                        error=reason,
                        steps_taken=step_seq,
                        usage=usage_totals(),
                    )
                except Exception as exc:
                    await emit("run.failed", {"reason": f"agent.step raised: {exc!r}"})
                    return RunResult(
                        correlation_id=cid,
                        bundle_id=policy.id,
                        error=f"agent.step raised: {type(exc).__name__}: {exc}",
                        steps_taken=step_seq,
                        usage=usage_totals(),
                    )
                if store is not None:
                    await journal("model.output", action_to_body(action, step_seq))
                if action.usage is not None:
                    record_usage(action.usage)
                    u = action.usage
                    await emit(
                        "step.usage",
                        {
                            "seq": step_seq,
                            "model": u.model,
                            "input_tokens": u.input_tokens,
                            "output_tokens": u.output_tokens,
                            "cache_read_tokens": u.cache_read_tokens,
                            "cache_write_tokens": u.cache_write_tokens,
                            "total_input": used_input,
                            "total_output": used_output,
                        },
                    )

            if isinstance(action, FinalAnswer):
                # Journal the final answer BEFORE announcing success, so a
                # crash in between resumes to "already final", not a re-run.
                if store is not None:
                    await journal("final", {"step": step_seq, "final_answer": action.text})
                success_body: dict[str, Any] = {"final_answer": action.text}
                totals = usage_totals()
                if totals is not None:
                    success_body["usage"] = {
                        "input_tokens": totals.input_tokens,
                        "output_tokens": totals.output_tokens,
                    }
                await emit("run.succeeded", success_body)
                return RunResult(
                    correlation_id=cid,
                    bundle_id=policy.id,
                    final_answer=action.text,
                    steps_taken=step_seq,
                    usage=totals,
                )

            assert isinstance(action, ToolCall)

            # Always record the assistant's tool-call attempt FIRST so adapters
            # translating to provider-specific shapes (Anthropic tool_use blocks,
            # OpenAI tool_calls) emit a well-formed assistant→tool alternation.
            assistant_call_id = action.call_id or f"step_{step_seq}"
            conversation = (
                *conversation,
                Message(
                    role="assistant",
                    content="",
                    name=action.tool,
                    tool_call_id=assistant_call_id,
                    tool_call_args=dict(action.args),
                ),
            )

            # ---- replay a journaled result: the action already happened in a
            # prior attempt; feed the recorded outcome back without policy
            # re-evaluation (the journal records what HAPPENED) or execution.
            replayed_result = idx.results.get(step_seq)
            if replayed_result is not None:
                await emit(
                    "step.replayed",
                    {
                        "seq": step_seq,
                        "tool": replayed_result.get("tool"),
                        "ok": replayed_result.get("ok"),
                        "verdict": replayed_result.get("verdict"),
                    },
                )
                conversation = (
                    *conversation,
                    Message(
                        role="tool",
                        content=str(replayed_result.get("message", "")),
                        tool_call_id=assistant_call_id,
                        name=action.tool,
                    ),
                )
                step_seq += 1
                continue

            # ---- uncertain retry: a prior attempt journaled an intent for
            # this step but no result — the action MAY have executed. Flag it
            # so policy can decide (deny / approve_required / re-run).
            orphan = idx.orphan_intents.get(step_seq)
            uncertain = orphan is not None and is_uncertain(orphan)

            # ---- build the ActionRequest using tool's declared metadata
            try:
                tool_def = tools.get(action.tool)
            except KeyError:
                await emit(
                    "step.proposed",
                    {"seq": step_seq, "tool": action.tool, "args": dict(action.args)},
                )
                if uncertain:
                    # Surface the orphan even though the tool is now unknown
                    # (resuming with a changed ToolSet is out of contract,
                    # but a possibly-executed action must never go silent).
                    await emit(
                        "action.uncertain",
                        {
                            "seq": step_seq,
                            "tool": action.tool,
                            "intent_key": orphan.idempotency_key if orphan else "",
                            "reason": "intent journaled without result — action may have executed",
                        },
                    )
                denial_msg = f"unknown tool: {action.tool!r}"
                await emit("action.failed", {"seq": step_seq, "reason": denial_msg})
                conversation = (
                    *conversation,
                    Message(
                        role="tool",
                        content=f"[error] {denial_msg}",
                        tool_call_id=assistant_call_id,
                        name=action.tool,
                    ),
                )
                step_seq += 1
                continue

            request = ActionRequest(
                tool=action.tool,
                args=dict(action.args),
                declared=tool_def.metadata,
                context=ExecutionContext(
                    principal=principal,
                    environment=environment,
                    workspace=workspace,
                    correlation_id=cid,
                    step_seq=step_seq,
                    timestamp=now_utc(),
                    extra=_UNCERTAIN_EXTRA if uncertain else _EMPTY_EXTRA,
                ),
            )

            await emit(
                "step.proposed",
                {"seq": step_seq, "tool": request.tool, "args": dict(request.args)},
            )

            # ---- repetition gate: the classic "same tool, same args, forever"
            # loop. Keyed on tool + canonical args (step-independent), so a
            # genuinely different argument never trips it.
            if budget.max_repeated_calls is not None:
                repeat_key = f"{request.tool}|{canonical_json(dict(request.args))}"
                call_counts[repeat_key] = call_counts.get(repeat_key, 0) + 1
                if call_counts[repeat_key] > budget.max_repeated_calls:
                    reason = (
                        f"repeated call limit exhausted "
                        f"({budget.max_repeated_calls}): {request.tool} with identical args"
                    )
                    await emit("run.failed", {"reason": reason, "seq": step_seq})
                    return RunResult(
                        correlation_id=cid,
                        bundle_id=policy.id,
                        error=reason,
                        steps_taken=step_seq,
                        usage=usage_totals(),
                    )

            if uncertain:
                await emit(
                    "action.uncertain",
                    {
                        "seq": step_seq,
                        "tool": request.tool,
                        "intent_key": orphan.idempotency_key if orphan else "",
                        "reason": "intent journaled without result — action may have executed",
                    },
                )

            # ---- policy decision (pure function)
            decision = evaluate(policy, request, request.context)
            verdict = decision.verdict.value
            await emit(
                "policy.evaluated",
                {
                    "seq": step_seq,
                    "verdict": verdict,
                    "reason": decision.reason,
                    "matched_rules": list(decision.matched_rules),
                },
            )

            # ---- write-ahead intent: the claim. Journaled BEFORE execution;
            # if this append loses a race, DuplicateRecord supersedes the run
            # before any side effect. (Args live in the step's model.output
            # record; the key hashes them, so the intent stays slim.)
            action_key = ""
            if store is not None:
                action_key = idempotency_key(run_id or "", step_seq, request.tool, request.args)
                await journal(
                    "action.intent",
                    {"step": step_seq, "tool": request.tool, "verdict": verdict},
                    idem=action_key,
                )

            # ---- kill-switch: last check before a real side effect. Catches a
            # cancel that arrived while policy was deciding or an approval was
            # pending — so a cancelled run never executes one more action.
            if cancel is not None and cancel.cancelled:
                reason = cancel.reason or "cancelled by caller"
                await emit(
                    "run.cancelled", {"reason": reason, "at": "pre_execute", "seq": step_seq}
                )
                return RunResult(
                    correlation_id=cid,
                    bundle_id=policy.id,
                    error=f"cancelled: {reason}",
                    steps_taken=step_seq,
                    usage=usage_totals(),
                )

            # ---- mediate the action
            action_kind = "action.dry_run" if verdict == "dry_run" else "action.started"
            await emit(action_kind, {"seq": step_seq, "verdict": verdict})

            if verdict == "approve_required":
                await emit(
                    "approval.requested",
                    {"seq": step_seq, "approvers": list(decision.approvers)},
                )

            result = await mediate(request, decision, tools, on_approval, executor)

            # ---- result compression (token optimization seam). Applied to
            # FRESH executions only — a replayed result took this path in a
            # prior attempt and was journaled already-compressed, so it is fed
            # back verbatim above. Compress BEFORE the tag/journal/conversation
            # below so the smaller text is what the model sees, what the
            # journal stores, and what a future replay returns. Fail OPEN: a
            # broken compressor must never drop a tool's real output.
            if compressor is not None and result.ok and isinstance(result.value, str):
                before_chars = len(result.value)
                try:
                    compressed = await compressor(result, request, tool_def)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    compressed = result
                    await emit(
                        "step.compress_failed",
                        {
                            "seq": step_seq,
                            "tool": request.tool,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                if isinstance(compressed, ActionResult) and isinstance(compressed.value, str):
                    after_chars = len(compressed.value)
                    if after_chars < before_chars:
                        result = compressed
                        await emit(
                            "step.compressed",
                            {
                                "seq": step_seq,
                                "tool": request.tool,
                                "before_chars": before_chars,
                                "after_chars": after_chars,
                                # A rough ~4-chars-per-token estimate — the
                                # kernel never invents exact token counts it
                                # didn't get from a provider.
                                "est_tokens_saved": (before_chars - after_chars) // 4,
                            },
                        )

            # ---- one ladder decides both the conversation tag and the audit
            # event kind, so they can never drift apart.
            if result.ok:
                if verdict == "dry_run":
                    tag, outcome_kind = "[dry_run]", "action.dry_run_completed"
                else:
                    tag, outcome_kind = "[ok]", "action.completed"
                tool_message = f"{tag} {result.value}"
            else:
                if verdict in ("deny", "approve_required"):
                    # Bucketed as denials so consumers can separate policy
                    # refusals from tool failures.
                    tag, outcome_kind = "[denied]", "action.denied"
                else:
                    tag, outcome_kind = "[error]", "action.failed"
                tool_message = f"{tag} {result.error}"

            if verdict == "approve_required":
                await emit(
                    "approval.granted" if result.ok else "approval.denied",
                    {"seq": step_seq, "ok": result.ok, "error": result.error},
                )

            if result.ok:
                await emit(outcome_kind, {"seq": step_seq, "duration_ms": result.duration_ms})
            else:
                await emit(outcome_kind, {"seq": step_seq, "reason": result.error})

            # ---- journal the result: it closes the uncertainty window.
            # After the audit emits, so an executed action always has its
            # outcome on the audit stream even if this append fails.
            if store is not None:
                result_body: dict[str, Any] = {
                    "step": step_seq,
                    "tool": request.tool,
                    "verdict": verdict,
                    "ok": result.ok,
                    "error": result.error,
                    "duration_ms": result.duration_ms,
                    "message": tool_message,
                }
                if uncertain:
                    # Forensics: this result resolved an uncertain retry; the
                    # original attempt may still have executed.
                    result_body["uncertain_retry"] = True
                await journal("action.result", result_body, idem=action_key)

            conversation = (
                *conversation,
                Message(
                    role="tool",
                    content=tool_message,
                    tool_call_id=assistant_call_id,
                    name=request.tool,
                ),
            )
            step_seq += 1

    except DuplicateRecord as dup:
        await emit("run.superseded", {"detail": str(dup) or "duplicate journal position"})
        return RunResult(
            correlation_id=cid,
            bundle_id=policy.id,
            error=f"superseded: another worker is executing run {run_id!r}",
            steps_taken=step_seq,
            usage=usage_totals(),
        )
    except _StoreFailed as exc:
        if not started_emitted:
            await emit("run.started", {"task": task, "principal_id": principal.id})
        await emit("run.failed", {"reason": str(exc)})
        return RunResult(
            correlation_id=cid,
            bundle_id=policy.id,
            error=str(exc),
            steps_taken=step_seq,
            usage=usage_totals(),
        )
