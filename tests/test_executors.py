"""Executor seam — pluggable execution for approved actions.

The seam's contract: every real execution (allow / transform /
approval-granted) goes through the executor; dry-runs stay in-process;
the default is bit-for-bit the pre-seam inline behavior; a misbehaving
executor fails the action, never the run.
"""

from __future__ import annotations

from typing import Any

from lynx import (
    ActionRequest,
    ActionResult,
    FinalAnswer,
    Message,
    ToolCall,
    ToolDef,
    ToolSet,
    auto_approve,
    compile_policy,
    inline_executor,
    route_executor,
    run_agent,
    tool,
)

ALLOW_ALL = "version: 1\ndefaults: { on_no_match: allow }\nrules: []"

CALLS: list[str] = []


@tool(reversible=True, scope=("compute:exec",))
async def plain(msg: str) -> str:
    """No isolation hint — default route."""
    CALLS.append(f"plain:{msg}")
    return f"plain ran: {msg}"


@tool(reversible=True, scope=("compute:exec",), isolation="boxed")
async def boxed(msg: str) -> str:
    """Declares isolation='boxed' — routed."""
    CALLS.append(f"boxed:{msg}")
    return f"boxed ran: {msg}"


@boxed.shadow
async def _boxed_shadow(msg: str) -> dict[str, Any]:
    return {"would_run": msg}


class Scripted:
    def __init__(self, *actions: Any) -> None:
        self._actions = list(actions)

    async def step(self, conversation: tuple[Message, ...]):
        return self._actions.pop(0)


def recording_executor(label: str, log: list[tuple[str, str, dict]]):
    async def execute(request: ActionRequest, tool_def: ToolDef) -> ActionResult:
        log.append((label, request.tool, dict(request.args)))
        value = await tool_def.fn(**dict(request.args))
        return ActionResult(ok=True, value=f"[{label}] {value}")

    return execute


def _reset():
    CALLS.clear()


# --- the seam --------------------------------------------------------------


async def test_default_executor_unchanged_behavior() -> None:
    _reset()
    result = await run_agent(
        Scripted(ToolCall(tool="plain", args={"msg": "a"}, call_id="c"), FinalAnswer(text="d")),
        task="t",
        tools=ToolSet.from_functions(plain),
        policy=compile_policy(ALLOW_ALL),
    )
    assert result.final_answer == "d"
    assert CALLS == ["plain:a"]


async def test_custom_executor_receives_allowed_calls() -> None:
    _reset()
    log: list[tuple[str, str, dict]] = []
    result = await run_agent(
        Scripted(ToolCall(tool="plain", args={"msg": "a"}, call_id="c"), FinalAnswer(text="d")),
        task="t",
        tools=ToolSet.from_functions(plain),
        policy=compile_policy(ALLOW_ALL),
        executor=recording_executor("X", log),
    )
    assert result.final_answer == "d"
    assert log == [("X", "plain", {"msg": "a"})]


async def test_executor_receives_transformed_args() -> None:
    """TRANSFORM: the executor must see the EFFECTIVE args, not the proposal."""
    _reset()
    policy = compile_policy(
        """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: rewrite
    match: { tool: plain }
    decision: transform
    transform:
      jsonpath: "$.args.msg"
      set: "rewritten"
        """
    )
    log: list[tuple[str, str, dict]] = []
    await run_agent(
        Scripted(
            ToolCall(tool="plain", args={"msg": "original"}, call_id="c"), FinalAnswer(text="d")
        ),
        task="t",
        tools=ToolSet.from_functions(plain),
        policy=policy,
        executor=recording_executor("X", log),
    )
    assert log == [("X", "plain", {"msg": "rewritten"})]
    assert CALLS == ["plain:rewritten"]


async def test_executor_used_after_approval_but_not_for_dry_run() -> None:
    _reset()
    policy = compile_policy(
        """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: preview-boxed
    match: { tool: boxed, args.msg.matches: "^preview" }
    decision: dry_run
  - id: approve-boxed
    match: { tool: boxed }
    decision: approve_required
        """
    )
    log: list[tuple[str, str, dict]] = []
    result = await run_agent(
        Scripted(
            ToolCall(tool="boxed", args={"msg": "preview this"}, call_id="c1"),
            ToolCall(tool="boxed", args={"msg": "do it"}, call_id="c2"),
            FinalAnswer(text="d"),
        ),
        task="t",
        tools=ToolSet.from_functions(boxed),
        policy=policy,
        on_approval=auto_approve(),
        executor=recording_executor("X", log),
    )
    assert result.final_answer == "d"
    # dry_run went to the shadow in-process (not the executor, no side effect);
    # the approved call went through the executor.
    assert log == [("X", "boxed", {"msg": "do it"})]
    assert CALLS == ["boxed:do it"]


