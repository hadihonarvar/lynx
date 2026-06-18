"""Kill-switch (cooperative cancellation) + repetition gate.

The kernel honors a CancelToken at every step boundary and immediately
before each tool executes — so a cancelled run stops after at most one
in-flight model/tool call, never the rest of the run. The repetition gate
trips the classic "same tool, same args, forever" loop.
"""

from __future__ import annotations

from typing import Any

from lynx import (
    Budget,
    CancelToken,
    FinalAnswer,
    Message,
    ToolCall,
    ToolSet,
    callback_sink,
    compile_policy,
    run_agent,
    tool,
)

ALLOW_ALL = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []")

CALLS: list[str] = []


@tool(reversible=True, scope=("compute:exec",))
async def do_thing(x: int) -> str:
    CALLS.append(f"do_thing:{x}")
    return f"did {x}"


def _reset():
    CALLS.clear()


def _collector():
    seen: list[Any] = []

    async def collect(ev):
        seen.append(ev)

    return seen, callback_sink(collect)


class Scripted:
    def __init__(self, *actions: Any) -> None:
        self._actions = list(actions)
        self.step_calls = 0

    async def step(self, conversation: tuple[Message, ...]):
        self.step_calls += 1
        return self._actions.pop(0)


# --- kill-switch -------------------------------------------------------------


async def test_cancel_before_start_executes_nothing() -> None:
    _reset()
    cancel = CancelToken()
    cancel.cancel("stop now")
    agent = Scripted(ToolCall(tool="do_thing", args={"x": 1}, call_id="c"), FinalAnswer(text="x"))
    seen, sink = _collector()
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(do_thing),
        policy=ALLOW_ALL,
        sinks=(sink,),
        cancel=cancel,
    )
    assert result.error == "cancelled: stop now"
    assert agent.step_calls == 0  # never even asked the model
    assert CALLS == []  # executed nothing
    assert "run.cancelled" in [e.kind for e in seen]


async def test_cancel_mid_run_stops_before_next_tool() -> None:
    _reset()
    cancel = CancelToken()

    # A sink that flips the kill-switch the moment the first tool completes.
    async def trip_after_first(event):
        if event.kind == "action.completed":
            cancel.cancel("kill after first action")

    agent = Scripted(
        ToolCall(tool="do_thing", args={"x": 1}, call_id="c1"),
        ToolCall(tool="do_thing", args={"x": 2}, call_id="c2"),  # must NOT run
        FinalAnswer(text="never"),
    )
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(do_thing),
        policy=ALLOW_ALL,
        sinks=(callback_sink(trip_after_first),),
        cancel=cancel,
    )
    assert result.error == "cancelled: kill after first action"
    assert CALLS == ["do_thing:1"]  # exactly one action executed
    assert result.final_answer is None


async def test_cancel_token_is_idempotent_first_reason_wins() -> None:
    c = CancelToken()
    assert c.cancelled is False
    c.cancel("first")
    c.cancel("second")
    assert c.cancelled is True
    assert c.reason == "first"


async def test_no_cancel_token_behaves_normally() -> None:
    _reset()
    agent = Scripted(
        ToolCall(tool="do_thing", args={"x": 1}, call_id="c"), FinalAnswer(text="done")
    )
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(do_thing),
        policy=ALLOW_ALL,
    )
    assert result.final_answer == "done"
    assert CALLS == ["do_thing:1"]


# --- repetition gate ---------------------------------------------------------


async def test_repetition_gate_trips_on_identical_calls() -> None:
    _reset()
    agent = Scripted(
        ToolCall(tool="do_thing", args={"x": 7}, call_id="c1"),
        ToolCall(tool="do_thing", args={"x": 7}, call_id="c2"),
        ToolCall(tool="do_thing", args={"x": 7}, call_id="c3"),  # 3rd identical → trips
        FinalAnswer(text="never"),
    )
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(do_thing),
        policy=ALLOW_ALL,
        budget=Budget(max_repeated_calls=2),
    )
    assert result.error is not None and "repeated call limit" in result.error
    assert CALLS == ["do_thing:7", "do_thing:7"]  # only the allowed two ran


async def test_repetition_gate_resets_on_different_args() -> None:
    _reset()
    agent = Scripted(
        ToolCall(tool="do_thing", args={"x": 1}, call_id="c1"),
        ToolCall(tool="do_thing", args={"x": 2}, call_id="c2"),
        ToolCall(tool="do_thing", args={"x": 1}, call_id="c3"),
        FinalAnswer(text="done"),
    )
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(do_thing),
        policy=ALLOW_ALL,
        budget=Budget(max_repeated_calls=2),
    )
    # x=1 appears twice (== limit, not over), x=2 once → never trips
    assert result.final_answer == "done"
    assert CALLS == ["do_thing:1", "do_thing:2", "do_thing:1"]
