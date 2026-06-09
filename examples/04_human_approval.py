"""
================================================================
EXAMPLE 04 — "Big decisions need a human" (MORE COMPLEX)
================================================================

SCENARIO:
    Some actions are too important for the assistant to do on its own —
    like wiring money out of your bank account, or sending a sensitive
    email, or deleting a customer's data.

    For these, you want a "pause and ask" rule:

        Assistant: "I'd like to wire $200 to ACME for last month's invoice."
        Lynx:      "That needs your OK first. Run stopped — waiting."
        You:       (Tap "Approve" on your phone)
        Lynx:      "Approved. Wiring now."
        Assistant: "Done. Wire confirmed."

    The assistant cannot move on its own; YOU make the call. Once approved,
    the run picks up exactly where it left off — no re-doing earlier work.

REAL-WORLD USE CASE:
    Human-in-the-loop AI:
      - Customer support refunds above a threshold
      - Code merges to a production branch
      - Sending messages to customers
      - Spending money or moving inventory
      - Anything that has a "are you sure?" feel in your head

WHAT THIS EXAMPLE SHOWS:
    - The APPROVE_REQUIRED verdict
    - The run PAUSING (status: paused)
    - Granting the approval via the runtime API
    - Resuming — the approved action then runs
    - The audit log records: who approved, when, and the action

RUN WITH:
    python examples/04_human_approval.py

WHAT YOU'LL SEE:
    First pass:
        Status: paused
        Paused approval: A-...
    (we then auto-grant the approval from code, like a Slack button)
    Second pass:
        Status: succeeded
        Final: Wire confirmed.
"""

from __future__ import annotations

import asyncio

from lynx import FinalAnswer, Message, Runtime, ToolCall, tool
from lynx.core.mediator import get_registry
from lynx.policy import compile_policy
from lynx.stores.sqlite import SQLiteStore

# ---------------------------------------------------------------------------
# Tool — pretend wire-transfer. In real life, hits Stripe or your bank API.
# ---------------------------------------------------------------------------


@tool(cost="high", reversible=False, scope=["money:transfer"])
async def wire_transfer(to: str, amount_usd: float, memo: str) -> dict:
    """Wire money. IRREVERSIBLE — policy will gate this."""
    return {
        "to": to,
        "amount_usd": amount_usd,
        "memo": memo,
        "confirmation": "WIRE-12345",
    }


@wire_transfer.shadow
async def _wire_transfer_preview(to: str, amount_usd: float, memo: str) -> dict:
    return {"would_wire": amount_usd, "to": to, "memo": memo}


# ---------------------------------------------------------------------------
# Policy — any wire transfer requires approval.
# ---------------------------------------------------------------------------


POLICY = """
version: 1
defaults:
  on_no_match: deny

rules:
  - id: wires-need-approval
    description: "Any wire transfer needs human sign-off"
    match:
      tool: wire_transfer
    decision: approve_required
    approvers: ["finance@acme.com"]
    timeout_seconds: 3600
"""


# ---------------------------------------------------------------------------
# Agent — conversation-aware (so it survives a resume restarting it fresh).
# ---------------------------------------------------------------------------


class WireAgent:
    """Proposes one wire, then finishes once the result lands."""

    async def step(self, conversation: list[Message]):
        # If we've already seen a wire_transfer result, the loop is done.
        for msg in conversation:
            if msg.role == "tool" and msg.name == "wire_transfer":
                return FinalAnswer(text="Wire confirmed; vendor paid.")
        return ToolCall(
            tool="wire_transfer",
            args={
                "to": "ACME Corp",
                "amount_usd": 200.00,
                "memo": "Invoice INV-2026-06-001",
            },
            call_id="c1",
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        runtime = Runtime(
            store=SQLiteStore(f"{tmp}/state.db"),
            policy=compile_policy(POLICY),
        )

        print("─" * 60)
        print("PASS 1 — start the run; expect it to PAUSE for approval")
        print("─" * 60)

        result = await runtime.run(
            agent=WireAgent(),
            task="Pay the ACME invoice.",
            principal={"kind": "service", "id": "accounts-payable-bot"},
        )

        print(f"Status:           {result.status}")
        print(f"Paused approval:  {result.paused_approval_id}")
        print()
        print("→ At this point, a supervisor would get a Slack ping or email.")
        print("  When they tap 'Approve', it hits runtime.approve(...).")
        print("  Then runtime.resume(...) picks up the run.")
        print()

        print("─" * 60)
        print("PASS 2 — grant the approval and resume")
        print("─" * 60)

        await runtime.approve(result.paused_approval_id, approver="alice@acme.com")
        print("Approval granted by alice@acme.com")

        result2 = await runtime.resume(WireAgent(), run_id=result.run_id)
        print(f"Status:  {result2.status}")
        print(f"Final:   {result2.final_answer}")

        print()
        print("Audit chain:")
        ok, err = runtime.verify_audit(result.run_id)
        print(f"  intact: {ok}  {err or ''}")


if __name__ == "__main__":
    get_registry().clear()

    @tool(cost="high", reversible=False, scope=["money:transfer"])
    async def wire_transfer(to: str, amount_usd: float, memo: str) -> dict:
        return {
            "to": to,
            "amount_usd": amount_usd,
            "memo": memo,
            "confirmation": "WIRE-12345",
        }

    @wire_transfer.shadow
    async def _wire_transfer_preview(to: str, amount_usd: float, memo: str) -> dict:
        return {"would_wire": amount_usd, "to": to, "memo": memo}

    asyncio.run(main())
