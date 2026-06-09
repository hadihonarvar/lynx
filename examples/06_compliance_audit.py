"""
================================================================
EXAMPLE 06 — "Paper trail for the auditors" (MORE COMPLEX)
================================================================

SCENARIO:
    Some industries are required BY LAW to keep records of every action
    an automated system took. Banks, hospitals, governments. If something
    goes wrong, an auditor needs to be able to say "show me what happened
    and prove nobody touched these records afterward."

    Lynx writes one of these recordings AUTOMATICALLY:
      - Every action proposed
      - Every policy decision
      - Every approval (who said yes, when)
      - Every result
      - Every step is hash-chained: if anyone edits even one character of
        a past record, the chain breaks and we can detect the tampering

    Think of it as a flight recorder for your AI agent. Sealed, time-stamped,
    cryptographically signed-ish.

REAL-WORLD USE CASE:
    Regulated industries:
      - SOC 2 Type II audits
      - HIPAA-compliant healthcare AI
      - PCI-DSS for payment systems
      - EU AI Act compliance
      - Internal security audits

    Also useful for ANY production agent — "show me what happened in this
    incident" is something you need for any post-mortem.

WHAT THIS EXAMPLE SHOWS:
    - Running a small workflow (the agent does a few things)
    - Walking the audit chain: every event in order
    - Verifying the chain is intact
    - Demonstrating tamper detection: we manually corrupt one record,
      re-verify, and Lynx catches it
    - Exporting the audit log as a jsonl file (the format auditors want)

RUN WITH:
    python examples/06_compliance_audit.py

WHAT YOU'LL SEE:
    - Audit chain intact: ✔
    - Then we tamper with one event, re-verify, see: ✘ broken at seq N
    - Exported evidence file lives at evidence.jsonl
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from lynx import FinalAnswer, Message, Runtime, ToolCall, tool
from lynx.core.mediator import get_registry
from lynx.core.types import canonical_json
from lynx.policy import compile_policy
from lynx.stores.sqlite import SQLiteStore


@tool(cost="low", reversible=True, scope=["customer:read"])
async def get_customer(customer_id: str) -> dict:
    fake_db = {"C-1": {"name": "Alice", "balance_usd": 250}}
    return fake_db.get(customer_id, {"error": "not found"})


@tool(cost="medium", reversible=False, scope=["money:transfer"])
async def issue_refund(customer_id: str, amount_usd: float) -> dict:
    return {"txn": "REF-001", "to": customer_id, "amount_usd": amount_usd}


@issue_refund.shadow
async def _refund_shadow(customer_id: str, amount_usd: float) -> dict:
    return {"would_refund": amount_usd, "to": customer_id}


POLICY = """
version: 1
defaults:
  on_no_match: allow

rules:
  - id: small-refunds-allowed
    match:
      tool: issue_refund
      args.amount_usd.le: 50
    decision: allow
"""


class WorkflowAgent:
    def __init__(self) -> None:
        self._i = 0
        self._plan = [
            ToolCall("get_customer", {"customer_id": "C-1"}, call_id="c1"),
            ToolCall("issue_refund", {"customer_id": "C-1", "amount_usd": 12.50}, call_id="c2"),
            FinalAnswer(text="Processed support ticket. Audit log is complete."),
        ]

    async def step(self, conversation: list[Message]):
        a = self._plan[self._i]
        self._i += 1
        return a


async def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        runtime = Runtime(
            store=SQLiteStore(f"{tmp}/state.db"),
            policy=compile_policy(POLICY),
        )

        result = await runtime.run(
            agent=WorkflowAgent(),
            task="Process support ticket for C-1.",
            principal={"kind": "service", "id": "support-bot"},
        )

        run_id = result.run_id
        print(f"Run completed: {run_id}\n")

        print("─" * 60)
        print("AUDIT CHAIN")
        print("─" * 60)
        events = runtime.audit_chain(run_id)
        for e in events:
            print(f"  seq={e.seq:>3}  kind={e.kind:<22}  id={e.id[:12]}...")
        print()

        # Step 1: verify the chain (clean)
        print("─" * 60)
        print("STEP 1 — verify the chain")
        print("─" * 60)
        ok, err = runtime.verify_audit(run_id)
        print(f"  ✓ intact: {ok}  {err or ''}\n")

        # Step 2: tamper with one event's body and re-verify
        print("─" * 60)
        print("STEP 2 — TAMPER with event seq=1 and re-verify")
        print("─" * 60)
        with runtime.store._conn:
            runtime.store._conn.execute(
                "UPDATE audit_events SET body=? WHERE run_id=? AND seq=1",
                (canonical_json({"evil": "altered_after_the_fact"}), run_id),
            )
        ok, err = runtime.verify_audit(run_id)
        print(f"  ✘ intact: {ok}")
        print(f"     error:  {err}")
        print("  (this is exactly how an auditor catches forgery)\n")

        # Step 3: export the (untampered original would have been) the
        # jsonl format that compliance auditors typically want.
        print("─" * 60)
        print("STEP 3 — export evidence.jsonl")
        print("─" * 60)
        # Write the export to a temp dir so re-running the example doesn't
        # leave files behind in the project tree.
        evidence_path = Path(tmp) / "evidence.jsonl"
        with evidence_path.open("w") as f:
            for e in runtime.audit_chain(run_id):
                f.write(
                    json.dumps(
                        {
                            "id": e.id,
                            "prev": e.prev,
                            "seq": e.seq,
                            "kind": e.kind,
                            "timestamp": e.timestamp.isoformat(),
                            "body": e.body,
                        }
                    )
                    + "\n"
                )
        print(f"  wrote {evidence_path}  ({evidence_path.stat().st_size} bytes)")
        print("  head of file:")
        with evidence_path.open() as f:
            for line in f.readlines()[:2]:
                print(f"    {line[:120].strip()}...")


if __name__ == "__main__":
    get_registry().clear()

    @tool(cost="low", reversible=True, scope=["customer:read"])
    async def get_customer(customer_id: str) -> dict:
        fake_db = {"C-1": {"name": "Alice", "balance_usd": 250}}
        return fake_db.get(customer_id, {"error": "not found"})

    @tool(cost="medium", reversible=False, scope=["money:transfer"])
    async def issue_refund(customer_id: str, amount_usd: float) -> dict:
        return {"txn": "REF-001", "to": customer_id, "amount_usd": amount_usd}

    @issue_refund.shadow
    async def _refund_shadow(customer_id: str, amount_usd: float) -> dict:
        return {"would_refund": amount_usd, "to": customer_id}

    asyncio.run(main())
