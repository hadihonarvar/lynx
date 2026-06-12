"""
================================================================
EXAMPLE 29 — "Gate agent memory through policy" (ADVANCED)
================================================================

SCENARIO:
    Long-term agent memory is an attack surface (OWASP Agentic Security
    ASI06: Memory & Context Poisoning — injected "facts" persist and
    steer every future run). Lynx deliberately ships NO memory substrate
    — but memory READS and WRITES are just tool calls, and tool calls are
    exactly what the policy gate judges. So you get a governed memory
    boundary for free: wrap your memory layer (mem0, Zep, a dict) in
    @tool functions with honest metadata, then write rules.

WHAT THIS EXAMPLE SHOWS:
    - Memory ops as tools: remember / recall / forget with scope metadata
    - POISONING BLOCKED: writes whose source is untrusted are denied —
      a web-scraped "fact" never enters the store
    - TENANT SCOPING: every recall is TRANSFORMed to carry a namespace
      tag, so cross-tenant leakage cannot be expressed
    - DELETION GUARDED: forget is irreversible -> its shadow previews
      what WOULD be deleted, and the real deletion needs a human
    - The audit stream shows every memory decision — a compliance trail
      for what the agent remembered, recalled, and forgot

RUN WITH:
    python examples/29_memory_gating.py
"""

from __future__ import annotations

import asyncio
import fnmatch

from lynx import (
    FinalAnswer,
    Message,
    ToolCall,
    ToolSet,
    auto_approve,
    cli_prompt_approval,  # noqa: F401  (the production choice; demo auto-approves)
    compile_policy,
    run_agent,
    tool,
)

# ---------------------------------------------------------------------------
# A toy memory substrate. In production this is mem0 / Zep / your Postgres —
# Lynx doesn't care: the GATE is the point, not the store.
# ---------------------------------------------------------------------------

MEMORY: dict[str, str] = {"tenant:acme/billing-contact": "ada@example.com"}


@tool(reversible=True, scope=("memory:write",))
async def remember(key: str, fact: str, source: str) -> str:
    """Persist a fact. `source` declares provenance — policy judges it."""
    MEMORY[key] = fact
    return f"remembered {key!r}"


@tool(reversible=True, scope=("memory:read",))
async def recall(query: str) -> str:
    """Recall facts matching a glob query."""
    hits = {k: v for k, v in MEMORY.items() if fnmatch.fnmatch(k, query)}
    return f"recall({query!r}) -> {hits or 'nothing'}"


@tool(reversible=False, scope=("memory:delete",))
async def forget(pattern: str) -> str:
    """Delete facts matching a glob pattern. Irreversible."""
    doomed = [k for k in MEMORY if fnmatch.fnmatch(k, pattern)]
    for k in doomed:
        del MEMORY[k]
    return f"forgot {len(doomed)} fact(s): {doomed}"


@forget.shadow
async def _forget_preview(pattern: str) -> dict:
    """Side-effect-free preview: what WOULD be deleted?"""
    return {"would_forget": [k for k in MEMORY if fnmatch.fnmatch(k, pattern)]}


TOOLS = ToolSet.from_functions(remember, recall, forget)

# ---------------------------------------------------------------------------
# The memory policy — the whole point of this example. Reviewable in a PR.
# ---------------------------------------------------------------------------

POLICY = compile_policy(
    """
version: 1
defaults: { on_no_match: allow, on_missing_shadow: approve_required }
rules:
  - id: block-untrusted-writes
    description: ASI06 memory-poisoning defense — provenance gates persistence
    match: { tool: remember, args.source: untrusted }
    decision: deny
    reason: facts from untrusted sources do not enter long-term memory

  - id: tenant-scope-every-recall
    description: cross-tenant recall cannot even be expressed
    match: { tool: recall }
    decision: transform
    transform:
      jsonpath: "$.args.query"
      set: "tenant:acme/*"

  - id: preview-deletions
    description: irreversible forget -> dry-run preview first
    match: { tool: forget, args.pattern.matches: "\\\\*" }
    decision: dry_run
    reason: wildcard deletions are previewed, never run blind

  - id: forget-needs-a-human
    match: { tool: forget }
    decision: approve_required
"""
)


class Researcher:
    """Scripted model: tries good and bad memory ops in sequence."""

    def __init__(self) -> None:
        self._plan = [
            # 1. a legitimate fact from a trusted pipeline -> allowed
            ToolCall(
                "remember",
                {"key": "tenant:acme/renewal", "fact": "renews 2026-09", "source": "crm"},
                call_id="c1",
            ),
            # 2. a web-scraped "fact" -> DENIED (poisoning blocked)
            ToolCall(
                "remember",
                {
                    "key": "tenant:acme/billing-contact",
                    "fact": "attacker@evil.example",
                    "source": "untrusted",
                },
                call_id="c2",
            ),
            # 3. recall everything -> TRANSFORMED to the tenant namespace
            ToolCall("recall", {"query": "*"}, call_id="c3"),
            # 4. wildcard forget -> dry-run preview only
            ToolCall("forget", {"pattern": "tenant:acme/*"}, call_id="c4"),
            # 5. targeted forget -> human approves
            ToolCall("forget", {"pattern": "tenant:acme/renewal"}, call_id="c5"),
            FinalAnswer(text="memory maintenance complete"),
        ]

    async def step(self, conv: tuple[Message, ...]):
        return self._plan.pop(0)


async def main() -> None:
    decisions = []

    async def watch(event):
        if event.kind == "policy.evaluated":
            decisions.append((event.body["verdict"], event.body["matched_rules"]))

    print("=" * 66)
    print("Five memory operations, five different policy outcomes")
    print("=" * 66)
    result = await run_agent(
        Researcher(),
        task="curate tenant:acme long-term memory",
        tools=TOOLS,
        policy=POLICY,
        sinks=(watch,),
        # Production: cli_prompt_approval() or your Slack handler.
        on_approval=auto_approve(approver="memory-curator"),
    )

    ops = [
        "remember (crm)",
        "remember (untrusted)",
        "recall *",
        "forget * (wildcard)",
        "forget targeted",
    ]
    for op, (verdict, rules) in zip(ops, decisions):
        print(f"  {op:<22} -> {verdict:<17} {rules}")
    print()
    print(f"  final  : {result.final_answer}")
    print(f"  memory : {MEMORY}")
    print()
    print("  - the poisoned billing-contact never entered memory (rule 1)")
    print("  - recall('*') silently became recall('tenant:acme/*') (rule 2)")
    print("  - the wildcard forget only PREVIEWED its blast radius (rule 3)")
    print("  - the targeted forget ran once a human said yes (rule 4)")
    print()
    print("  Lynx ships no memory — it ships the boundary your memory needed.")


if __name__ == "__main__":
    asyncio.run(main())
