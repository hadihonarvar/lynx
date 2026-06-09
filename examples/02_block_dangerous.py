"""
================================================================
EXAMPLE 02 — "Block the catastrophic command" (SIMPLE)
================================================================

SCENARIO:
    Imagine you've hired a smart but careless assistant.  You ask them to
    tidy up. They get confused and pick up a flamethrower to clean the
    dishes.

    Without Lynx: the flamethrower goes off. House gone.
    With Lynx:    you wrote one sticky note: "NEVER USE A FLAMETHROWER."
                  When the assistant reaches for it, Lynx says "no."
                  The assistant reads "no — try something safer" and
                  reaches for the sponge instead.

    In the computer world, the flamethrower is `rm -rf /` (a command that
    deletes everything on the disk). One typo or one confused AI and the
    machine is wiped. With Lynx, that single command is blocked at the
    kernel level.

REAL-WORLD USE CASE:
    Hard-blocking known-catastrophic commands. The kind of thing you NEVER
    want any agent to be able to do, regardless of how it got into the
    conversation. Think: dropping all database tables, force-pushing to
    main, terminating production VMs.

WHAT THIS EXAMPLE SHOWS:
    - A DENY verdict triggered by a regex rule
    - The denial getting fed back to the agent
    - The agent (which would normally retry safer) sees the denial and
      moves on
    - The dangerous syscall NEVER runs

RUN WITH:
    python examples/02_block_dangerous.py

WHAT YOU'LL SEE:
    Step 0:  shell("ls /tmp")        → allow  ✓
    Step 1:  shell("rm -rf /")       → DENY   ✗  (rm -rf / is hard-blocked)
    Status:  succeeded
    Final:   I tried something safe and something dangerous; the dangerous
             one was blocked. Working as intended.
"""

from __future__ import annotations

import asyncio

from lynx import FinalAnswer, Message, Runtime, ToolCall, tool
from lynx.core.mediator import get_registry
from lynx.policy import compile_policy
from lynx.stores.sqlite import SQLiteStore

# ---------------------------------------------------------------------------
# Tool — a shell command. Marked reversible=True for demo simplicity
# (real shell tools should be reversible=False, but the policy below uses
# `default: allow` so we keep this minimal).
# ---------------------------------------------------------------------------


@tool(cost="medium", reversible=True, scope=["compute:exec"])
async def shell(cmd: str) -> str:
    """Run a shell command and return its output."""
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return (out + err).decode().strip() or "(no output)"


# ---------------------------------------------------------------------------
# Policy — one rule: hard-block `rm -rf /`. Everything else is fine.
# ---------------------------------------------------------------------------


POLICY = """
version: 1
defaults:
  on_no_match: allow

rules:
  - id: block-rm-rf-root
    description: "Never delete from filesystem root"
    match:
      tool: shell
      args.cmd.matches: '^\\s*rm\\s+(-[rRf]+\\s+)+/(\\s|$)'
    decision: deny
    reason: "rm -rf / is hard-blocked by policy"
"""


# ---------------------------------------------------------------------------
# Agent — proposes one safe command and one catastrophic command.
# ---------------------------------------------------------------------------


class CarelessAgent:
    """Plays a script that includes a hallucinated dangerous command."""

    def __init__(self) -> None:
        self._step = 0
        self._plan = [
            ToolCall(tool="shell", args={"cmd": "ls /tmp"}, call_id="c1"),
            ToolCall(tool="shell", args={"cmd": "rm -rf /"}, call_id="c2"),  # the bad one
            FinalAnswer(
                text=(
                    "I tried something safe and something dangerous; "
                    "the dangerous one was blocked. Working as intended."
                )
            ),
        ]

    async def step(self, conversation: list[Message]):
        action = self._plan[self._step]
        self._step += 1
        return action


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        runtime = Runtime(
            store=SQLiteStore(f"{tmp}/state.db"),
            policy=compile_policy(POLICY),
        )

        result = await runtime.run(
            agent=CarelessAgent(),
            task="Demonstrate that catastrophic commands get blocked.",
            principal={"kind": "user", "id": "demo"},
        )

        print(f"Status:   {result.status}")
        print(f"Final:    {result.final_answer}")
        print(f"Run ID:   {result.run_id}")
        print()
        print("Step-by-step:")
        for step in runtime.get_steps(result.run_id):
            verdict = step.decision.verdict.value if step.decision else "?"
            mark = "✓" if verdict == "allow" else "✗"
            cmd = step.action.args.get("cmd", "?") if step.action else "?"
            reason = step.decision.reason if step.decision else ""
            print(f"  #{step.seq}  shell({cmd!r})  →  {verdict}  {mark}  {reason}")


if __name__ == "__main__":
    get_registry().clear()

    @tool(cost="medium", reversible=True, scope=["compute:exec"])
    async def shell(cmd: str) -> str:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return (out + err).decode().strip() or "(no output)"

    asyncio.run(main())
