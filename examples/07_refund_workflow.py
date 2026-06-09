"""
================================================================
EXAMPLE 07 — "Customer support, real-world rules" (ADVANCED)
================================================================

SCENARIO:
    Imagine a customer-support chatbot at a software company. Customers
    email asking for refunds. The bot needs to handle three different
    kinds of situations correctly:

      1. SMALL refunds ($5–$50) — Auto-process. Apologize, refund, move on.
      2. MEDIUM refunds ($50–$500) — PAUSE and ask a human supervisor.
      3. BIG refunds (over $500) — Always say "I'll escalate, please call us."
      4. KNOWN scammers — Block every request, no matter the amount.

    Without Lynx, you have to trust the LLM to apply these rules every
    single time, with perfect consistency, across millions of tickets.

    With Lynx, you write the rules ONCE in YAML and the policy engine
    applies them every single time. The LLM can be wrong about the rule;
    Lynx is not.

REAL-WORLD USE CASE:
    Any rules-based workflow:
      - Customer support refunds (this example)
      - Approving expense reports
      - Auto-replying to email tiers (FAQ vs sales vs escalation)
      - Resetting passwords (auto for retail; require approval for admins)
      - Cancelling orders (small auto, big approve, fraudulent deny)

WHAT THIS EXAMPLE SHOWS:
    - Multiple tools (`get_customer`, `refund_customer`) registered
    - A real-world YAML policy with priorities + predicates + multi-tier
      decisions
    - Three different customer scenarios in a single run:
        * C-789 (fraud watchlist) → DENY
        * C-456 (medium $200 refund) → APPROVE_REQUIRED
        * C-123 (small $1.63 refund) → ALLOW
    - The audit log captures all three; useful for SOC 2

RUN WITH:
    python examples/07_refund_workflow.py

WHAT YOU'LL SEE:
    Three separate runs, one per customer, with different verdicts.
    The audit log shows exactly who got what and why.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from lynx import FinalAnswer, Message, Runtime, ToolCall, tool
from lynx.core.mediator import get_registry
from lynx.policy import load_policy_file
from lynx.stores.sqlite import SQLiteStore

# ---------------------------------------------------------------------------
# Fake DB
# ---------------------------------------------------------------------------

CUSTOMERS = {
    "C-123": {"name": "Alice", "plan": "Pro", "monthly_usd": 49},
    "C-456": {"name": "Bob", "plan": "Team", "monthly_usd": 199},
    "C-789": {"name": "Carol", "plan": "Pro", "monthly_usd": 49},
}

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(cost="low", reversible=True, scope=["customer:read"])
async def get_customer(customer_id: str) -> dict:
    return CUSTOMERS.get(customer_id, {"error": "not found"})


@tool(cost="medium", reversible=False, scope=["customer:write", "money:transfer"])
async def refund_customer(customer_id: str, amount_usd: float, reason: str) -> dict:
    return {"refunded": amount_usd, "to": customer_id, "reason": reason, "txn": "TXN-XYZ"}


@refund_customer.shadow
async def _refund_shadow(customer_id: str, amount_usd: float, reason: str) -> dict:
    return {"would_refund": amount_usd, "to": customer_id, "note": "DRY RUN"}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class RefundAgent:
    """One scenario per agent instance: tries get_customer + refund_customer."""

    SCENARIOS = {
        "C-789": (5000.0, "customer demanded compensation"),  # fraud watchlist → DENY
        "C-456": (200.0, "month-long outage credit"),  # medium → APPROVE
        "C-123": (1.63, "1-day outage refund"),  # small → ALLOW
    }

    def __init__(self, customer_id: str):
        self.customer_id = customer_id
        amount, reason = self.SCENARIOS[customer_id]
        self._plan = [
            ToolCall("get_customer", {"customer_id": customer_id}, call_id="c1"),
            ToolCall(
                "refund_customer",
                {"customer_id": customer_id, "amount_usd": amount, "reason": reason},
                call_id="c2",
            ),
            FinalAnswer(text=f"Processed ticket for {customer_id}."),
        ]
        self._i = 0

    async def step(self, conversation: list[Message]):
        a = self._plan[self._i]
        self._i += 1
        return a


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run_one(runtime: Runtime, customer_id: str) -> None:
    name = CUSTOMERS[customer_id]["name"]
    print()
    print(f"=== Ticket for {customer_id} ({name}) ===")
    result = await runtime.run(
        agent=RefundAgent(customer_id),
        task=f"Process refund ticket for customer {customer_id}",
        principal={"kind": "service", "id": "support-bot"},
        environment="prod",
    )
    print(f"  run_id:           {result.run_id}")
    print(f"  status:           {result.status}")
    if result.paused_approval_id:
        print(f"  paused for:       lynx approve {result.paused_approval_id}")
    print(f"  final answer:     {result.final_answer}")
    print(f"  trace command:    lynx trace {result.run_id}")


async def main() -> None:
    import tempfile

    policy_path = Path(__file__).resolve().parent / "policies" / "refund.yaml"

    with tempfile.TemporaryDirectory() as tmp:
        runtime = Runtime(
            store=SQLiteStore(f"{tmp}/state.db"),
            policy=load_policy_file(policy_path),
        )

        for cid in ("C-789", "C-456", "C-123"):
            await run_one(runtime, cid)


if __name__ == "__main__":
    get_registry().clear()

    @tool(cost="low", reversible=True, scope=["customer:read"])
    async def get_customer(customer_id: str) -> dict:
        return CUSTOMERS.get(customer_id, {"error": "not found"})

    @tool(cost="medium", reversible=False, scope=["customer:write", "money:transfer"])
    async def refund_customer(customer_id: str, amount_usd: float, reason: str) -> dict:
        return {"refunded": amount_usd, "to": customer_id, "reason": reason, "txn": "TXN-XYZ"}

    @refund_customer.shadow
    async def _refund_shadow(customer_id: str, amount_usd: float, reason: str) -> dict:
        return {"would_refund": amount_usd, "to": customer_id, "note": "DRY RUN"}

    asyncio.run(main())
