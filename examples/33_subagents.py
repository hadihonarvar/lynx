"""
================================================================
EXAMPLE 33 — "Subagents: a tool that runs an agent" (ADVANCED)
================================================================

SCENARIO:
    A subagent is NOT a kernel feature — it's the run-inside-run pattern, on
    stock Lynx with no kernel change. A "lead" agent delegates sub-tasks to a
    "worker" agent by calling a @tool whose body calls run_agent().

    The win is that every Lynx guarantee composes:
      - The spawn is gated by the LEAD's policy (scope "agent:spawn") — you
        can deny / approve_required / rate-limit who spawns what.
      - The worker gets its OWN policy, tools, and budget — a real permission
        boundary, invoked dynamically by the model (not a static graph edge).
      - The lead's CancelToken is passed into the worker, so killing the lead
        kills the whole subtree.

WHAT THIS EXAMPLE SHOWS:
    - subagent-as-tool: `research(topic)` -> run_agent(worker, topic, ...)
    - SEQUENTIAL spawning (the lead calls research twice, one per step)
    - a PARALLEL variant (`research_all`) using asyncio.gather
    - the audit tree: each run has its own correlation_id (lead vs workers)

ORDER OF EXECUTION:
    The lead's loop is sequential — one tool call per step. Whether a spawn
    tool runs its children one-by-one or all-at-once is decided by the tool
    body: `await` = sequential, `asyncio.gather` = parallel. The kernel never
    parallelizes for you.

RUN WITH:
    python examples/33_subagents.py
"""

from __future__ import annotations

import asyncio

from lynx import (
    Budget,
    CancelToken,
    FinalAnswer,
    Message,
    ToolCall,
    ToolSet,
    compile_policy,
    run_agent,
    tool,
)

# The lead may spawn; everything else is allowed. In production this is where
# you'd gate spawning (approve_required, a per-run cap, deny in prod, ...).
LEAD_POLICY = """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: spawning-is-allowed
    match: { tool: research }
    decision: allow
    reason: lead may delegate research
"""

# The worker is a TIGHTER boundary: it can read, nothing else. Its own policy,
# enforced independently of the lead's.
WORKER_POLICY = "version: 1\ndefaults: { on_no_match: allow }\nrules: []"


# ---------------------------------------------------------------------------
# Worker side: one read-only tool + a deterministic worker agent.
# ---------------------------------------------------------------------------


@tool(reversible=True, scope=("compute:read",))
async def web_search(q: str) -> str:
    """A stand-in knowledge source (no network)."""
    facts = {
        "climate policy": "carbon pricing now in 40+ jurisdictions",
        "ev adoption": "EVs are ~18% of new-car sales globally",
    }
    return facts.get(q.lower().strip(), f"(no data for {q!r})")


class WorkerAgent:
    """Deterministic, a pure function of the conversation: search once, then
    summarize. Being conversation-pure makes it safe to reuse and to run many
    copies in parallel."""

    async def step(self, conv: tuple[Message, ...]) -> ToolCall | FinalAnswer:
        results = [m.content for m in conv if m.role == "tool"]
        if not results:
            topic = conv[0].content
            return ToolCall(tool="web_search", args={"q": topic}, call_id="s1")
        # tool results arrive tagged "[ok] ..."; hand back just the fact.
        return FinalAnswer(text=results[-1].removeprefix("[ok] "))


# ---------------------------------------------------------------------------
# The spawn tools — a @tool whose body calls run_agent(). This is the entire
# "subagent" mechanism. `cancel` is captured so the lead's kill-switch reaches
# the children.
# ---------------------------------------------------------------------------


def make_spawn_tools(cancel: CancelToken, sinks: tuple) -> tuple:
    worker_policy = compile_policy(WORKER_POLICY)
    worker_tools = ToolSet.from_functions(web_search)

    async def _run_worker(topic: str) -> str:
        result = await run_agent(
            WorkerAgent(),
            task=topic,
            tools=worker_tools,
            policy=worker_policy,
            budget=Budget(steps=3),  # the worker's OWN cap
            cancel=cancel,  # kill the lead -> kills the worker
            sinks=sinks,  # worker events join the same audit stream
        )
        return result.final_answer or f"[error] {result.error}"

    @tool(name="research", reversible=True, scope=("agent:spawn",))
    async def research(topic: str) -> str:
        """SEQUENTIAL: delegate one sub-task and wait for it."""
        return await _run_worker(topic)

    @tool(name="research_all", reversible=True, scope=("agent:spawn",))
    async def research_all(topics: list[str]) -> str:
        """PARALLEL: fan out independent sub-tasks with asyncio.gather."""
        answers = await asyncio.gather(*(_run_worker(t) for t in topics))
        return " | ".join(answers)

    return research, research_all


# ---------------------------------------------------------------------------
# Lead agent: deterministic. Researches each topic in its own step, then
# summarizes — so you see the lead loop stay sequential while it delegates.
# ---------------------------------------------------------------------------


class LeadAgent:
    TOPICS = ("climate policy", "ev adoption")

    async def step(self, conv: tuple[Message, ...]) -> ToolCall | FinalAnswer:
        gathered = [m.content for m in conv if m.role == "tool"]
        n = len(gathered)
        if n < len(self.TOPICS):
            return ToolCall(tool="research", args={"topic": self.TOPICS[n]}, call_id=f"r{n}")
        return FinalAnswer(text="LEAD SUMMARY → " + " ;; ".join(gathered))


async def main() -> None:
    cancel = CancelToken()

    # A sink that makes the run tree visible: each run_agent call (lead and
    # every worker) has its own correlation_id.
    seen: dict[str, str] = {}

    async def sink(ev) -> None:
        if ev.kind in ("run.started", "run.succeeded"):
            who = seen.setdefault(ev.correlation_id, f"run#{len(seen) + 1}")
            label = ev.body.get("task") or ev.body.get("final_answer", "")
            print(f"  [{who} {ev.correlation_id[:8]}] {ev.kind:<13} {label}")

    research, research_all = make_spawn_tools(cancel, (sink,))
    lead_tools = ToolSet.from_functions(research, research_all)
    lead_policy = compile_policy(LEAD_POLICY)

    print("=" * 64)
    print("Sequential: lead spawns one worker per step")
    print("=" * 64)
    result = await run_agent(
        LeadAgent(),
        task="brief me on two topics",
        tools=lead_tools,
        policy=lead_policy,
        sinks=(sink,),
    )
    print(f"\n  final: {result.final_answer}\n")

    print("=" * 64)
    print("Parallel: one tool call fans out to many workers (asyncio.gather)")
    print("=" * 64)

    class ParallelLead:
        async def step(self, conv):
            if any(m.role == "tool" for m in conv):
                return FinalAnswer(text=[m.content for m in conv if m.role == "tool"][-1])
            return ToolCall(
                tool="research_all",
                args={"topics": ["climate policy", "ev adoption"]},
                call_id="p1",
            )

    seen.clear()
    result = await run_agent(
        ParallelLead(),
        task="brief me, in parallel",
        tools=lead_tools,
        policy=lead_policy,
        sinks=(sink,),
    )
    print(f"\n  final: {result.final_answer}")


if __name__ == "__main__":
    asyncio.run(main())
