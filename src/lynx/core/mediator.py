"""Action Mediator (PEP).

Pure async function. Takes a request, decision, the toolset, an approval
handler, and (optionally) an executor. Returns an ``ActionResult``. No
globals. No store. No broker.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from lynx.core.types import (
    ActionRequest,
    ActionResult,
    ApprovalRequest,
    Decision,
    ExecutionContext,
    Obligation,
    ObligationOutcome,
    ToolSet,
    Verdict,
)

if TYPE_CHECKING:
    from lynx.approvals import ApprovalHandler
    from lynx.executors import Executor

__all__ = ["ObligationHandler", "ObligationRegistry", "mediate"]


# The obligation seam: one async handler per obligation id, supplied by the
# developer (the kernel ships none — mechanism, not policy). A handler fulfills
# its obligation or raises to signal it could not.
ObligationHandler = Callable[[Obligation, ActionRequest, ExecutionContext], Awaitable[None]]
ObligationRegistry = Mapping[str, ObligationHandler]


# Default cap for an approve_required step when the rule doesn't specify one.
# Picks an aggressive value because hanging-forever is the worst outcome.
_DEFAULT_APPROVAL_TIMEOUT_SECONDS = 1800


async def mediate(
    request: ActionRequest,
    decision: Decision,
    tools: ToolSet,
    on_approval: ApprovalHandler,
    executor: Executor | None = None,
    obligations: ObligationRegistry | None = None,
) -> ActionResult:
    """Dispatch one action under the verdict's rules.

    Behavior by verdict:
      * ALLOW             → execute the real tool with request.args
      * DENY              → return a failed ActionResult with the deny reason
      * DRY_RUN           → call the shadow function; return preview
      * APPROVE_REQUIRED  → call on_approval(...) with the rule's timeout;
                            on grant → execute as ALLOW; on deny / timeout /
                            handler exception → return a failed ActionResult.
      * TRANSFORM         → execute the real tool with decision.transform_args
                            (which must be a Mapping)

    Real execution (allow / transform / approval-granted) goes through the
    ``executor`` — the seam where the user's isolation attaches. ``None``
    means in-process (identical to pre-seam behavior). The executor receives
    the *effective* args: for TRANSFORM, the request is rebuilt with the
    transformed args before it reaches the executor. Dry-runs always call
    the shadow in-process — shadows are side-effect-free by contract.

    **Obligations** (``decision.obligations``) are mandatory side-actions
    resolved against the ``obligations`` registry. ``pre`` obligations run
    before the action and *gate* it — a failed pre-obligation DENIES the
    action (fail-closed; the tool never runs). ``post`` obligations run after
    and are best-effort: a failure is recorded on ``ActionResult.obligations``
    but cannot un-execute the side effect. A decision that never executes
    (deny / refused / timed-out approval) runs all its obligations best-effort.
    An unknown obligation id, or any obligation when no registry is configured,
    fails closed. When a decision carries no obligations this path is a no-op —
    behavior is byte-identical to before.

    On a tool raising an exception, the result has ok=False with a structured
    error string. The kernel never crashes due to a misbehaving tool, a
    misbehaving approval handler, a misbehaving executor, a misbehaving
    obligation handler, or a malformed transform.
    """
    pre = tuple(o for o in decision.obligations if o.phase == "pre")
    post = tuple(o for o in decision.obligations if o.phase == "post")

    if decision.verdict == Verdict.DENY:
        result = ActionResult(
            ok=False, error=f"denied: {decision.reason or 'Policy denied this action'}"
        )
        return await _run_unexecuted_obligations(result, request, obligations, decision.obligations)

    if decision.verdict == Verdict.APPROVE_REQUIRED:
        req = ApprovalRequest(
            request=request,
            decision=decision,
            correlation_id=request.context.correlation_id,
        )
        timeout = decision.timeout_seconds or _DEFAULT_APPROVAL_TIMEOUT_SECONDS
        try:
            approval = await asyncio.wait_for(on_approval(req), timeout=timeout)
        except TimeoutError:
            result = ActionResult(
                ok=False,
                error=f"denied: approval handler timed out after {timeout}s",
            )
            return await _run_unexecuted_obligations(
                result, request, obligations, decision.obligations
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = ActionResult(
                ok=False,
                error=f"denied: approval handler raised {type(exc).__name__}: {exc}",
            )
            return await _run_unexecuted_obligations(
                result, request, obligations, decision.obligations
            )
        if not approval.granted:
            result = ActionResult(
                ok=False,
                error=(
                    f"denied: approval refused by {approval.approver}"
                    + (f" — {approval.reason}" if approval.reason else "")
                ),
            )
            return await _run_unexecuted_obligations(
                result, request, obligations, decision.obligations
            )
        # Granted — fall through and execute as if ALLOW
        return await _execute_with_obligations(
            lambda: _execute_real(request, tools, executor), request, obligations, pre, post
        )

    if decision.verdict == Verdict.DRY_RUN:
        return await _execute_with_obligations(
            lambda: _execute_shadow(request, tools), request, obligations, pre, post
        )

    if decision.verdict == Verdict.TRANSFORM:
        if not isinstance(decision.transform_args, Mapping):
            # Compile-time validation should make this unreachable for YAML
            # rules; Python rules can still produce this footgun.
            return ActionResult(
                ok=False,
                error=(
                    "transform decision missing transform_args (must be a Mapping). "
                    "Refusing to fall through to original args."
                ),
            )
        # The executor sees the EFFECTIVE args — what will actually run.
        effective = dataclasses.replace(request, args=dict(decision.transform_args))
        return await _execute_with_obligations(
            lambda: _execute_real(effective, tools, executor), request, obligations, pre, post
        )

    # ALLOW
    return await _execute_with_obligations(
        lambda: _execute_real(request, tools, executor), request, obligations, pre, post
    )


# ---------------------------------------------------------------------------
# Obligation enforcement (the PEP side of XACML obligations)
# ---------------------------------------------------------------------------


async def _execute_with_obligations(
    execute: Callable[[], Awaitable[ActionResult]],
    request: ActionRequest,
    registry: ObligationRegistry | None,
    pre: tuple[Obligation, ...],
    post: tuple[Obligation, ...],
) -> ActionResult:
    """Run pre-obligations (fail-closed gate), the action, then post-obligations
    (best-effort). With no obligations this is exactly ``await execute()`` — no
    overhead and no behavior change for the existing paths."""
    if not pre and not post:
        return await execute()

    outcomes: list[ObligationOutcome] = []
    for ob in pre:
        ok, err = await _run_obligation(ob, request, registry)
        outcomes.append(ObligationOutcome(id=ob.id, phase="pre", fulfilled=ok, error=err))
        if not ok:
            # Fail closed: the action does NOT execute.
            return ActionResult(
                ok=False,
                error=f"denied: pre-obligation {ob.id!r} unfulfilled" + (f": {err}" if err else ""),
                obligations=tuple(outcomes),
            )

    result = await execute()

    for ob in post:
        ok, err = await _run_obligation(ob, request, registry)
        outcomes.append(ObligationOutcome(id=ob.id, phase="post", fulfilled=ok, error=err))

    return dataclasses.replace(result, obligations=(*result.obligations, *outcomes))


async def _run_unexecuted_obligations(
    result: ActionResult,
    request: ActionRequest,
    registry: ObligationRegistry | None,
    obls: tuple[Obligation, ...],
) -> ActionResult:
    """For decisions that never execute (deny / refused / timed-out approval):
    there is no action to gate, so every obligation runs best-effort and is
    recorded without changing the already-failed result's ``ok``."""
    if not obls:
        return result
    outcomes = []
    for ob in obls:
        ok, err = await _run_obligation(ob, request, registry)
        outcomes.append(ObligationOutcome(id=ob.id, phase=ob.phase, fulfilled=ok, error=err))
    return dataclasses.replace(result, obligations=(*result.obligations, *outcomes))


