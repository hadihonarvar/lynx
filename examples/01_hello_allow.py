"""
================================================================
EXAMPLE 01 — "Just see it work" (SIMPLE)
================================================================

SCENARIO:
    This is the smallest possible example. Like turning a key in a car
    to make sure the engine starts before you drive anywhere.

    The smart assistant is asked "what files are in this folder?"
    Lynx checks its rulebook and says "sure, just looking is harmless,
    let it through."  The answer comes back.

    If THIS works, Lynx is installed correctly and you're ready for
    the more interesting examples.

REAL-WORLD USE CASE:
    Smoke-testing your Lynx install. Confirming the loop works end-to-end:
    agent proposes an action → policy allows it → action runs → result
    flows back → audit log writes the event.

WHAT THIS EXAMPLE SHOWS:
    - The simplest @tool you can register
    - The simplest possible YAML policy ("allow everything")
    - One ALLOW verdict
    - The minimal agent loop

RUN WITH:
    python examples/01_hello_allow.py

WHAT YOU'LL SEE:
    Status:  succeeded
    Files:   ['README.md', 'src', 'tests', ...]
    Run ID:  R-...

That's it. No magic, no danger, no surprise. The point is to confirm
the whole pipeline is connected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from lynx import FinalAnswer, Message, Runtime, ToolCall, tool
from lynx.core.mediator import get_registry
from lynx.policy import compile_policy
from lynx.stores.sqlite import SQLiteStore

# ---------------------------------------------------------------------------
# Tool — a single read-only function the agent can call.
# ---------------------------------------------------------------------------


@tool(cost="low", reversible=True, scope=["filesystem:read"])
async def list_files(path: str = ".") -> list[str]:
    """List the files in a directory."""
    return sorted(p.name for p in Path(path).iterdir())


# ---------------------------------------------------------------------------
# Policy — "allow anything."  Real policies are stricter; this is hello-world.
# ---------------------------------------------------------------------------


POLICY = """
version: 1
defaults:
  on_no_match: allow
rules: []
"""

# ---------------------------------------------------------------------------
# Agent — a tiny scripted agent: ask for files, then finish.
# ---------------------------------------------------------------------------


class HelloAgent:
    def __init__(self) -> None:
        self._step = 0
        self._plan = [
            ToolCall(tool="list_files", args={"path": "."}, call_id="c1"),
            FinalAnswer(text="Listed the files."),
        ]

    async def step(self, conversation: list[Message]):
        action = self._plan[self._step]
        self._step += 1
        return action


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    # In a real project you'd point at .lynx/state.db; here we keep state
    # in-memory by using a fresh temp path that we discard at exit.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        runtime = Runtime(
            store=SQLiteStore(f"{tmp}/state.db"),
            policy=compile_policy(POLICY),
        )

        result = await runtime.run(
            agent=HelloAgent(),
            task="List the files in the current directory.",
            principal={"kind": "user", "id": "demo"},
        )

        print(f"Status:  {result.status}")
        print(f"Final:   {result.final_answer}")
        print(f"Run ID:  {result.run_id}")

        steps = runtime.get_steps(result.run_id)
        if steps and steps[0].result:
            print(f"Files:   {steps[0].result.value}")


if __name__ == "__main__":
    # Lynx's tool registry is process-global; clear it so re-runs are clean.
    get_registry().clear()

    @tool(cost="low", reversible=True, scope=["filesystem:read"])
    async def list_files(path: str = ".") -> list[str]:
        return sorted(p.name for p in Path(path).iterdir())

    asyncio.run(main())
