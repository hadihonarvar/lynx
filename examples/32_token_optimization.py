"""
================================================================
EXAMPLE 32 — "Token optimization: the compressor seam" (ADVANCED)
================================================================

SCENARIO:
    Policy decides WHETHER an action runs; the executor decides WHERE.
    The compressor seam decides HOW MUCH OF THE RESULT the model has to
    read. A tool that dumps 40 KB of logs or a 2,000-line `ls -R` doesn't
    just cost tokens once — that blob is re-sent in full on every later
    step of the loop. Trim it once at the boundary and the saving compounds.

    Lynx is NOT a token optimizer and never will be (an external semantic
    cache / LLM gateway is a non-goal — see docs/what-lynx-is-and-isnt.md).
    Lynx owns the seam; the strategy is YOURS — exactly like "you bring the
    sandbox." It ships a few pure-Python reference compressors so the common
    cases (truncate, dedup) need no code.

WHAT THIS EXAMPLE SHOWS:
    - Passing `compressor=` to run_agent: every fresh string result is
      shrunk BEFORE it enters the conversation, the journal, and any replay
    - compose_compressors(dedup, truncate): collapse repeated lines, then cap
    - route_compressor + @tool(compress=...): per-tool strategy, and one tool
      opting OUT of compression entirely
    - The `step.compressed` audit event: observe chars-in / chars-out so the
      saving is measurable, not a vibe
    - Fail-open: a broken compressor never drops a tool's real output

A NOTE ON RTK (https://github.com/rtk-ai/rtk):
    RTK ("Rust Token Killer") saves tokens by RUNNING the command itself and
    has no stdin-filter mode — so it cannot post-process a result Lynx has
    already produced. Wire RTK at the TOOL level: have your shell tool run
    `rtk <cmd>` / `rtk proxy <cmd>` instead of the raw command. This seam is
    the framework-native pass for every other (non-command) tool. For a
    generic external filter binary you maintain, see external_filter_compressor.

A NOTE ON PROMPT CACHING (the other half of the token bill):
    The Anthropic adapter (ClaudeAgent) marks the system prompt, tool schemas,
    and conversation prefix with cache breakpoints by default (cache_prompt=True),
    so a long loop re-reads prior turns from cache instead of re-billing them.
    That lives in the adapter, not here — nothing for you to wire.

RUN WITH:
    python examples/32_token_optimization.py
"""

from __future__ import annotations

import asyncio

from lynx import (
    FinalAnswer,
    Message,
    ToolCall,
    ToolSet,
    compile_policy,
    compose_compressors,
    dedup_compressor,
    identity_compressor,
    route_compressor,
    run_agent,
    tool,
    truncate_compressor,
)

POLICY = "version: 1\ndefaults: { on_no_match: allow }\nrules: []"


# ---------------------------------------------------------------------------
# Tools — the compress= hint is how a tool asks for a specific compressor.
# A tool can also opt OUT by routing to identity (e.g. output the agent
# must see verbatim).
# ---------------------------------------------------------------------------


@tool(reversible=True, scope=("compute:read",), compress="logs")
async def tail_log() -> str:
    """A noisy log: one real error buried in 200 identical heartbeat lines."""
    return "heartbeat ok\n" * 200 + "ERROR: disk full on /var\n" + "heartbeat ok\n" * 50


@tool(reversible=True, scope=("compute:read",))
async def list_tree() -> str:
    """A big directory listing — no hint, so it takes the default route."""
    return "\n".join(f"src/module_{i}/file_{j}.py" for i in range(60) for j in range(8))


@tool(reversible=True, scope=("compute:read",), compress="raw")
async def get_token() -> str:
    """A short, exact value the agent must see in full — opts OUT of compression."""
    return "verification-code: 814-220"


# ---------------------------------------------------------------------------
# The compressor: a router, exactly like route_executor.
#   - "logs" tools: collapse duplicate lines, THEN cap length
#   - default (None): just cap length
#   - "raw" tools: identity — never touched
# ---------------------------------------------------------------------------

COMPRESSOR = route_compressor(
    {
        None: truncate_compressor(max_chars=600),
        "logs": compose_compressors(dedup_compressor(), truncate_compressor(max_chars=600)),
        "raw": identity_compressor(),
    }
)


class Scripted:
    def __init__(self, *actions):
        self._actions = list(actions)

    async def step(self, conv: tuple[Message, ...]):
        return self._actions.pop(0)


async def main() -> None:
    policy = compile_policy(POLICY)
    tools = ToolSet.from_functions(tail_log, list_tree, get_token)

    agent = Scripted(
        ToolCall(tool="tail_log", args={}, call_id="c1"),
        ToolCall(tool="list_tree", args={}, call_id="c2"),
        ToolCall(tool="get_token", args={}, call_id="c3"),
        FinalAnswer(text="done — see what got compressed"),
    )

    print("=" * 64)
    print("One run, three tools, three compression strategies")
    print("=" * 64)

    saved = []

    async def sink(event):
        if event.kind == "step.compressed":
            saved.append(event.body)
            b = event.body
            print(
                f"  [step.compressed] {b['tool']:<10} "
                f"{b['before_chars']:>6} -> {b['after_chars']:>4} chars "
                f"(~{b['est_tokens_saved']} tokens saved)"
            )

    result = await run_agent(
        agent,
        task="inspect the box",
        tools=tools,
        policy=policy,
        sinks=(sink,),
        compressor=COMPRESSOR,
    )

    print(f"\n  final: {result.final_answer}")
    print("\n  What happened:")
    print("  - tail_log   -> 'logs' route  (dedup heartbeats, then cap)")
    print("  - list_tree  -> default route (cap to 600 chars)")
    print("  - get_token  -> 'raw' route   (identity: short exact value kept whole,")
    print("                                 so NO step.compressed event for it)")
    total = sum(b["before_chars"] - b["after_chars"] for b in saved)
    print(f"\n  {len(saved)} results compressed, ~{total // 4} tokens saved this run")
    print("  (and re-saved on every subsequent step those results would be re-sent)")


if __name__ == "__main__":
    asyncio.run(main())
