"""
================================================================
EXAMPLE 05 — "A real AI brain, hitting the wall" (MORE COMPLEX)
================================================================

SCENARIO:
    Earlier examples used a "puppet" assistant: it played a fixed script,
    we knew exactly what it would say.

    This example uses a REAL AI brain — Claude (or ChatGPT). You give it
    a task; it actually thinks; it sometimes proposes something dangerous.
    Lynx blocks the dangerous proposal exactly like it blocked the puppet's.

    The point: Lynx is not specific to scripted demos. It does the same job
    when a real LLM is driving — which is the actual production case.

REAL-WORLD USE CASE:
    Any production deployment of an AI agent:
      - Claude in your customer support pipeline
      - GPT-5 in your DevOps assistant
      - Gemini in your data analyst bot
    Lynx sits underneath the model and enforces the YAML policy the same
    way regardless of which model is driving.

WHAT THIS EXAMPLE SHOWS:
    - Plugging in a real ClaudeAgent (or OpenAIAgent)
    - The real LLM's tool-call decisions flowing through Lynx
    - A genuine policy denial fed back to the model
    - The model recovering and giving a final answer

REQUIRES:
    pip install anthropic            (for Claude)
        or
    pip install openai               (for GPT)
    export ANTHROPIC_API_KEY=...     (or OPENAI_API_KEY=...)

RUN WITH:
    python examples/05_real_llm_blocked.py

WHAT YOU'LL SEE:
    Claude (or GPT) chooses what shell commands to propose.
    Some will be allowed, some will be blocked.
    The model sees each denial in the next turn and adjusts.
"""

from __future__ import annotations

import asyncio
import os

from lynx import Runtime, tool
from lynx.core.mediator import get_registry
from lynx.policy import compile_policy
from lynx.stores.sqlite import SQLiteStore

# ---------------------------------------------------------------------------
# Tool — a constrained shell.
# ---------------------------------------------------------------------------


@tool(cost="medium", reversible=True, scope=["compute:exec"])
async def shell(cmd: str) -> str:
    """Run a shell command. Policy decides what's allowed."""
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return (out + err).decode().strip() or "(no output)"


# ---------------------------------------------------------------------------
# Policy — block known-catastrophic; allow read-only inspection.
# ---------------------------------------------------------------------------


POLICY = """
version: 1
defaults:
  on_no_match: deny

rules:
  - id: block-rm-rf-root
    priority: 100
    match:
      tool: shell
      args.cmd.matches: '^\\s*rm\\s+(-[rRf]+\\s+)+/(\\s|$)'
    decision: deny
    reason: "rm -rf / is hard-blocked"

  - id: block-system-paths
    priority: 90
    match:
      tool: shell
      args.cmd.matches: '(/etc/|/System/|/Library/(?!Caches)|/root/)'
    decision: deny
    reason: "writes to system-owned paths are forbidden"

  - id: allow-read-only-inspection
    priority: 50
    match:
      tool: shell
      args.cmd.matches: '^(ls|cat|head|tail|grep|find|du|wc|file|stat)\\s'
    decision: allow

  - id: allow-echo
    priority: 30
    match:
      tool: shell
      args.cmd.matches: '^echo\\s'
    decision: allow
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    import tempfile

    if os.getenv("ANTHROPIC_API_KEY"):
        from lynx.adapters.anthropic_sdk import ClaudeAgent

        agent = ClaudeAgent(
            model="claude-opus-4-7",
            system=(
                "You are a careful sysadmin running on a shared host. "
                "You may inspect freely (ls/cat/find) but must not modify the "
                "filesystem. If your action gets denied, acknowledge it and "
                "try something safer."
            ),
        )
        provider = "Anthropic Claude"
    elif os.getenv("OPENAI_API_KEY"):
        from lynx.adapters.openai_sdk import OpenAIAgent

        agent = OpenAIAgent(
            model="gpt-5",
            system=(
                "You are a careful sysadmin. Inspect freely, modify nothing. "
                "Acknowledge denials and try safer alternatives."
            ),
        )
        provider = "OpenAI GPT"
    else:
        print("ERROR: set ANTHROPIC_API_KEY or OPENAI_API_KEY in your environment first.")
        print()
        print("This example uses a REAL LLM to drive the agent loop.")
        print("Try example 02_block_dangerous.py for an offline scripted version.")
        return

    print(f"Using:  {provider}")
    print("Task:   inspect what's running on this host without modifying anything.")
    print()

    with tempfile.TemporaryDirectory() as tmp:
        runtime = Runtime(
            store=SQLiteStore(f"{tmp}/state.db"),
            policy=compile_policy(POLICY),
        )

        result = await runtime.run(
            agent=agent,
            task=(
                "Inspect the contents of /tmp. List the files. "
                "Don't try to modify anything. When you're done, summarize what's there."
            ),
            budget={"steps": 10, "duration_seconds": 120, "usd": 0.50},
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
