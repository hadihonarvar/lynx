"""
================================================================
EXAMPLE 18 — "subprocess_executor — best-effort resource caps" (ADVANCED)
================================================================

SCENARIO:
    Some tools you can't fully trust to be well-behaved — even your own.
    Since v2.3 you don't wrap each tool body by hand: pass
    `executor=subprocess_executor(...)` to `run_agent` and EVERY approved
    action runs in a fresh Python interpreter with best-effort POSIX
    resource limits and a wall-clock timeout. Tool authors write plain
    tools; the executor bounds them.

    **This is NOT a security boundary.** It bounds the blast radius of a
    runaway-but-trusted tool (CPU, memory, hangs). It will not contain an
    adversary — for that, plug your own container/microVM executor into
    the same seam (see example 26 and SECURITY.md).

WHAT THIS EXAMPLE SHOWS:
    - `subprocess_executor()` on the executor seam: zero changes to the
      tools themselves
    - A runaway tool killed by the wall-clock timeout — subprocess killed
      AND reaped (no zombies, no leaked pipes), surfaced to the agent as
      a normal `[error] SandboxError: ...` it can adapt to
    - The constraint the subprocess mechanism imposes: tool functions
      must be top-level (picklable) — no lambdas/closures
    - The pre-seam manual API (`lynx.sandbox.run_in_subprocess`) still
      exists for wrapping a single call by hand; the executor is the
      same mechanism applied at the chokepoint

RUN WITH:
    python examples/18_sandboxed_tool.py
"""

from __future__ import annotations

import asyncio
import time

from lynx import (
    FinalAnswer,
    Message,
    ToolCall,
    ToolSet,
    auto_deny,
    compile_policy,
    run_agent,
    stdout_sink,
    subprocess_executor,
    tool,
)

# ---------------------------------------------------------------------------
# Plain tools — note there is NO sandbox code in them. They must be
# top-level functions (the subprocess pickles them across the boundary).
# ---------------------------------------------------------------------------


@tool(reversible=True, scope=("compute:exec",))
async def heavy_compute(n: int) -> int:
    """CPU work that finishes — completes inside the child interpreter."""
    total = 0
    for i in range(n):
        total += i * i
    return total


@tool(reversible=True, scope=("compute:exec",))
async def runaway_compute(n: int) -> int:
    """Burns CPU forever — the executor's timeout saves the parent."""
    while True:
        for i in range(n):
            _ = i * i


POLICY = "version: 1\ndefaults: { on_no_match: allow }\nrules: []"


class _Agent:
    def __init__(self):
        self._plan = [
            ToolCall("heavy_compute", {"n": 100_000}, call_id="c1"),
            ToolCall("runaway_compute", {"n": 1_000_000}, call_id="c2"),
            FinalAnswer(text="sandbox demo complete — the runaway was contained"),
        ]
        self._i = 0

    async def step(self, conv: tuple[Message, ...]):
        a = self._plan[self._i]
        self._i += 1
        return a


async def main() -> None:
    t0 = time.monotonic()
    result = await run_agent(
        _Agent(),
        task="run two tools under resource caps",
        tools=ToolSet.from_functions(heavy_compute, runaway_compute),
        policy=compile_policy(POLICY),
        sinks=(stdout_sink(),),
        on_approval=auto_deny("not used"),
        # The whole point: one line, every approved action runs capped.
        executor=subprocess_executor(
            cpu_seconds=5,
            max_memory_mb=256,
            timeout_seconds=2.0,  # the runaway tool dies here
        ),
    )
    elapsed = time.monotonic() - t0
    print()
    print(f"Final answer: {result.final_answer}")
    print(f"Total wall clock: {elapsed:.2f}s — the timeout fired cleanly.")
    print()
    print("Notice:")
    print("  - The tools contain ZERO sandbox code; the executor seam applied")
    print("    the caps to every approved action (see example 26 to mix")
    print("    executors per tool with route_executor + @tool(isolation=...))")
    print("  - runaway_compute's child was killed after ~2s and REAPED")
    print("    (no zombies, no leaked stdout/stderr pipes)")
    print("  - The SandboxError surfaced as an [error] tool message the agent")
    print("    could see and adapt to — the run finished normally")
    print()
    print("Manual alternative: lynx.sandbox.run_in_subprocess(fn, args, ...)")
    print("wraps a single call by hand — same mechanism, same caveats.")


if __name__ == "__main__":
    asyncio.run(main())
