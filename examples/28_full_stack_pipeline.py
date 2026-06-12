"""
================================================================
EXAMPLE 28 — "The full stack: every pillar in ONE run" (CAPSTONE)
================================================================

SCENARIO:
    Every other example isolates one feature. This one composes them all,
    because the whole point of Lynx is that policy, audit, durability,
    token metering, execution isolation, and handoff routing are views of
    ONE chokepoint — not six bolted-on systems:

        handoff graph   : triage (read-only) -> resolver (may refund)
        policy          : per-node permission boundaries + approval gate
        executor seam   : the signature check runs in a subprocess;
                          everything else inline (route_executor)
        token metering  : every step reports Usage; totals on the result
        durability      : the whole workflow journals to a RunStore;
                          re-running it replays with ZERO model calls
        audit           : every event from every layer lands in one sink

WHAT THIS EXAMPLE SHOWS:
    - One realistic refund pipeline wired with ALL of the above
    - A summary table at the end: events by kind, tokens spent, journal
      records written, and the proof that a re-run is free and safe

RUN WITH:
    python examples/28_full_stack_pipeline.py
"""

from __future__ import annotations

import asyncio
from collections import Counter

from lynx import (
    Budget,
    DuplicateRecord,
    FinalAnswer,
    GraphNode,
    Message,
    StepRecord,
    ToolCall,
    ToolSet,
    Usage,
    auto_approve,
    compile_graph,
    compile_policy,
    inline_executor,
    route_executor,
    run_graph,
    subprocess_executor,
    tool,
)

# ---------------------------------------------------------------------------
# Tools. Note verify_signature: CPU-ish and marked isolation="subprocess" —
# the route_executor below sends it to a fresh capped interpreter. It must
# be top-level (picklable) for that. The refund is irreversible -> approval.
# ---------------------------------------------------------------------------

REFUNDS: list[str] = []


@tool(reversible=True, scope=("crm:read",))
async def lookup_order(order_id: str) -> str:
    return f"order {order_id}: $89, delivered late, customer: ada@example.com"


@tool(reversible=True, scope=("compute:exec",), isolation="subprocess")
async def verify_signature(payload: str) -> str:
    total = 0
    for i in range(50_000):  # pretend-crypto, capped by the subprocess executor
        total = (total + i * i) % 99991
    return f"signature ok (checksum {total}) for {payload!r}"


@tool(reversible=False, scope=("payments:refund",))
async def issue_refund(order_id: str, amount: int) -> str:
    REFUNDS.append(order_id)
    return f"refunded ${amount} for order {order_id}"


TOOLS = ToolSet.from_functions(lookup_order, verify_signature, issue_refund)

READ_ONLY = compile_policy(
    """
version: 1
defaults: { on_no_match: allow, on_missing_shadow: allow }
rules:
  - id: triage-cannot-pay
    match: { declared.scope.contains: "payments:refund" }
    decision: deny
    reason: triage is read-only — route to the resolver
"""
)
RESOLVER = compile_policy(
    """
version: 1
defaults: { on_no_match: allow, on_missing_shadow: allow }
rules:
  - id: refunds-need-a-nod
    match: { tool: issue_refund }
    decision: approve_required
"""
)

GRAPH = compile_graph(
    """
start: triage
max_transitions: 4
edges:
  - from: triage
    when: { answer_matches: "(?i)refund justified" }
    to: resolver
  - from: triage
    to: done
  - from: resolver
    to: done
"""
)


# ---------------------------------------------------------------------------
# Scripted agents that report Usage — what real adapters do automatically.
# ---------------------------------------------------------------------------


def u(i: int, o: int) -> Usage:
    return Usage(input_tokens=i, output_tokens=o, model="claude-haiku-4-5")


class TriageAgent:
    async def step(self, conv: tuple[Message, ...]):
        text = " ".join(m.content for m in conv)
        if "order 778" not in text:
            return ToolCall("lookup_order", {"order_id": "778"}, call_id="c1", usage=u(900, 40))
        if "signature ok" not in text:
            return ToolCall(
                "verify_signature", {"payload": "claim-778"}, call_id="c2", usage=u(1300, 35)
            )
        return FinalAnswer(
            text="refund justified: delivered late, claim verified", usage=u(1700, 60)
        )


