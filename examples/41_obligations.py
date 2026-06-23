"""
================================================================
EXAMPLE 41 — "Allow, *and also* do X" (OBLIGATIONS)
================================================================

SCENARIO:
    A verdict alone isn't always enough. Real authorization engines (XACML,
    AWS Cedar) return a decision *plus obligations* — mandatory side-actions
    that are part of enforcing the decision. "Allow the refund, BUT ONLY IF a
    short-lived credential was issued first, AND notify finance afterwards."

    Lynx attaches an `obligations:` block to any verdict. Each obligation names
    a handler `id` resolved against an `ObligationRegistry` you pass to
    `run_agent(..., obligations={...})` — the kernel ships NO handlers
    (mechanism, not policy). `phase` decides when it runs and what failure means:

        pre   runs BEFORE the action and GATES it — if the handler raises (or
              the id isn't registered), the action is DENIED and the tool never
              runs. Fail-closed.
        post  runs AFTER the action — a failure is flagged + audited but cannot
              un-execute the side effect. Best-effort, by physics.

    Three refunds are proposed:
      1. $500   — small: allow + a `post` notify (the tool runs, finance is told)
      2. $5,000 — large: a `pre` credential gate that SUCCEEDS, then the refund
                  runs, then the `post` notify fires
      3. $50,000 — large: the `pre` credential gate FAILS (over the vault's
                  single-issue cap) → the refund is DENIED and never executes

    Watch the audit stream for `obligation.required / fulfilled / failed`, and
    note that refund #3's tool body never prints — fail-closed in action.

WHAT THIS EXAMPLE SHOWS:
    - Attaching `pre` / `post` obligations to an `allow` rule in YAML.
    - The bare-string shorthand (`- notify-finance` == a `post` obligation).
    - An ObligationRegistry of plain async handlers you own.
    - A failing `pre` obligation denying the action (the tool never runs).
    - A `post` obligation firing after a successful action.

REQUIRES:
    pip install lynx-agent        # stdlib only — no extra deps

RUN WITH:
    python examples/41_obligations.py
"""

from __future__ import annotations

import asyncio

from lynx import (
    ActionRequest,
    ExecutionContext,
    FinalAnswer,
    Message,
    Obligation,
    ToolCall,
    ToolSet,
    auto_deny,
    compile_policy,
    run_agent,
    stdout_sink,
    tool,
)


@tool(reversible=False, scope=("payments:write",))
async def refund_customer(customer_id: str, amount_usd: int) -> str:
    """Issue a refund to a customer. Irreversible — real money moves."""
    print(f"   [tool] >>> refunded ${amount_usd} to {customer_id}")
    return f"refunded ${amount_usd} to {customer_id}"


# --- the policy: obligations ride on the `allow` verdict --------------------

POLICY = """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: large-refund-gated
    priority: 10
    match: { tool: refund_customer, args.amount_usd.gt: 1000 }
    decision: allow
    obligations:
      - { id: issue-ttl-credential, phase: pre,  params: { seconds: 300 } }
      - { id: notify-finance,       phase: post, params: { channel: "#finance" } }
  - id: small-refund
    match: { tool: refund_customer }
    decision: allow
    obligations:
      - notify-finance        # bare string == a `post` obligation, no params
"""


# --- the registry: your handlers, the kernel ships none ---------------------


async def issue_ttl_credential(ob: Obligation, req: ActionRequest, ctx: ExecutionContext) -> None:
    """`pre` handler: mint a short-lived credential before the refund runs.

    Raising here is the whole point — it DENIES the action, fail-closed.
    The bank's vault refuses to single-issue a credential above its cap.
    """
    amount = req.args["amount_usd"]
    seconds = ob.params.get("seconds", 300)
    VAULT_SINGLE_ISSUE_CAP = 9_000
    if amount > VAULT_SINGLE_ISSUE_CAP:
        raise RuntimeError(
            f"credential vault refused: ${amount} exceeds the "
            f"${VAULT_SINGLE_ISSUE_CAP} single-issue cap"
        )
    print(f"   [pre obligation] issued a {seconds}s credential for ${amount}")


async def notify_finance(ob: Obligation, req: ActionRequest, ctx: ExecutionContext) -> None:
    """`post` handler: tell finance after the refund has happened."""
    channel = ob.params.get("channel", "#finance")
    print(
        f"   [post obligation] notified {channel}: {req.args['customer_id']} ${req.args['amount_usd']}"
    )


OBLIGATION_REGISTRY = {
    "issue-ttl-credential": issue_ttl_credential,
    "notify-finance": notify_finance,
}


class RefundAgent:
    """Proposes three refunds, then finishes."""

    def __init__(self) -> None:
        self._i = 0
        self._plan: list[ToolCall | FinalAnswer] = [
            ToolCall(
                tool="refund_customer",
                args={"customer_id": "C-100", "amount_usd": 500},
                call_id="r1",
            ),
            ToolCall(
                tool="refund_customer",
                args={"customer_id": "C-200", "amount_usd": 5_000},
                call_id="r2",
            ),
            ToolCall(
                tool="refund_customer",
                args={"customer_id": "C-300", "amount_usd": 50_000},
                call_id="r3",
            ),
            FinalAnswer(
                text="Processed the queue: $500 and $5,000 refunded; "
                "the $50,000 refund was blocked because its pre-obligation "
                "(credential issuance) failed."
            ),
        ]

    async def step(self, conv: tuple[Message, ...]) -> ToolCall | FinalAnswer:
        action = self._plan[self._i]
        self._i += 1
        return action


async def main() -> None:
    result = await run_agent(
        RefundAgent(),
        task="Process the refund queue",
        tools=ToolSet.from_functions(refund_customer),
        policy=compile_policy(POLICY),
        sinks=(stdout_sink(),),
        on_approval=auto_deny("approvals not configured for this demo"),
        obligations=OBLIGATION_REGISTRY,
    )
    print()
    print(f"Final: {result.final_answer}")


if __name__ == "__main__":
    asyncio.run(main())
