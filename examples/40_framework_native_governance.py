"""
================================================================
EXAMPLE 40 — "Govern an agent framework's own tool calls" (INTEGRATION)
================================================================

SCENARIO:
    Lynx's adapters (lynx.adapters) wrap an LLM so LYNX drives the loop. But
    most teams already run their agent inside a framework — the OpenAI Agents
    SDK, LangChain, CrewAI, PydanticAI — that owns the loop and calls plain
    Python tool functions. You don't want to rewrite that. You want to drop a
    governance boundary in front of every tool call the framework makes, with
    no proxy and no rewrite.

    `ToolGuard` is that boundary. Construct it once with the same inputs you'd
    give `run_agent` (tools, policy, principal, approval handler, executor,
    sinks). Then, from inside the framework's native tool hook, call
    `guard.check(tool_name, args)` — it runs Lynx's pure PDP (`evaluate`) and
    enforces the verdict (`mediate`), returning a `GovernedCall`. All five
    verdicts work, identically to `run_agent`, because it reuses the same kernel.

WHAT THIS EXAMPLE SHOWS:
    - One `ToolGuard` governing simulated framework tool calls.
    - All five verdicts at the boundary: allow / deny / dry_run / transform /
      approve_required — plus a fail-closed unknown tool.
    - The one-liner that turns a ToolSet into governed OpenAI Agents SDK tools.

REQUIRES:
    pip install lynx-agent                 # ToolGuard core — stdlib only
    pip install lynx-agent[openai-agents]  # only for the real SDK wiring (bottom)

RUN WITH:
    python examples/40_framework_native_governance.py
"""

from __future__ import annotations

import asyncio

from lynx import Principal, ToolGuard, ToolSet, compile_policy, shadow, tool

# --- the team's existing plain tool functions -------------------------------


@tool(reversible=True, scope=("compute:read",))
async def search(query: str) -> str:
    return f"results for {query!r}"


@tool(reversible=False, scope=("payments:write",))
async def refund(amount: int) -> str:
    return f"refunded {amount}"


@shadow(refund)
async def _refund_shadow(amount: int) -> str:
    return f"WOULD refund {amount}"


TOOLS = ToolSet.from_functions(search, refund)

# A policy a developer owns — reads flow, refunds are capped, big refunds need
# a human, everything unknown is denied (fail-closed).
POLICY = compile_policy(
    """
version: 1
defaults: {on_no_match: deny}
rules:
  - {id: allow-search, match: {tool: search}, decision: allow}
  - id: cap-small-refund
    match: {all_of: [{tool: refund}, {args.amount.le: 100}]}
    decision: transform
    transform: {jsonpath: "$.args.amount", set: 1}
  - {id: gate-big-refund, match: {tool: refund}, decision: approve_required}
"""
)


async def main() -> None:
    async def approver(_req: object) -> object:
        from lynx import ApprovalDecision

        return ApprovalDecision(granted=True, approver="finance-oncall")

    guard = ToolGuard(
        tools=TOOLS,
        policy=POLICY,
        principal=Principal(kind="user", id="agent-7"),
        on_approval=approver,
    )

    # Simulate the tool calls a framework's agent loop would make. In a real
    # integration these come from the framework's native hook, not a list.
    proposed = [
        ("search", {"query": "open tickets"}),  # ALLOW
        ("refund", {"amount": 50}),  # TRANSFORM -> capped to 1
        ("refund", {"amount": 5000}),  # APPROVE_REQUIRED -> granted
        ("delete_account", {"id": 1}),  # unknown -> DENY (fail-closed)
    ]

    for name, args in proposed:
        call = await guard.check(name, args)
        outcome = call.result.value if call.allowed else call.result.error
        print(f"{name:<16} {args!s:<26} -> {call.decision.verdict.value:<16} {outcome}")

    print(
        "\nSame ToolGuard, four framework tool calls, five-verdict governance at the\n"
        "boundary — the framework still drives the loop; Lynx governs every call in it."
    )

    # --- Wiring into the real OpenAI Agents SDK (needs the extra) -----------
    # from agents import Agent, Runner
    # from lynx.integrations.openai_agents import governed_function_tools
    #
    # agent = Agent(
    #     name="support",
    #     instructions="Help the user; use tools.",
    #     tools=governed_function_tools(TOOLS, policy=POLICY, on_approval=approver),
    # )
    # result = await Runner.run(agent, "refund order 123 for $5000")
    # Each tool the SDK calls is routed through the same ToolGuard above:
    # denied calls come back to the model as "[denied] ..."; previews and
    # transformed args are applied before the real function ever runs.


if __name__ == "__main__":
    asyncio.run(main())