async def _run_obligation(
    ob: Obligation,
    request: ActionRequest,
    registry: ObligationRegistry | None,
) -> tuple[bool, str | None]:
    """Attempt one obligation. Returns ``(fulfilled, error)``. An unknown id or
    a missing registry fails closed; a handler that raises fails closed with a
    structured error — a misbehaving handler never crashes the kernel."""
    if registry is None or ob.id not in registry:
        return False, "no handler registered for obligation"
    handler = registry[ob.id]
    try:
        await handler(ob, request, request.context)
        return True, None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _execute_real(
    request: ActionRequest,
    tools: ToolSet,
    executor: Executor | None,
) -> ActionResult:
    tool = tools.get(request.tool)
    if executor is None:
        # Avoid a module-level import cycle: executors.py imports core.types.
        from lynx.executors import inline_executor

        executor = inline_executor()
    try:
        result = await executor(request, tool)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # A misbehaving executor must not crash the run.
        return ActionResult(
            ok=False,
            error=f"executor raised {type(exc).__name__}: {exc}",
        )
    if not isinstance(result, ActionResult):
        return ActionResult(
            ok=False,
            error=(
                f"executor returned {type(result).__name__}, not ActionResult "
                f"(tool {request.tool!r})"
            ),
        )
    return result


async def _execute_shadow(request: ActionRequest, tools: ToolSet) -> ActionResult:
    tool = tools.get(request.tool)
    if tool.shadow_fn is None:
        return ActionResult(
            ok=False,
            error=f"tool {request.tool!r} has no shadow; cannot dry-run",
        )
    started = time.perf_counter()
    try:
        value = await tool.shadow_fn(**dict(request.args))
        return ActionResult(
            ok=True,
            value={"dry_run": True, "preview": value},
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return ActionResult(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
