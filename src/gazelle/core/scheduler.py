"""Scheduler — the agent step loop.

This is the only component that talks to both the adapter and the kernel.
For each step it:
    1. Calls agent.step(conversation)
    2. If ToolCall: build ActionRequest, evaluate, mediate, journal, audit.
    3. If FinalAnswer: emit audit terminal event, return.

Crash safety: a checkpoint is written BEFORE result.ok=True is reported,
so a crash mid-execution does not double-execute a successful action on resume.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import msgpack

from gazelle.core.mediator import (
    ApprovalPending,
    ToolDenied,
    get_broker,
    get_registry,
    mediate,
)
from gazelle.core.policy import PolicyBundle, evaluate
from gazelle.core.types import (
    ActionRequest,
    ActionResult,
    AuditEvent,
    Budget,
    Decision,
    ExecutionContext,
    Principal,
    Run,
    RunStatus,
    Step,
    Task,
    Verdict,
    new_id,
    now_utc,
)
from gazelle.sdk import Agent, FinalAnswer, Message, ToolCall
from gazelle.stores.sqlite import SQLiteStore


@dataclass
class RunResult:
    run_id: str
    task_id: str
    status: RunStatus
    final_answer: str | None
    steps: int
    error: str | None = None
    paused_approval_id: str | None = None


class BudgetExhausted(Exception):
    pass


class Scheduler:
    """Drives the agent loop with policy enforcement, checkpointing, audit."""

    def __init__(self, store: SQLiteStore, bundle: PolicyBundle) -> None:
        self.store = store
        self.bundle = bundle

    # -----------------------------------------------------------------------
    # Entry points
    # -----------------------------------------------------------------------

    async def start(
        self,
        agent: Agent,
        goal: str,
        principal: Principal,
        environment: str = "dev",
        workspace: str = ".",
        budget: Budget | None = None,
    ) -> RunResult:
        task = Task.create(
            goal=goal,
            created_by=principal,
            policy_bundle_id=self.bundle.id,
            budget=budget,
            metadata={"environment": environment, "workspace": workspace},
        )
        run = Run.create(task_id=task.id)
        run.status = RunStatus.RUNNING
        self.store.save_task(task)
        self.store.save_run(run)
        self._audit(run.id, "run.started", {"task_id": task.id, "goal": goal})

        conversation: list[Message] = [Message(role="user", content=goal)]
        return await self._loop(
            agent=agent,
            task=task,
            run=run,
            conversation=conversation,
            environment=environment,
            workspace=workspace,
            principal=principal,
            start_seq=0,
        )

    async def resume(self, agent: Agent, run_id: str, approver: str | None = None) -> RunResult:
        """Resume a paused run after an approval has been recorded.

        Reads approval state from the store (not the in-memory broker), so
        resume works correctly across process restarts.
        """
        from gazelle.core.mediator import mediate
        from gazelle.core.policy import allow as policy_allow

        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"Run {run_id} not found")
        if run.status != RunStatus.PAUSED:
            raise ValueError(f"Run {run_id} is {run.status}, not paused")
        task = self.store.get_task(run.task_id)
        if task is None:
            raise ValueError(f"Task {run.task_id} not found")

        env = task.metadata.get("environment", "dev")
        workspace = task.metadata.get("workspace", ".")

        # The paused step is at run.last_step_seq; reload it + its approval.
        paused_seq = run.last_step_seq
        paused_step = self.store.get_step(run_id, paused_seq)
        approval = self.store.get_approval(run.resume_token) if run.resume_token else None

        # Reconstruct the conversation as of the paused step's checkpoint.
        conversation = self._reconstruct_conversation(run_id, task.goal)

        run.status = RunStatus.RUNNING
        run.resume_token = None
        self.store.save_run(run)
        self._audit(
            run.id,
            "run.resumed",
            {"by": approver or "system", "approval_id": approval["id"] if approval else None},
        )

        next_seq = paused_seq + 1

        if paused_step and paused_step.action and approval:
            if approval["status"] == "granted":
                # Execute the approved action directly with an ALLOW verdict.
                decision = policy_allow(
                    reason=f"approved by {approval['granted_by'] or '?'}",
                    matched_rules=("<approval>",),
                )
                try:
                    result = await mediate(paused_step.action, decision)
                    self._audit(
                        run.id,
                        "action.completed",
                        {"seq": paused_seq, "ok": result.ok, "via": "approval"},
                    )
                except Exception as exc:
                    from gazelle.core.types import ActionResult

                    result = ActionResult(ok=False, error=f"{type(exc).__name__}: {exc}")
                    self._audit(
                        run.id,
                        "action.failed",
                        {"seq": paused_seq, "reason": result.error},
                    )
                paused_step.decision = decision
                paused_step.result = result
                paused_step.ended_at = now_utc()
                self.store.save_step(paused_step)
                conversation.append(
                    Message(
                        role="tool",
                        content=_format_tool_result(result),
                        tool_call_id=str(paused_seq),
                        name=paused_step.action.tool,
                    )
                )
            elif approval["status"] == "denied":
                self._audit(
                    run.id,
                    "action.denied",
                    {"seq": paused_seq, "reason": "approval denied by human"},
                )
                conversation.append(
                    Message(
                        role="tool",
                        content=f"[denied by approver {approval['granted_by'] or '?'}]",
                        tool_call_id=str(paused_seq),
                        name=paused_step.action.tool,
                    )
                )

        return await self._loop(
            agent=agent,
            task=task,
            run=run,
            conversation=conversation,
            environment=env,
            workspace=workspace,
            principal=task.created_by,
            start_seq=next_seq,
        )

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    async def _loop(
        self,
        agent: Agent,
        task: Task,
        run: Run,
        conversation: list[Message],
        environment: str,
        workspace: str,
        principal: Principal,
        start_seq: int,
    ) -> RunResult:
        seq = start_seq
        started_wall = time.time()

        try:
            while True:
                self._check_budget(task.budget, seq, started_wall)
                step_started = now_utc()

                action_taken = await agent.step(conversation)

                if isinstance(action_taken, FinalAnswer):
                    self._audit(run.id, "run.succeeded", {"final_answer": action_taken.text})
                    run.status = RunStatus.SUCCEEDED
                    run.ended_at = now_utc()
                    self.store.save_run(run)
                    return RunResult(
                        run_id=run.id,
                        task_id=task.id,
                        status=run.status,
                        final_answer=action_taken.text,
                        steps=seq,
                    )

                assert isinstance(action_taken, ToolCall)

                # Build ActionRequest using the tool's declared metadata
                request = self._build_request(
                    action_taken, run.id, seq, principal, environment, workspace
                )
                self._audit(
                    run.id,
                    "step.proposed",
                    {"seq": seq, "tool": request.tool, "args": request.args},
                )

                decision = evaluate(self.bundle, request, request.context)
                self._audit(
                    run.id,
                    "policy.evaluated",
                    {
                        "seq": seq,
                        "verdict": decision.verdict.value,
                        "reason": decision.reason,
                        "matched_rules": list(decision.matched_rules),
                    },
                )

                checkpoint_blob = self._snapshot_conversation(conversation)
                # Pre-execution checkpoint (durability)
                self._save_partial_step(
                    run_id=run.id,
                    seq=seq,
                    request=request,
                    decision=decision,
                    started=step_started,
                    checkpoint_blob=checkpoint_blob,
                )

                try:
                    self._audit(
                        run.id,
                        "action.started"
                        if decision.verdict != Verdict.DRY_RUN
                        else "action.dry_run",
                        {"seq": seq, "verdict": decision.verdict.value},
                    )
                    result = await mediate(request, decision)
                    self._audit(
                        run.id,
                        "action.completed",
                        {"seq": seq, "ok": result.ok, "duration_ms": result.duration_ms},
                    )
                    conversation.append(
                        Message(
                            role="tool",
                            content=_format_tool_result(result),
                            tool_call_id=action_taken.call_id or str(seq),
                            name=request.tool,
                        )
                    )
                except ToolDenied as td:
                    self._audit(
                        run.id,
                        "action.denied",
                        {"seq": seq, "reason": td.reason},
                    )
                    conversation.append(
                        Message(
                            role="tool",
                            content=f"[denied by policy] {td.reason}",
                            tool_call_id=action_taken.call_id or str(seq),
                            name=request.tool,
                        )
                    )
                    result = ActionResult(ok=False, error=f"denied: {td.reason}")
                except ApprovalPending as ap:
                    self._audit(
                        run.id,
                        "approval.requested",
                        {
                            "seq": seq,
                            "approval_id": ap.approval_id,
                            "approvers": list(decision.approvers),
                        },
                    )
                    self.store.save_approval(get_broker().get(ap.approval_id))
                    run.status = RunStatus.PAUSED
                    run.resume_token = ap.approval_id
                    run.last_step_seq = seq
                    self.store.save_run(run)
                    self._audit(run.id, "run.paused", {"approval_id": ap.approval_id})
                    return RunResult(
                        run_id=run.id,
                        task_id=task.id,
                        status=run.status,
                        final_answer=None,
                        steps=seq,
                        paused_approval_id=ap.approval_id,
                    )

                self._finalize_step(
                    run_id=run.id,
                    seq=seq,
                    request=request,
                    decision=decision,
                    result=result,
                    started=step_started,
                )
                run.last_step_seq = seq
                self.store.save_run(run)
                seq += 1

        except BudgetExhausted as be:
            run.status = RunStatus.FAILED
            run.error = str(be)
            run.ended_at = now_utc()
            self.store.save_run(run)
            self._audit(run.id, "run.failed", {"reason": str(be)})
            return RunResult(
                run_id=run.id,
                task_id=task.id,
                status=run.status,
                final_answer=None,
                steps=seq,
                error=str(be),
            )
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
            run.ended_at = now_utc()
            self.store.save_run(run)
            self._audit(run.id, "run.failed", {"reason": run.error})
            return RunResult(
                run_id=run.id,
                task_id=task.id,
                status=run.status,
                final_answer=None,
                steps=seq,
                error=run.error,
            )

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _build_request(
        self,
        call: ToolCall,
        run_id: str,
        seq: int,
        principal: Principal,
        environment: str,
        workspace: str,
    ) -> ActionRequest:
        registry = get_registry()
        tool = registry.get(call.tool)
        metadata = tool.metadata_factory(call.args)
        context = ExecutionContext(
            principal=principal,
            environment=environment,
            workspace=workspace,
            run_id=run_id,
            step_seq=seq,
            timestamp=now_utc(),
        )
        return ActionRequest.build(
            tool=call.tool, args=call.args, declared=metadata, context=context
        )

    def _snapshot_conversation(self, conversation: list[Message]) -> bytes:
        return msgpack.packb([asdict(m) for m in conversation], use_bin_type=True)

    def _reconstruct_conversation(self, run_id: str, goal: str) -> list[Message]:
        steps = self.store.get_steps(run_id)
        if not steps:
            return [Message(role="user", content=goal)]
        # Latest step's checkpoint is the conversation up through that step.
        latest = steps[-1]
        raw = msgpack.unpackb(latest.checkpoint_blob, raw=False)
        return [Message(**m) for m in raw]

    def _save_partial_step(
        self,
        run_id: str,
        seq: int,
        request: ActionRequest,
        decision: Decision,
        started: datetime,
        checkpoint_blob: bytes,
    ) -> None:
        step = Step(
            id=new_id("S"),
            run_id=run_id,
            seq=seq,
            started_at=started,
            ended_at=started,
            checkpoint_blob=checkpoint_blob,
            action=request,
            decision=decision,
            result=None,
        )
        self.store.save_step(step)

    def _finalize_step(
        self,
        run_id: str,
        seq: int,
        request: ActionRequest,
        decision: Decision,
        result: ActionResult,
        started: datetime,
    ) -> None:
        existing = self.store.get_step(run_id, seq)
        if existing is None:
            return
        existing.result = result
        existing.ended_at = now_utc()
        self.store.save_step(existing)

    def _audit(self, run_id: str, kind: str, body: dict[str, Any]) -> None:
        prev = self.store.latest_audit_hash(run_id)
        seq = sum(1 for _ in self.store.audit_chain(run_id))
        event = AuditEvent.build(prev=prev, run_id=run_id, seq=seq, kind=kind, body=body)
        self.store.append_audit(event)

    def _check_budget(self, budget: Budget, seq: int, started_wall: float) -> None:
        if budget.steps is not None and seq >= budget.steps:
            raise BudgetExhausted(f"step budget exhausted ({budget.steps})")
        if (
            budget.duration_seconds is not None
            and time.time() - started_wall >= budget.duration_seconds
        ):
            raise BudgetExhausted(f"duration budget exhausted ({budget.duration_seconds}s)")


def _format_tool_result(result: ActionResult) -> str:
    if result.ok:
        return f"[tool ok] {result.value}"
    return f"[tool error] {result.error}"