async def test_raising_executor_fails_action_not_run() -> None:
    _reset()

    async def broken(request: ActionRequest, tool_def: ToolDef) -> ActionResult:
        raise RuntimeError("executor exploded")

    result = await run_agent(
        Scripted(
            ToolCall(tool="plain", args={"msg": "a"}, call_id="c"), FinalAnswer(text="adapted")
        ),
        task="t",
        tools=ToolSet.from_functions(plain),
        policy=compile_policy(ALLOW_ALL),
        executor=broken,
    )
    assert result.final_answer == "adapted"  # run survived; agent saw [error]
    assert CALLS == []  # tool never executed


async def test_non_actionresult_return_fails_closed() -> None:
    async def wrong(request: ActionRequest, tool_def: ToolDef):
        return "not an ActionResult"

    result = await run_agent(
        Scripted(
            ToolCall(tool="plain", args={"msg": "a"}, call_id="c"), FinalAnswer(text="adapted")
        ),
        task="t",
        tools=ToolSet.from_functions(plain),
        policy=compile_policy(ALLOW_ALL),
        executor=wrong,
    )
    assert result.final_answer == "adapted"


# --- routing ----------------------------------------------------------------


async def test_route_executor_routes_by_isolation_hint() -> None:
    _reset()
    log: list[tuple[str, str, dict]] = []
    router = route_executor(
        {
            None: recording_executor("default", log),
            "boxed": recording_executor("boxed-route", log),
        }
    )
    await run_agent(
        Scripted(
            ToolCall(tool="plain", args={"msg": "a"}, call_id="c1"),
            ToolCall(tool="boxed", args={"msg": "b"}, call_id="c2"),
            FinalAnswer(text="d"),
        ),
        task="t",
        tools=ToolSet.from_functions(plain, boxed),
        policy=compile_policy(ALLOW_ALL),
        executor=router,
    )
    assert [(label, tool_name) for label, tool_name, _ in log] == [
        ("default", "plain"),
        ("boxed-route", "boxed"),
    ]


async def test_route_executor_fails_closed_on_unrouted_isolation() -> None:
    """A tool asking for isolation no route provides must NOT silently run
    on the default route."""
    _reset()
    router = route_executor({None: inline_executor()})  # no "boxed" route
    result = await run_agent(
        Scripted(
            ToolCall(tool="boxed", args={"msg": "b"}, call_id="c"), FinalAnswer(text="adapted")
        ),
        task="t",
        tools=ToolSet.from_functions(boxed),
        policy=compile_policy(ALLOW_ALL),
        executor=router,
    )
    assert result.final_answer == "adapted"
    assert CALLS == []  # the boxed tool never ran anywhere


async def test_isolation_hint_survives_shadow_decoration() -> None:
    # boxed has both isolation="boxed" AND a shadow — the shadow decorator
    # must not drop the isolation field when it rebuilds metadata.
    meta = boxed.__lynx_meta__.metadata
    assert meta.isolation == "boxed"
    assert meta.has_shadow is True


# --- timeouts ----------------------------------------------------------------


@tool(reversible=True, scope=("compute:exec",))
async def sleepy(seconds: float) -> str:
    """Cooperatively sleeps — cancellable by inline_executor's timeout."""
    import asyncio as _asyncio

    await _asyncio.sleep(seconds)
    return "woke up"


async def test_agent_step_timeout_fails_run_cleanly() -> None:
    from lynx import Budget

    class HangingAgent:
        async def step(self, conversation):
            import asyncio as _asyncio

            await _asyncio.sleep(30)

    result = await run_agent(
        HangingAgent(),
        task="t",
        tools=ToolSet.from_functions(plain),
        policy=compile_policy(ALLOW_ALL),
        budget=Budget(steps=5, step_timeout_seconds=0.05),
    )
    assert result.error == "agent.step timed out after 0.05s"
    assert result.final_answer is None


async def test_inline_executor_timeout_fails_action_not_run() -> None:
    result = await run_agent(
        Scripted(
            ToolCall(tool="sleepy", args={"seconds": 30}, call_id="c"),
            FinalAnswer(text="adapted"),
        ),
        task="t",
        tools=ToolSet.from_functions(sleepy),
        policy=compile_policy(ALLOW_ALL),
        executor=inline_executor(timeout_seconds=0.05),
    )
    # The tool timed out, but the RUN survived — the agent saw the error.
    assert result.final_answer == "adapted"


async def test_inline_executor_no_timeout_unchanged() -> None:
    result = await run_agent(
        Scripted(
            ToolCall(tool="sleepy", args={"seconds": 0.01}, call_id="c"),
            FinalAnswer(text="d"),
        ),
        task="t",
        tools=ToolSet.from_functions(sleepy),
        policy=compile_policy(ALLOW_ALL),
        executor=inline_executor(timeout_seconds=5),
    )
    assert result.final_answer == "d"
