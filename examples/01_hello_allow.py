"""
================================================================
EXAMPLE 01 — "Just see it work" (SIMPLE)
================================================================

SCENARIO:
    The smallest possible Lynx run. We ask the assistant to list files
    in the current directory. The policy allows everything. The result
    comes back. If THIS works, your install is correct.

WHAT THIS EXAMPLE SHOWS:
    - The smallest @tool you can write
    - The smallest YAML policy ("allow everything")
    - The entry point: `run_agent(...)` — no Runtime class
    - Stdout sink: events stream to terminal as they happen
    - Nothing persisted on disk

RUN WITH:
    python examples/01_hello_allow.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from lynx import (
    FinalAnswer,
    Message,
    ToolCall,
    ToolSet,
    auto_deny,
    compile_policy,
    run_agent,
    stdout_sink,
    tool,
)


@tool(reversible=True, scope=("filesystem:read",))
async def list_files(path: str = ".") -> list[str]:
    """List the files in a directory."""
    return sorted(p.name for p in Path(path).iterdir())


class HelloAgent:
    """Asks for files once, then finishes."""

    async def step(self, conversation: tuple[Message, ...]):
        # If we've seen a tool result, we're done.
        for m in conversation:
            if m.role == "tool":
                return FinalAnswer(text="Listed the files.")
        return ToolCall(tool="list_files", args={"path": "."}, call_id="c1")


async def main() -> None:
    policy = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []")
    tools = ToolSet.from_functions(list_files)

    result = await run_agent(
        HelloAgent(),
        task="List the files here.",
        tools=tools,
        policy=policy,
        sinks=(stdout_sink(),),
        on_approval=auto_deny("not used"),
    )

    print()
    print(f"Status:  {'succeeded' if result.error is None else 'failed'}")
    print(f"Final:   {result.final_answer}")
    print(f"Correlation: {result.correlation_id}")


if __name__ == "__main__":
    asyncio.run(main())
