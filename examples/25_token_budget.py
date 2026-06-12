"""
================================================================
EXAMPLE 25 — "Token metering, cost sinks, and hard caps" (ADVANCED)
================================================================

SCENARIO:
    Agent token spend explodes silently: a loop retries, fan-outs multiply,
    and the bill arrives later. Lynx's stance: the kernel counts and
    enforces counts; money never enters the kernel. Adapters report
    input/output tokens per step; you do three things with them:

      1. WATCH  — step.usage events stream to your sink
      2. PRICE  — your sink multiplies counts by YOUR rates (no price
                  tables in Lynx; they go stale weekly)
      3. CAP    — Budget(output_tokens=...) stops a runaway loop between
                  steps, exactly like Budget(steps=...) does

WHAT THIS EXAMPLE SHOWS:
    - A scripted "model" attaching Usage to each step (what ClaudeAgent /
      OpenAIAgent do automatically from the real API response)
    - A ~15-line cost sink with user-supplied rates
    - A runaway agent stopped by an output-token cap, with the honest
      caveat on display: the cap stops the NEXT call; the step that
      crossed the line already happened
    - Lifetime totals on RunResult.usage

RUN WITH:
    python examples/25_token_budget.py
"""

from __future__ import annotations

import asyncio

from lynx import (
    Budget,
    FinalAnswer,
    Message,
    ToolCall,
    ToolSet,
    Usage,
    compile_policy,
    run_agent,
    tool,
)

POLICY = "version: 1\ndefaults: { on_no_match: allow }\nrules: []"


@tool(reversible=True, scope=("compute:exec",))
async def search(query: str) -> str:
    return f"10 results for {query!r}"


# ---------------------------------------------------------------------------
# A scripted "model" that reports usage — exactly what the real adapters
# (ClaudeAgent / OpenAIAgent) attach from the provider's API response.
# ---------------------------------------------------------------------------


class ChattyResearcher:
    """Keeps searching forever — the runaway-loop failure mode."""

    async def step(self, conv: tuple[Message, ...]):
        n = sum(1 for m in conv if m.role == "tool")
        return ToolCall(
            tool="search",
            args={"query": f"subtopic {n}"},
            call_id=f"c{n}",
            # Each step "costs" tokens; conversation grows, so input grows.
            usage=Usage(
                input_tokens=1_000 + 500 * n,
                output_tokens=400,
                model="claude-haiku-4-5",
            ),
        )


class BriefAgent:
    """Two steps and done — the well-behaved case."""

    async def step(self, conv: tuple[Message, ...]):
        if not any(m.role == "tool" for m in conv):
            return ToolCall(
                tool="search",
                args={"query": "lynx"},
                call_id="c1",
                usage=Usage(input_tokens=1_200, output_tokens=350, model="claude-haiku-4-5"),
            )
        return FinalAnswer(
            text="lynx: a policy kernel for agent tool calls",
            usage=Usage(input_tokens=2_100, output_tokens=180, model="claude-haiku-4-5"),
        )


# ---------------------------------------------------------------------------
# The cost sink — YOUR rates, YOUR currency, YOUR alerting. ~15 lines.
# ---------------------------------------------------------------------------

MY_RATES = {  # $ per million tokens — you maintain these, not Lynx
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
}


def make_cost_sink():
    spent = {"usd": 0.0}

    async def cost_sink(event):
        if event.kind != "step.usage":
            return
        body = event.body
        rates = MY_RATES.get(body["model"] or "", {"in": 0, "out": 0})
        step_usd = (
            (body["input_tokens"] or 0) * rates["in"] + (body["output_tokens"] or 0) * rates["out"]
        ) / 1_000_000
        spent["usd"] += step_usd
        print(
            f"    [cost sink] step {body['seq']}: "
            f"{body['input_tokens']:>6} in / {body['output_tokens']:>4} out "
            f"-> ${step_usd:.6f}  (run total ${spent['usd']:.6f})"
        )

    return cost_sink


async def main() -> None:
    policy = compile_policy(POLICY)
    tools = ToolSet.from_functions(search)

    print("=" * 64)
    print("Act 1 — a well-behaved agent: metered, totaled, priced")
    print("=" * 64)
    result = await run_agent(
        BriefAgent(),
        task="What is lynx?",
        tools=tools,
        policy=policy,
        sinks=(make_cost_sink(),),
    )
    print(f"  final  : {result.final_answer}")
    print(f"  totals : {result.usage}")

    print()
    print("=" * 64)
    print("Act 2 — a runaway loop stopped by Budget(output_tokens=2000)")
    print("=" * 64)
    result = await run_agent(
        ChattyResearcher(),  # would search forever
        task="Research everything about everything",
        tools=tools,
        policy=policy,
        sinks=(make_cost_sink(),),
        budget=Budget(steps=100, output_tokens=2_000),
    )
    print(f"  error  : {result.error}")
    print(f"  totals : {result.usage}")
    print()
    print("  Note the honest caveat: the cap stopped the NEXT model call —")
    print("  the step that crossed 2000 output tokens had already happened.")
    print("  That's true of every in-loop limiter; Lynx just says it out loud.")


if __name__ == "__main__":
    asyncio.run(main())
