"""
================================================================
EXAMPLE 30 — "FinOps: per-customer chargeback from one sink" (ADVANCED)
================================================================

SCENARIO:
    "Which customer / feature / model is burning our token budget?"
    Provider dashboards can't answer it — they see API keys, not your
    customers. Lynx's answer: the kernel emits facts (step.usage with
    per-step model + token counts, run.started with the principal),
    and YOUR sink joins them into attribution. No price tables in Lynx,
    no proxy in front of your traffic, no analytics platform required —
    one ~30-line sink and a dict of YOUR negotiated rates.

WHAT THIS EXAMPLE SHOWS:
    - A fleet of runs across 3 customers and 2 models
    - One attribution sink that joins run.started (who) with step.usage
      (what it cost) by correlation_id
    - A chargeback table: $ per customer x model, plus fleet totals
    - The same fleet protected by Budget(tokens=...): the chatty
      customer's runaway run is stopped mid-flight — and the partial
      spend still lands in the attribution (real money was burned)

RUN WITH:
    python examples/30_cost_attribution.py
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from lynx import (
    Budget,
    FinalAnswer,
    Message,
    Principal,
    ToolCall,
    ToolSet,
    Usage,
    compile_policy,
    run_agent,
    tool,
)

POLICY = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []")

# YOUR rates — negotiated, discounted, internal-chargeback, whatever.
# Lynx never ships these; they'd be stale by Friday.
RATES = {  # $ per million tokens
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
    "claude-opus-4-8": {"in": 15.00, "out": 75.00},
}


@tool(reversible=True, scope=("kb:read",))
async def search_kb(q: str) -> str:
    return f"3 articles about {q!r}"


TOOLS = ToolSet.from_functions(search_kb)


# ---------------------------------------------------------------------------
# The attribution sink — the entire FinOps "integration". It joins
# run.started (principal) with step.usage (model + tokens) by correlation_id.
# ---------------------------------------------------------------------------


def make_attribution_sink():
    run_owner: dict[str, str] = {}  # correlation_id -> customer
    spend = defaultdict(float)  # (customer, model) -> usd
    tokens = defaultdict(int)  # customer -> total tokens

    async def sink(event):
        if event.kind == "run.started":
            run_owner[event.correlation_id] = event.body["principal_id"]
        elif event.kind == "step.usage":
            who = run_owner.get(event.correlation_id, "unknown")
            model = event.body["model"] or "unknown"
            rate = RATES.get(model, {"in": 0.0, "out": 0.0})
            i, o = event.body["input_tokens"] or 0, event.body["output_tokens"] or 0
            spend[(who, model)] += (i * rate["in"] + o * rate["out"]) / 1_000_000
            tokens[who] += i + o

    sink.spend = spend  # expose the aggregates for the report
    sink.tokens = tokens
    return sink


# ---------------------------------------------------------------------------
# Scripted "models" with usage attached (what real adapters do). The chatty
# one searches forever — Budget(tokens=...) is what stops it.
# ---------------------------------------------------------------------------


class SupportAgent:
    def __init__(self, model: str, searches: int, tokens_per_step: int) -> None:
        self.model, self.searches, self.tps = model, searches, tokens_per_step

    async def step(self, conv: tuple[Message, ...]):
        done = sum(1 for m in conv if m.role == "tool")
        usage = Usage(input_tokens=self.tps, output_tokens=self.tps // 5, model=self.model)
        if done < self.searches:
            return ToolCall("search_kb", {"q": f"topic {done}"}, call_id=f"c{done}", usage=usage)
        return FinalAnswer(text="answered", usage=usage)


FLEET = [
    # (customer, model, searches, tokens/step)  — opus runs cost ~15x haiku
    ("acme", "claude-haiku-4-5", 2, 2_000),
    ("acme", "claude-opus-4-8", 1, 3_000),
    ("globex", "claude-haiku-4-5", 3, 1_500),
    ("initech", "claude-haiku-4-5", 999, 4_000),  # runaway — capped below
]


async def main() -> None:
    sink = make_attribution_sink()

    print("=" * 66)
    print("4 runs, 3 customers, 2 models -> one attribution sink")
    print("=" * 66)
    for i, (customer, model, searches, tps) in enumerate(FLEET):
        result = await run_agent(
            SupportAgent(model, searches, tps),
            task=f"answer {customer}'s ticket",
            tools=TOOLS,
            policy=POLICY,
            sinks=(sink,),
            principal=Principal(kind="user", id=customer),
            correlation_id=f"{customer}-run{i}",
            budget=Budget(steps=200, tokens=30_000),  # the fleet-wide seatbelt
        )
        status = result.final_answer or result.error
        print(f"  {customer:<8} {model:<18} -> {status}")

    print()
    print("  CHARGEBACK (your rates x kernel-reported counts)")
    print(f"  {'customer':<10} {'model':<18} {'usd':>10}")
    total = 0.0
    for (who, model), usd in sorted(sink.spend.items()):
        total += usd
        print(f"  {who:<10} {model:<18} {usd:>10.4f}")
    print(f"  {'':<10} {'fleet total':<18} {total:>10.4f}")
    print()
    print("  Notice initech: its runaway run was STOPPED by Budget(tokens=30k)")
    print(f"  — and its partial spend ({sink.tokens['initech']:,} tokens) is still attributed,")
    print("  because the tokens were genuinely burned before the cap fired.")
    print()
    print("  The kernel never saw a dollar. run.started said WHO, step.usage")
    print("  said WHAT IT COST in tokens — the join and the prices are yours.")


if __name__ == "__main__":
    asyncio.run(main())
