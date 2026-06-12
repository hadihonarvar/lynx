"""Token usage emission + Budget token caps.

The kernel counts and enforces counts; it never converts tokens to money.
Agents that report no usage are simply unmetered — no events, no caps.
"""

from __future__ import annotations

from typing import Any

from lynx import (
    Budget,
    FinalAnswer,
    Message,
    ToolCall,
    ToolSet,
    Usage,
    callback_sink,
    compile_policy,
    run_agent,
    tool,
)

ALLOW_ALL = "version: 1\ndefaults: { on_no_match: allow }\nrules: []"


@tool(reversible=True, scope=("compute:exec",))
async def echo_u(msg: str) -> str:
    """Echo."""
    return msg


class MeteredAgent:
    """Scripted agent attaching Usage to each action, like a real adapter."""

    def __init__(self, *actions: Any) -> None:
        self._actions = list(actions)
        self.step_calls = 0

    async def step(self, conversation: tuple[Message, ...]):
        self.step_calls += 1
        return self._actions.pop(0)


def _collector():
    seen: list[Any] = []

    async def collect(ev):
        seen.append(ev)

    return seen, callback_sink(collect)


def _call(usage: Usage | None) -> ToolCall:
    return ToolCall(tool="echo_u", args={"msg": "hi"}, call_id="c", usage=usage)


async def test_usage_emitted_and_totaled() -> None:
    seen, sink = _collector()
    agent = MeteredAgent(
        _call(Usage(input_tokens=100, output_tokens=20, model="m1")),
        FinalAnswer(text="done", usage=Usage(input_tokens=150, output_tokens=30, model="m1")),
    )
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(echo_u),
        policy=compile_policy(ALLOW_ALL),
        sinks=(sink,),
    )
    assert result.final_answer == "done"
    assert result.usage == Usage(
        input_tokens=250, output_tokens=50, cache_read_tokens=0, cache_write_tokens=0
    )
    usage_events = [ev for ev in seen if ev.kind == "step.usage"]
    assert len(usage_events) == 2
    assert usage_events[0].body["input_tokens"] == 100
    assert usage_events[0].body["model"] == "m1"
    assert usage_events[1].body["total_input"] == 250
    assert usage_events[1].body["total_output"] == 50
    succeeded = next(ev for ev in seen if ev.kind == "run.succeeded")
    assert succeeded.body["usage"] == {"input_tokens": 250, "output_tokens": 50}


async def test_no_usage_means_no_metering() -> None:
    seen, sink = _collector()
    agent = MeteredAgent(_call(None), FinalAnswer(text="done"))
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(echo_u),
        policy=compile_policy(ALLOW_ALL),
        sinks=(sink,),
        budget=Budget(steps=10, tokens=1),  # cap can never trigger: nothing reported
    )
    assert result.final_answer == "done"
    assert result.usage is None
    assert not [ev for ev in seen if ev.kind == "step.usage"]
    assert "usage" not in next(ev for ev in seen if ev.kind == "run.succeeded").body


async def test_output_token_budget_stops_next_step() -> None:
    seen, sink = _collector()
    agent = MeteredAgent(
        _call(Usage(input_tokens=10, output_tokens=600)),
        _call(Usage(input_tokens=10, output_tokens=600)),  # never reached
        FinalAnswer(text="never"),
    )
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(echo_u),
        policy=compile_policy(ALLOW_ALL),
        sinks=(sink,),
        budget=Budget(steps=10, output_tokens=500),
    )
    assert result.final_answer is None
    assert result.error == "output token budget exhausted (500)"
    assert agent.step_calls == 1  # the cap stopped the NEXT model call
    assert result.usage is not None and result.usage.output_tokens == 600
    failed = next(ev for ev in seen if ev.kind == "run.failed")
    assert "output token budget" in failed.body["reason"]


async def test_combined_token_budget() -> None:
    agent = MeteredAgent(
        _call(Usage(input_tokens=300, output_tokens=300)),
        FinalAnswer(text="never"),
    )
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(echo_u),
        policy=compile_policy(ALLOW_ALL),
        budget=Budget(steps=10, tokens=500),
    )
    assert result.error == "token budget exhausted (500)"


async def test_input_token_budget() -> None:
    agent = MeteredAgent(
        _call(Usage(input_tokens=900)),
        FinalAnswer(text="never"),
    )
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(echo_u),
        policy=compile_policy(ALLOW_ALL),
        budget=Budget(steps=10, input_tokens=800),
    )
    assert result.error == "input token budget exhausted (800)"


async def test_usage_survives_journal_resume() -> None:
    """Replayed steps count toward lifetime totals and budgets, but step.usage
    is not re-emitted for them (the paying attempt already announced it)."""
    from tests.test_durability import MemoryStore

    policy = compile_policy(ALLOW_ALL)
    tools = ToolSet.from_functions(echo_u)
    store = MemoryStore()
    first = MeteredAgent(
        _call(Usage(input_tokens=100, output_tokens=40, model="m1")),
        FinalAnswer(text="done", usage=Usage(input_tokens=120, output_tokens=10, model="m1")),
    )
    r1 = await run_agent(first, task="t", tools=tools, policy=policy, store=store, run_id="r1")
    assert r1.usage is not None and r1.usage.input_tokens == 220

    # Completed-run re-invoke: lifetime totals reconstructed from the journal,
    # zero model calls, zero new step.usage events.
    seen, sink = _collector()
    r2 = await run_agent(
        MeteredAgent(),
        task="t",
        tools=tools,
        policy=policy,
        sinks=(sink,),
        store=store,
        run_id="r1",
    )
    assert r2.final_answer == "done"
    assert r2.usage is not None
    assert r2.usage.input_tokens == 220
    assert r2.usage.output_tokens == 50
    assert not [ev for ev in seen if ev.kind == "step.usage"]
