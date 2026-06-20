"""
================================================================
EXAMPLE 06 — "Stream every event to a jsonl file" (MORE COMPLEX)
================================================================

SCENARIO:
    Lynx has no built-in audit storage. Instead, every event is
    streamed to whatever sinks you provide. Here we show:
      - stdout_sink: pretty-print for humans
      - jsonl_sink: one JSON record per event for machine processing
      - multi_sink: fan out to both

    The jsonl file is what compliance auditors get. Lynx never opens it;
    you do.

RUN WITH:
    python examples/06_streaming_to_jsonl.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from lynx import (
    FinalAnswer,
    Message,
    ToolCall,
    ToolSet,
    auto_deny,
    compile_policy,
    jsonl_sink,
    multi_sink,
    run_agent,
    stdout_sink,
    tool,
)


@tool(reversible=True, scope=("customer:read",))
async def get_customer(customer_id: str) -> dict:
    return {"id": customer_id, "name": "Alice", "balance_usd": 250}


@tool(reversible=False, scope=("money:transfer",))
async def issue_refund(customer_id: str, amount_usd: float) -> dict:
    return {"txn": "REF-001", "to": customer_id, "amount_usd": amount_usd}


@issue_refund.shadow
async def _refund_shadow(customer_id: str, amount_usd: float) -> dict:
    return {"would_refund": amount_usd, "to": customer_id}


POLICY = """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: small-refunds-only
    match: { tool: issue_refund, args.amount_usd.le: 50 }
    decision: allow
"""


class WorkflowAgent:
    def __init__(self):
        self._i = 0
        self._plan = [
            ToolCall("get_customer", {"customer_id": "C-1"}, call_id="c1"),
            ToolCall("issue_refund", {"customer_id": "C-1", "amount_usd": 12.50}, call_id="c2"),
            FinalAnswer(text="Processed ticket."),
        ]

    async def step(self, conv: tuple[Message, ...]):
        a = self._plan[self._i]
        self._i += 1
        return a


async def main() -> None:
    audit_path = Path("audit.jsonl")
    with audit_path.open("w") as audit_file:
        sink = multi_sink(stdout_sink(), jsonl_sink(audit_file))
        result = await run_agent(
            WorkflowAgent(),
            task="Process C-1 ticket",
            tools=ToolSet.from_functions(get_customer, issue_refund),
            policy=compile_policy(POLICY),
            sinks=(sink,),
            on_approval=auto_deny("not configured"),
        )

    print()
    print(f"Final: {result.final_answer}")
    print()
    print(f"Wrote {audit_path.stat().st_size} bytes of events to {audit_path}")
    print("First two events:")
    with audit_path.open() as f:
        for line in f.readlines()[:2]:
            d = json.loads(line)
            print(f"  seq={d['seq']:>2}  kind={d['kind']:<22}  body={d['body']}")

    # Clean up the demo file
    audit_path.unlink()
    print()
    print("(audit.jsonl cleaned up for the demo; in production you'd archive it)")


if __name__ == "__main__":
    asyncio.run(main())