class ResolverAgent:
    async def step(self, conv: tuple[Message, ...]):
        text = " ".join(m.content for m in conv)
        if "refunded" not in text:
            return ToolCall(
                "issue_refund", {"order_id": "778", "amount": 89}, call_id="c3", usage=u(800, 30)
            )
        return FinalAnswer(text="refund of $89 issued and confirmed", usage=u(1100, 45))


class MemoryRunStore:  # the same ~12-line reference store as examples 24/27
    def __init__(self) -> None:
        self.records: dict[tuple[str, int], StepRecord] = {}

    async def append(self, record: StepRecord) -> None:
        key = (record.run_id, record.seq)
        if key in self.records:
            raise DuplicateRecord(f"{key} already journaled")
        self.records[key] = record

    async def load(self, run_id: str):
        return sorted(
            (r for (rid, _), r in self.records.items() if rid == run_id),
            key=lambda r: r.seq,
        )


def make_nodes() -> dict[str, GraphNode]:
    return {
        "triage": GraphNode(
            agent=TriageAgent(), tools=TOOLS, policy=READ_ONLY, budget=Budget(steps=10)
        ),
        "resolver": GraphNode(
            agent=ResolverAgent(),
            tools=TOOLS,
            policy=RESOLVER,
            budget=Budget(steps=6, output_tokens=5_000),
            on_approval=auto_approve(approver="oncall-finance"),
        ),
    }


async def main() -> None:
    events: Counter[str] = Counter()
    tokens = {"in": 0, "out": 0}

    async def observer(event):
        events[event.kind] += 1
        if event.kind == "step.usage":
            tokens["in"] += event.body["input_tokens"] or 0
            tokens["out"] += event.body["output_tokens"] or 0

    executor = route_executor(
        {
            None: inline_executor(),
            "subprocess": subprocess_executor(cpu_seconds=5, max_memory_mb=256, timeout_seconds=10),
        }
    )
    store = MemoryRunStore()

    print("=" * 66)
    print("One refund pipeline — policy + audit + durability + tokens + seam")
    print("=" * 66)
    result = await run_graph(
        make_nodes(),
        "Customer 778 demands a refund for a late delivery",
        router=GRAPH,
        executor=executor,
        sinks=(observer,),
        store=store,
        run_id="refund-778",
    )

    for o in result.path:
        print(f"  {o.node:<9} -> {o.result.final_answer}  [{o.denials} denial(s)]")
    print(f"  refunds issued: {len(REFUNDS)}")
    print()
    print("  What the chokepoint saw, all layers in one stream:")
    for kind in [
        "policy.evaluated",
        "action.denied",
        "approval.requested",
        "approval.granted",
        "action.completed",
        "step.usage",
        "graph.handoff",
    ]:
        print(f"    {kind:<20} x{events[kind]}")
    print(f"    tokens metered       {tokens['in']} in / {tokens['out']} out")
    print(f"    journal records      {sum(1 for _ in store.records)}")

    # ---- the durability payoff: a free, safe re-run ------------------------
    class WouldExplode:
        async def step(self, conv):
            raise AssertionError("re-run must not call any model")

    boom = {
        n: GraphNode(agent=WouldExplode(), tools=g.tools, policy=g.policy)
        for n, g in make_nodes().items()
    }
    again = await run_graph(
        boom,
        "Customer 778 demands a refund for a late delivery",
        router=GRAPH,
        executor=executor,
        store=store,
        run_id="refund-778",
    )
    print()
    print(f"  re-run    : {again.final.final_answer!r}")
    print(f"  refunds   : still {len(REFUNDS)} — replayed, not re-executed")
    print()
    print("  Six features, one configuration block, zero infrastructure —")
    print("  policy, audit, durability, metering, isolation, and routing are")
    print("  all views of the same propose -> decide -> execute chokepoint.")


if __name__ == "__main__":
    asyncio.run(main())
