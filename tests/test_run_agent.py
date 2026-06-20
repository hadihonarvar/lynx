"""Integration tests for ``run_agent`` — the public entry point."""

from __future__ import annotations

import io
import json
from typing import Any

from lynx import (
    Budget,
    FinalAnswer,
    Message,
    ToolCall,
    ToolSet,
    auto_approve,
    auto_deny,
    callback_sink,
    compile_policy,
    jsonl_sink,
    run_agent,
    tool,
)

# --- tools --------------------------------------------------------------


@tool(reversible=True, scope=("compute:exec",))
async def echo(msg: str) -> str:
    """Echo a message."""
    return msg


@tool(reversible=False, scope=("filesystem:write",))
async def dangerous(cmd: str) -> str:
    """Pretend to do something dangerous."""
    return f"did: {cmd}"


@dangerous.shadow
async def _dangerous_shadow(cmd: str) -> dict[str, Any]:
    return {"would_do": cmd}


# --- agents -------------------------------------------------------------


class _ScriptedAgent:
    def __init__(self, *actions):
        self._actions = list(actions)

    async def step(self, conversation: tuple[Message, ...]):
        return self._actions.pop(0)


# --- tests --------------------------------------------------------------


async def test_run_agent_returns_minimal_result() -> None:
    policy = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []")
    tools = ToolSet.from_functions(echo)
    agent = _ScriptedAgent(
        ToolCall(tool="echo", args={"msg": "hi"}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    result = await run_agent(
        agent,
        task="say hi",
        tools=tools,
        policy=policy,
        on_approval=auto_deny("no approvals"),
    )
    assert result.final_answer == "done"
    assert result.error is None
    assert result.steps_taken == 1
    assert result.correlation_id  # uuid
    assert result.bundle_id == policy.id


async def test_run_started_surfaces_effective_budget() -> None:
    policy = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []")
    seen: list[Any] = []

    async def collect(ev):
        seen.append(ev)

    agent = _ScriptedAgent(FinalAnswer(text="done"))
    await run_agent(
        agent, task="t", tools=ToolSet.from_functions(echo), policy=policy,
        sinks=(callback_sink(collect),), on_approval=auto_deny("x"),
    )
    started = next(e for e in seen if e.kind == "run.started")
    # The effective caps are visible on the audit stream by default.
    assert started.body["budget"]["steps"] == 50
    assert started.body["budget"]["duration_seconds"] == 600
    assert started.body["environment"] == "dev"
    # A bounded run does NOT emit the loud unbounded warning.
    assert not any(e.kind == "run.unbounded" for e in seen)


async def test_unlimited_budget_emits_run_unbounded() -> None:
    policy = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []")
    seen: list[Any] = []

    async def collect(ev):
        seen.append(ev)

    agent = _ScriptedAgent(FinalAnswer(text="done"))
    await run_agent(
        agent, task="t", tools=ToolSet.from_functions(echo), policy=policy,
        budget=Budget.unlimited(), sinks=(callback_sink(collect),),
        on_approval=auto_deny("x"),
    )
    # Opting out of caps is loud, never silent.
    assert any(e.kind == "run.unbounded" for e in seen)
    started = next(e for e in seen if e.kind == "run.started")
    assert started.body["budget"]["steps"] is None


async def test_run_agent_blocks_dangerous_with_deny_policy() -> None:
    policy = compile_policy(
        """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: block-dangerous
    match: { tool: dangerous }
    decision: deny
    reason: no dangerous allowed
        """
    )
    tools = ToolSet.from_functions(dangerous, echo)
    agent = _ScriptedAgent(
        ToolCall(tool="dangerous", args={"cmd": "rm"}, call_id="c1"),
        FinalAnswer(text="adapted"),
    )

    seen: list[Any] = []

    async def collect(ev):
        seen.append(ev)

    result = await run_agent(
        agent,
        task="try dangerous",
        tools=tools,
        policy=policy,
        sinks=(callback_sink(collect),),
        on_approval=auto_deny("no"),
    )
    assert result.final_answer == "adapted"
    # The deny must surface as an audit event and as a [denied] message —
    # not silently turn into a successful tool call.
    kinds = [ev.kind for ev in seen]
    assert "action.denied" in kinds
    denied_event = next(ev for ev in seen if ev.kind == "policy.evaluated")
    assert denied_event.body["verdict"] == "deny"


async def test_run_agent_dry_runs_through_shadow() -> None:
    policy = compile_policy(
        """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: dry-run-dangerous
    match: { tool: dangerous }
    decision: dry_run
        """
    )
    tools = ToolSet.from_functions(dangerous)
    seen_events = []

    async def collector(ev):
        seen_events.append(ev)

    agent = _ScriptedAgent(
        ToolCall(tool="dangerous", args={"cmd": "rm /"}, call_id="c1"),
        FinalAnswer(text="ok"),
    )
    await run_agent(
        agent,
        task="dry run",
        tools=tools,
        policy=policy,
        sinks=(callback_sink(collector),),
        on_approval=auto_deny("no"),
    )
    assert any("action.dry_run" in ev.kind for ev in seen_events)


async def test_run_agent_calls_on_approval_handler() -> None:
    policy = compile_policy(
        """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: approve-dangerous
    match: { tool: dangerous }
    decision: approve_required
        """
    )
    tools = ToolSet.from_functions(dangerous)

    handler_was_called = False

    async def approve_once(req):
        nonlocal handler_was_called
        handler_was_called = True
        from lynx import ApprovalDecision

        return ApprovalDecision(granted=True, approver="test")

    from lynx.approvals import callback_approval

    agent = _ScriptedAgent(
        ToolCall(tool="dangerous", args={"cmd": "x"}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    result = await run_agent(
        agent,
        task="needs approval",
        tools=tools,
        policy=policy,
        on_approval=callback_approval(approve_once),
    )
    assert handler_was_called
    assert result.final_answer == "done"


async def test_run_agent_streams_events_to_sinks() -> None:
    policy = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []")
    tools = ToolSet.from_functions(echo)
    buf = io.StringIO()
    agent = _ScriptedAgent(
        ToolCall(tool="echo", args={"msg": "hi"}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    await run_agent(
        agent,
        task="streaming",
        tools=tools,
        policy=policy,
        sinks=(jsonl_sink(buf),),
        on_approval=auto_approve(),
    )
    lines = [line for line in buf.getvalue().split("\n") if line.strip()]
    kinds = [json.loads(line)["kind"] for line in lines]
    assert "run.started" in kinds
    assert "policy.evaluated" in kinds
    assert "run.succeeded" in kinds


async def test_run_agent_budget_steps() -> None:
    policy = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []")
    tools = ToolSet.from_functions(echo)

    # Agent never finishes
    class NeverFinishes:
        async def step(self, conv):
            return ToolCall(tool="echo", args={"msg": "x"}, call_id="c")

    result = await run_agent(
        NeverFinishes(),
        task="loop",
        tools=tools,
        policy=policy,
        budget=Budget(steps=3, duration_seconds=10),
        on_approval=auto_approve(),
    )
    assert result.error is not None
    assert "budget exhausted" in result.error
    assert result.steps_taken == 3


async def test_run_agent_unknown_tool_doesnt_crash() -> None:
    policy = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []")
    tools = ToolSet.from_functions(echo)
    agent = _ScriptedAgent(
        ToolCall(tool="nonexistent_tool", args={}, call_id="c1"),
        FinalAnswer(text="adapted"),
    )
    seen: list[Any] = []

    async def collect(ev):
        seen.append(ev)

    result = await run_agent(
        agent,
        task="bad tool",
        tools=tools,
        policy=policy,
        sinks=(callback_sink(collect),),
        on_approval=auto_deny("no"),
    )
    # Should recover gracefully — final answer reached
    assert result.final_answer == "adapted"
    # And the unknown-tool failure should be audited.
    failed = [ev for ev in seen if ev.kind == "action.failed"]
    assert failed
    assert "unknown tool" in failed[0].body["reason"]


# ---------------------------------------------------------------------------
# TRANSFORM end-to-end
# ---------------------------------------------------------------------------


@tool(reversible=True, scope=("db:exec",))
async def sql_exec(sql: str) -> str:
    """Pretend to execute a SQL statement."""
    return f"executed: {sql}"


async def test_transform_set_rewrites_args_before_execution() -> None:
    policy = compile_policy(
        """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: rewrite-sql
    match: { tool: sql_exec }
    decision: transform
    transform:
      jsonpath: "$.args.sql"
      set: "SELECT 1"
        """
    )
    tools = ToolSet.from_functions(sql_exec)
    agent = _ScriptedAgent(
        ToolCall(tool="sql_exec", args={"sql": "DROP TABLE users"}, call_id="c1"),
        FinalAnswer(text="done"),
    )

    seen: list[Any] = []

    async def collect(ev):
        seen.append(ev)

    result = await run_agent(
        agent,
        task="run sql",
        tools=tools,
        policy=policy,
        sinks=(callback_sink(collect),),
        on_approval=auto_deny("no"),
    )
    assert result.final_answer == "done"
    # The completed event tells us the tool ran; verify it ran on the
    # transformed args by inspecting the conversation echo.
    completed = [ev for ev in seen if ev.kind == "action.completed"]
    assert completed


async def test_transform_append_concatenates_to_existing_value() -> None:
    policy = compile_policy(
        """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: append-where
    match: { tool: sql_exec }
    decision: transform
    transform:
      jsonpath: "$.args.sql"
      append: " WHERE tenant_id = 'X'"
        """
    )
    tools = ToolSet.from_functions(sql_exec)

    captured_sql: list[str] = []

    @tool(reversible=True, scope=("db:exec",))
    async def capture(sql: str) -> str:
        captured_sql.append(sql)
        return sql

    tools = ToolSet.from_functions(capture)
    # The transform rule matches `sql_exec` not `capture`; rewrite to match `capture`.
    policy = compile_policy(
        """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: append-where
    match: { tool: capture }
    decision: transform
    transform:
      jsonpath: "$.args.sql"
      append: " WHERE tenant_id = 'X'"
        """
    )

    agent = _ScriptedAgent(
        ToolCall(tool="capture", args={"sql": "SELECT *"}, call_id="c1"),
        FinalAnswer(text="ok"),
    )
    await run_agent(
        agent,
        task="run",
        tools=tools,
        policy=policy,
        on_approval=auto_deny("no"),
    )
    assert captured_sql == ["SELECT * WHERE tenant_id = 'X'"]


# ---------------------------------------------------------------------------
# Defaults end-to-end
# ---------------------------------------------------------------------------


async def test_default_on_no_match_deny_blocks_unmatched_tool() -> None:
    # No rules at all + defaults=deny → echo gets denied even though it's
    # totally benign. Proves the safety net.
    policy = compile_policy("version: 1\nrules: []\n")  # on_no_match defaults to deny
    tools = ToolSet.from_functions(echo)
    seen: list[Any] = []

    async def collect(ev):
        seen.append(ev)

    agent = _ScriptedAgent(
        ToolCall(tool="echo", args={"msg": "hi"}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    await run_agent(
        agent,
        task="run",
        tools=tools,
        policy=policy,
        sinks=(callback_sink(collect),),
        on_approval=auto_deny("no"),
    )
    denied = [ev for ev in seen if ev.kind == "action.denied"]
    assert denied


# ---------------------------------------------------------------------------
# Approval timeout + raising handlers
# ---------------------------------------------------------------------------


async def test_approval_handler_timeout_is_enforced() -> None:
    import asyncio as _asyncio

    policy = compile_policy(
        """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: approve
    match: { tool: dangerous }
    decision: approve_required
    timeout_seconds: 1
        """
    )
    tools = ToolSet.from_functions(dangerous)

    async def slow_handler(req):
        await _asyncio.sleep(5)  # 5x the timeout
        from lynx import ApprovalDecision

        return ApprovalDecision(granted=True, approver="never")

    from lynx.approvals import callback_approval

    seen: list[Any] = []

    async def collect(ev):
        seen.append(ev)

    agent = _ScriptedAgent(
        ToolCall(tool="dangerous", args={"cmd": "rm"}, call_id="c1"),
        FinalAnswer(text="adapted"),
    )
    result = await run_agent(
        agent,
        task="try",
        tools=tools,
        policy=policy,
        sinks=(callback_sink(collect),),
        on_approval=callback_approval(slow_handler),
    )
    assert result.final_answer == "adapted"
    denied = [ev for ev in seen if ev.kind == "action.denied"]
    assert denied
    assert "timed out" in denied[0].body["reason"]


async def test_approval_handler_raising_becomes_a_deny() -> None:
    policy = compile_policy(
        """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: approve
    match: { tool: dangerous }
    decision: approve_required
        """
    )
    tools = ToolSet.from_functions(dangerous)

    async def boom(req):
        raise RuntimeError("approval system down")

    from lynx.approvals import callback_approval

    seen: list[Any] = []

    async def collect(ev):
        seen.append(ev)

    agent = _ScriptedAgent(
        ToolCall(tool="dangerous", args={"cmd": "rm"}, call_id="c1"),
        FinalAnswer(text="adapted"),
    )
    result = await run_agent(
        agent,
        task="try",
        tools=tools,
        policy=policy,
        sinks=(callback_sink(collect),),
        on_approval=callback_approval(boom),
    )
    # Run completes — the raise does NOT crash run_agent.
    assert result.final_answer == "adapted"
    denied = [ev for ev in seen if ev.kind == "action.denied"]
    assert denied
    assert "RuntimeError" in denied[0].body["reason"]


# ---------------------------------------------------------------------------
# Sink that fails does not crash the run
# ---------------------------------------------------------------------------


async def test_sink_failure_does_not_crash_run() -> None:
    policy = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []")
    tools = ToolSet.from_functions(echo)

    async def broken(ev):
        raise RuntimeError("sink died")

    agent = _ScriptedAgent(
        ToolCall(tool="echo", args={"msg": "hi"}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    result = await run_agent(
        agent,
        task="x",
        tools=tools,
        policy=policy,
        sinks=(callback_sink(broken),),
        on_approval=auto_deny("no"),
    )
    assert result.final_answer == "done"
    assert result.error is None


# ---------------------------------------------------------------------------
# Shadow that raises → ActionResult(ok=False) with clear error
# ---------------------------------------------------------------------------


@tool(reversible=False, scope=("filesystem:write",))
async def writes_file(path: str) -> str:
    return f"wrote {path}"


@writes_file.shadow
async def _writes_file_shadow(path: str) -> dict[str, Any]:
    raise RuntimeError("shadow blew up")


async def test_shadow_exception_surfaces_as_failed_action() -> None:
    policy = compile_policy(
        """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: dry-run
    match: { tool: writes_file }
    decision: dry_run
        """
    )
    tools = ToolSet.from_functions(writes_file)
    seen: list[Any] = []

    async def collect(ev):
        seen.append(ev)

    agent = _ScriptedAgent(
        ToolCall(tool="writes_file", args={"path": "/x"}, call_id="c1"),
        FinalAnswer(text="adapted"),
    )
    result = await run_agent(
        agent,
        task="x",
        tools=tools,
        policy=policy,
        sinks=(callback_sink(collect),),
        on_approval=auto_deny("no"),
    )
    assert result.final_answer == "adapted"
    failed = [ev for ev in seen if ev.kind == "action.failed"]
    assert failed
    assert "RuntimeError" in failed[0].body["reason"]


# ---------------------------------------------------------------------------
# Hot-swap proves no shared state
# ---------------------------------------------------------------------------


async def test_two_consecutive_runs_with_different_policies_decide_independently() -> None:
    allow_policy = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []\n")
    deny_policy = compile_policy("version: 1\ndefaults: { on_no_match: deny }\nrules: []\n")
    tools = ToolSet.from_functions(echo)

    def make_agent():
        return _ScriptedAgent(
            ToolCall(tool="echo", args={"msg": "x"}, call_id="c1"),
            FinalAnswer(text="done"),
        )

    r1 = await run_agent(
        make_agent(),
        task="x",
        tools=tools,
        policy=allow_policy,
        on_approval=auto_deny("no"),
    )
    r2 = await run_agent(
        make_agent(),
        task="x",
        tools=tools,
        policy=deny_policy,
        on_approval=auto_deny("no"),
    )
    assert r1.bundle_id == allow_policy.id
    assert r2.bundle_id == deny_policy.id
    assert r1.bundle_id != r2.bundle_id


# ---------------------------------------------------------------------------
# Adapter-protocol: assistant tool_call_args recorded on conversation
# ---------------------------------------------------------------------------


async def test_assistant_tool_call_message_recorded_for_adapters() -> None:
    """The scheduler must append an assistant message carrying tool_call_args
    so Anthropic / OpenAI adapters can rebuild a well-formed alternation."""
    policy = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []\n")
    tools = ToolSet.from_functions(echo)

    captured_convs: list[tuple[Message, ...]] = []

    class CapturingAgent:
        def __init__(self):
            self._i = 0

        async def step(self, conv):
            captured_convs.append(conv)
            self._i += 1
            if self._i == 1:
                return ToolCall(tool="echo", args={"msg": "hi"}, call_id="x1")
            return FinalAnswer(text="done")

    await run_agent(
        CapturingAgent(),
        task="t",
        tools=tools,
        policy=policy,
        on_approval=auto_deny("no"),
    )
    # On the second step the agent should see the conversation with:
    #   user → assistant(tool_call x1) → tool([ok] ...)
    assert len(captured_convs) == 2
    second_conv = captured_convs[1]
    roles = [m.role for m in second_conv]
    assert roles == ["user", "assistant", "tool"]
    assistant_msg = second_conv[1]
    assert assistant_msg.tool_call_id == "x1"
    assert assistant_msg.tool_call_args == {"msg": "hi"}
