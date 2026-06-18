"""
================================================================
EXAMPLE 31 — "Kill-switch + repetition gate: stopping runaways" (ADVANCED)
================================================================

SCENARIO:
    Two ways agents go rogue in production:
      1. They ignore "stop" — a cancel that only checks between turns lets a
         runaway keep editing files for minutes after you hit the button.
      2. They loop — calling the same tool with the same args forever.

    Lynx owns the propose→decide→execute chokepoint, so it can stop both
    cleanly:
      - a CancelToken is checked at every step boundary AND right before each
        tool executes, so cancelling stops the run after at most ONE more
        action — never the rest of it.
      - Budget(max_repeated_calls=N) trips the identical-call loop, keyed on
        tool + args so a genuinely different argument never trips it.

WHAT THIS EXAMPLE SHOWS:
    - A live kill-switch flipped mid-run (here, by a sink watching events);
      the next queued action never executes
    - The repetition gate stopping a same-tool-same-args loop
    - Both ending in a clean RunResult.error, never a crash

RUN WITH:
    python examples/31_kill_switch.py
"""

from __future__ import annotations

import asyncio

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

# on_missing_shadow: allow so the irreversible edit tool runs without an
# approval gate — this example is about the kill-switch, not approvals.
POLICY = compile_policy(
    "version: 1\ndefaults: { on_no_match: allow, on_missing_shadow: allow }\nrules: []"
)

EDITS: list[str] = []


@tool(reversible=False, scope=("fs:write",))
async def edit_file(path: str) -> str:
    EDITS.append(path)
    return f"edited {path}"


class RunawayEditor:
    """Would edit 6 files if nothing stopped it."""

    def __init__(self) -> None:
        self._n = 0

    async def step(self, conv: tuple[Message, ...]):
        self._n += 1
        if self._n > 6:
            return FinalAnswer(text="done (should never reach this)")
        return ToolCall("edit_file", {"path": f"file_{self._n}.py"}, call_id=f"c{self._n}")


class StubbornLooper:
    """Calls the same tool with identical args forever."""

    async def step(self, conv: tuple[Message, ...]):
        return ToolCall("edit_file", {"path": "config.py"}, call_id="c")


async def main() -> None:
    # ---- Act 1: a human hits stop after 2 edits --------------------------
    print("=" * 64)
    print("Act 1 — kill-switch: stop a runaway after the 2nd edit")
    print("=" * 64)
    EDITS.clear()
    cancel = CancelToken()

    async def stop_after_two(event):
        if event.kind == "action.completed" and len(EDITS) >= 2:
            cancel.cancel("operator hit stop")

    result = await run_agent(
        RunawayEditor(),
        task="refactor everything",
        tools=ToolSet.from_functions(edit_file),
        policy=POLICY,
        sinks=(callback_sink(stop_after_two),),
        cancel=cancel,
    )
    print(f"  result : {result.error}")
    print(f"  edits  : {EDITS}")
    print("  → the 3rd edit was queued but the kill-switch stopped it first.")

    # ---- Act 2: the repetition gate breaks an identical-call loop ---------
    print()
    print("=" * 64)
    print("Act 2 — repetition gate: break a same-tool-same-args loop")
    print("=" * 64)
    EDITS.clear()
    result = await run_agent(
        StubbornLooper(),
        task="fix the config",
        tools=ToolSet.from_functions(edit_file),
        policy=POLICY,
        budget=Budget(max_repeated_calls=3),  # 3 identical calls allowed; the 4th trips
    )
    print(f"  result : {result.error}")
    print(f"  edits  : {len(EDITS)} identical calls ran before the gate tripped")
    print("  → no infinite loop, no crash — a clean, structured stop.")


if __name__ == "__main__":
    asyncio.run(main())
