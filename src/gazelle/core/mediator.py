"""Action Mediator — the Policy Enforcement Point (PEP).

Given an ActionRequest and a Decision from the PDP, the mediator dispatches:
    ALLOW            → call real tool
    DENY             → return ToolDenied (model continues with a denial message)
    DRY_RUN          → call tool.shadow()
    APPROVE_REQUIRED → suspend run, persist ApprovalRequest, raise PausedRun
    TRANSFORM        → call real tool with transformed args
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from gazelle.core.types import (
    ActionRequest,
    ActionResult,
    Decision,
    Verdict,
)

# ---------------------------------------------------------------------------
# Exceptions used as control flow between mediator and scheduler
# ---------------------------------------------------------------------------


class ToolDenied(Exception):
    """Raised inside the mediator when the PDP denied an action.

    Carries the reason so the scheduler can feed it back to the model as
    a structured tool result the agent can react to.
    """

    def __init__(self, reason: str, decision: Decision) -> None:
        super().__init__(reason)
        self.reason = reason
        self.decision = decision


class ApprovalPending(Exception):
    """Raised to bubble a paused-for-approval state up to the scheduler."""

    def __init__(self, approval_id: str, decision: Decision) -> None:
        super().__init__("Approval required")
        self.approval_id = approval_id
        self.decision = decision


# ---------------------------------------------------------------------------
# Tool registry (populated by @tool decorator)
# ---------------------------------------------------------------------------


@dataclass
class RegisteredTool:
    name: str
    description: str
    fn: Callable[..., Coroutine[Any, Any, Any]]
    shadow_fn: Callable[..., Coroutine[Any, Any, Any]] | None
    metadata_factory: Callable[[dict[str, Any]], ToolMetadataLike]


class ToolMetadataLike:  # forward-decl shim avoiding circular import in type hints
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> RegisteredTool:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def all(self) -> dict[str, RegisteredTool]:
        return dict(self._tools)

    def clear(self) -> None:
        self._tools.clear()


# Module-level singleton; tests can clear it.
_REGISTRY = ToolRegistry()


def get_registry() -> ToolRegistry:
    return _REGISTRY


# ---------------------------------------------------------------------------
# Approval broker (in-process MVP; transports wrap this)
# ---------------------------------------------------------------------------


@dataclass
class ApprovalRequest:
    id: str
    run_id: str
    step_seq: int
    action: ActionRequest
    decision: Decision
    status: str = "pending"  # pending | granted | denied | timeout
    granted_by: str | None = None


class ApprovalBroker:
    """Tracks pending approvals. The scheduler queries this on resume."""

    def __init__(self) -> None:
        self._pending: dict[str, ApprovalRequest] = {}
        self._resolved: dict[str, ApprovalRequest] = {}

    def open(
        self, run_id: str, step_seq: int, action: ActionRequest, decision: Decision
    ) -> ApprovalRequest:
        from gazelle.core.types import new_id

        req = ApprovalRequest(
            id=new_id("A"),
            run_id=run_id,
            step_seq=step_seq,
            action=action,
            decision=decision,
        )
        self._pending[req.id] = req
        return req

    def grant(self, approval_id: str, approver: str) -> ApprovalRequest:
        req = self._pending.pop(approval_id)
        req.status = "granted"
        req.granted_by = approver
        self._resolved[approval_id] = req
        return req

    def deny(self, approval_id: str, approver: str) -> ApprovalRequest:
        req = self._pending.pop(approval_id)
        req.status = "denied"
        req.granted_by = approver
        self._resolved[approval_id] = req
        return req

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._pending.get(approval_id) or self._resolved.get(approval_id)

    def pending(self) -> list[ApprovalRequest]:
        return list(self._pending.values())


_BROKER = ApprovalBroker()


def get_broker() -> ApprovalBroker:
    return _BROKER


# ---------------------------------------------------------------------------
# The mediator
# ---------------------------------------------------------------------------


async def mediate(request: ActionRequest, decision: Decision) -> ActionResult:
    """Run the action under the verdict's rules. Returns an ActionResult.

    Raises ToolDenied for DENY, ApprovalPending for APPROVE_REQUIRED.
    """
    if decision.verdict == Verdict.DENY:
        raise ToolDenied(decision.reason or "Policy denied this action", decision)

    if decision.verdict == Verdict.APPROVE_REQUIRED:
        approval = _BROKER.open(
            run_id=request.context.run_id,
            step_seq=request.context.step_seq,
            action=request,
            decision=decision,
        )
        raise ApprovalPending(approval.id, decision)

    tool = _REGISTRY.get(request.tool)
    started = time.perf_counter()

    try:
        if decision.verdict == Verdict.DRY_RUN:
            if tool.shadow_fn is None:
                # Defensive: PDP should have caught this via on_missing_shadow,
                # but if a dry_run sneaks through, surface a clean error.
                raise ToolDenied(
                    f"Tool {request.tool!r} has no shadow; cannot dry-run",
                    decision,
                )
            value = await tool.shadow_fn(**request.args)
            return ActionResult(
                ok=True,
                value={"dry_run": True, "preview": value},
                duration_ms=int((time.perf_counter() - started) * 1000),
                side_effects=("dry-run-only; no real side effects",),
            )

        args = (
            decision.transform_args
            if decision.verdict == Verdict.TRANSFORM and decision.transform_args is not None
            else request.args
        )
        value = await tool.fn(**args)
        return ActionResult(
            ok=True,
            value=value,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except ToolDenied:
        raise
    except Exception as exc:
        return ActionResult(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.perf_counter() - started) * 1000),
            side_effects=(traceback.format_exc()[-500:],),
        )
