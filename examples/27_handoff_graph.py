"""
================================================================
EXAMPLE 27 — "Handoff graph: the edge is a permission boundary" (ADVANCED)
================================================================

SCENARIO:
    Multi-agent setups usually fail two ways: the orchestrator agent
    bypasses its role and does the work itself (tool bleed), and handoffs
    lose context. Lynx's handoff graph is a finite **state machine** over
    agents (nodes are states, edges are guarded transitions, `done` is the
    terminal state) and fixes both structurally:

      - each node is one complete run_agent() call with ITS OWN policy,
        tools, and budget — the triage node CANNOT write even if its
        model tries (enforced, not prompted)
      - context passing is explicit: the next node's task carries the
        previous node's result, clearly marked
      - edges are pure predicates over outcomes — including DENIAL COUNTS,
        a routing signal only possible because policy is first-class
      - mandatory max_transitions: unbounded recursion is impossible

    And it's all OPTIONAL — a node is just a run_agent() call, so this
    module is declarative sugar over a loop you could write yourself.

WHAT THIS EXAMPLE SHOWS:
    - Act 1: the killer pattern — triage (read-only) → fixer (write) →
      reviewer (read-only), looping fixer↔reviewer until approved, with
      YAML-declared edges (compile_graph) and per-node denial counts
    - Act 2: Python first — a plain function as the Router, no YAML
    - Act 3: `denials_gt` routing — escalate to a privileged node BECAUSE
      policy kept denying the worker (plus a per-node Budget)
    - Act 4: durable workflows — re-run the whole graph against the same
      RunStore with agents that would crash if stepped: zero model calls

RUN WITH:
    python examples/27_handoff_graph.py
"""

from __future__ import annotations

import asyncio

from lynx import (
    Budget,
    DuplicateRecord,
    FinalAnswer,
    GraphNode,
    Message,
    NodeOutcome,
    StepRecord,
    ToolCall,
    ToolSet,
    compile_graph,
    compile_policy,
    run_graph,
    tool,
)

# ---------------------------------------------------------------------------
# Tools — one read, one write. Which node may use which is POLICY, not vibes.
# ---------------------------------------------------------------------------


@tool(reversible=True, scope=("fs:read",))
async def read_file(path: str) -> str:
    return f"def login(user):  # TODO: timing-unsafe compare in {path}"


@tool(reversible=True, scope=("fs:write",))
async def patch_file(path: str, change: str) -> str:
    return f"patched {path}: {change}"


TOOLS = ToolSet.from_functions(read_file, patch_file)

READ_ONLY = compile_policy(
    """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: no-writes-here
    match: { declared.scope.contains: "fs:write" }
    decision: deny
    reason: this node is read-only — hand off to the fixer
"""
)
CAN_WRITE = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []")


# ---------------------------------------------------------------------------
# Scripted "models" so the demo is deterministic and offline. Note the triage
# model TRIES to patch — and gets denied, because its node is read-only.
# ---------------------------------------------------------------------------


class TriageAgent:
    async def step(self, conv: tuple[Message, ...]):
        text = " ".join(m.content for m in conv)
        if "timing-unsafe" not in text:
            return ToolCall(tool="read_file", args={"path": "auth.py"}, call_id="c1")
        if "[denied]" not in text:
            # The model overreaches — it tries to fix it itself.
            return ToolCall(
                tool="patch_file", args={"path": "auth.py", "change": "hmac"}, call_id="c2"
            )
        return FinalAnswer(text="needs fix: timing-unsafe compare in auth.py")


class FixerAgent:
    async def step(self, conv: tuple[Message, ...]):
        text = " ".join(m.content for m in conv)
        if "patched" not in text:
            return ToolCall(
                tool="patch_file",
                args={"path": "auth.py", "change": "use hmac.compare_digest"},
                call_id="c1",
            )
        return FinalAnswer(text="patched auth.py with hmac.compare_digest")


class ReviewerAgent:
    def __init__(self) -> None:
        self.visits = 0

    async def step(self, conv: tuple[Message, ...]):
        self.visits += 1
        # First review rejects (sends it back to the fixer); second approves.
        if self.visits == 1:
            return FinalAnswer(text="rejected: missing constant-time note in docstring")
        return FinalAnswer(text="approved: patch is correct and documented")


# ---------------------------------------------------------------------------
# The graph — reviewable YAML. First matching edge wins; cycles are fine;
# max_transitions makes runaway loops impossible by construction.
# ---------------------------------------------------------------------------

GRAPH = compile_graph(
    """
version: 1
start: triage
max_transitions: 8
edges:
  - from: triage
    when: { status: succeeded, answer_matches: "(?i)needs fix" }
    to: fixer
  - from: triage
    to: done
  - from: fixer
    to: reviewer
  - from: reviewer
    when: { answer_matches: "(?i)approved" }
    to: done
  - from: reviewer
    to: fixer
"""
)


def make_nodes() -> dict[str, GraphNode]:
    """Fresh agents per run — graph nodes are stateless run_agent calls."""
    return {
        "triage": GraphNode(agent=TriageAgent(), tools=TOOLS, policy=READ_ONLY),
        "fixer": GraphNode(agent=FixerAgent(), tools=TOOLS, policy=CAN_WRITE),
        "reviewer": GraphNode(agent=ReviewerAgent(), tools=TOOLS, policy=READ_ONLY),
    }


def show(result) -> None:
    for o in result.path:
        outcome = o.result.final_answer or o.result.error
        denials = f"  [{o.denials} denial(s)]" if o.denials else ""
        print(f"  hop {o.transitions}: {o.node:<10} -> {outcome}{denials}")
    print(f"  final: {result.final.final_answer}  (error={result.error})")


async def main() -> None:
    # ---- Act 1: the YAML review loop ---------------------------------------
    print("=" * 66)
    print("Act 1 — triage (read-only) -> fixer (write) <-> reviewer loop")
    print("=" * 66)
    result = await run_graph(make_nodes(), "Fix the security bug in auth.py", router=GRAPH)
    show(result)
    print()
    print("  Notice hop 0: the triage MODEL tried to patch the file itself —")
    print("  its node's policy denied it (that's the [1 denial(s)]), so it")
    print("  handed off instead. Role boundaries enforced, not prompted.")

    # ---- Act 2: Python-first — any function is a Router --------------------
    print()
    print("=" * 66)
    print("Act 2 — no YAML needed: a plain function is a Router")
    print("=" * 66)

    def my_router(o: NodeOutcome) -> str | None:
        if o.node == "triage" and "needs fix" in (o.result.final_answer or ""):
            return "fixer"
        if o.node == "fixer":
            return "reviewer"
        if o.node == "reviewer" and "approved" not in (o.result.final_answer or ""):
            return "fixer"
        return None  # terminal

    result = await run_graph(
        make_nodes(), "Fix the security bug in auth.py", router=my_router, start="triage"
    )
    show(result)

    # ---- Act 3: DENIAL COUNTS as a routing signal ---------------------------
    print()
    print("=" * 66)
    print("Act 3 — escalate by denial count (policy as a routing signal)")
    print("=" * 66)
    escalation = compile_graph(
        """
start: worker
max_transitions: 4
edges:
  - from: worker
    when: { denials_gt: 0 }
    to: privileged
  - from: worker
    to: done
"""
    )
    nodes = {
        # The worker is read-only AND on a tight per-node budget: its patch
        # attempts get denied until the budget stops it...
        "worker": GraphNode(
            agent=FixerAgent(), tools=TOOLS, policy=READ_ONLY, budget=Budget(steps=2)
        ),
        # ...so the graph escalates to the node that's allowed to write.
        "privileged": GraphNode(agent=FixerAgent(), tools=TOOLS, policy=CAN_WRITE),
    }
    result = await run_graph(nodes, "Patch auth.py", router=escalation)
    show(result)
    print("  `denials_gt` is routing on POLICY outcomes — only possible")
    print("  because the permission boundary is part of the graph. The worker")
    print("  also had its own per-node Budget(steps=2): boundaries all the way.")

    # ---- Act 4: durable workflows — crash-resume the WHOLE graph -----------
    print()
    print("=" * 66)
    print("Act 4 — resume a whole workflow: zero model calls on re-run")
    print("=" * 66)

    class MemoryRunStore:  # the same ~12-line reference store as example 24
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

    store = MemoryRunStore()
    first = await run_graph(
        make_nodes(),
        "Fix the security bug in auth.py",
        router=GRAPH,
        store=store,
        run_id="ticket-4711",
    )
    print(f"  first run : {len(first.path)} node runs -> {first.final.final_answer!r}")

    class WouldExplode:  # proves nothing is re-executed on resume
        async def step(self, conv):
            raise AssertionError("resume must NOT re-call any model")

    boom = {
        name: GraphNode(agent=WouldExplode(), tools=n.tools, policy=n.policy)
        for name, n in make_nodes().items()
    }
    second = await run_graph(
        boom,
        "Fix the security bug in auth.py",
        router=GRAPH,
        store=store,
        run_id="ticket-4711",
    )
    print(f"  re-run    : {len(second.path)} node runs -> {second.final.final_answer!r}")
    print("  every node replayed from the journal; routing decisions replayed")
    print("  from journaled handoff records — no model calls, no side effects.")


if __name__ == "__main__":
    asyncio.run(main())
