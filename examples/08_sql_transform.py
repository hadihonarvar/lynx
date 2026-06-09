"""
================================================================
EXAMPLE 08 — "Auto-fix queries before they run" (ADVANCED)
================================================================

SCENARIO:
    Imagine a SaaS company that serves many businesses with one shared
    database. Each business is called a "tenant" and they have a tenant_id
    in every row (1 = Alice's bakery, 2 = Bob's bookstore, etc.).

    You have an AI assistant that runs database queries. THE NIGHTMARE is
    if the assistant accidentally runs:

        DELETE FROM users WHERE name = 'John'

    Without a tenant_id filter, this would delete every John across EVERY
    customer of your service. Catastrophic.

    With Lynx, you write one rule: "any update or delete must include
    a tenant_id filter. If it doesn't, automatically ADD one before the
    query runs."

    The assistant writes:    DELETE FROM users WHERE name = 'John'
    Lynx silently rewrites:  DELETE FROM users WHERE name = 'John'
                                                AND tenant_id = 'TENANT-ALICE'

    The query is now safe. The assistant didn't need to know the rule.

REAL-WORLD USE CASE:
    Multi-tenant safety in SaaS apps:
      - Row-level security via policy
      - Soft-deletes (add `WHERE deleted_at IS NULL`)
      - Read-replica routing (rewrite query targets)
      - Adding `LIMIT 1000` to every SELECT
      - Anything where "always add this clause" is a safety rule

WHAT THIS EXAMPLE SHOWS:
    - The TRANSFORM verdict — Lynx rewrites the action's arguments
    - A multi-tier policy: SELECTs allowed, bulk mutations denied,
      scoped mutations transformed
    - The agent doesn't know the rewrite happened; the audit log does

RUN WITH:
    python examples/08_sql_transform.py

WHAT YOU'LL SEE:
    Step 0: SELECT * FROM users LIMIT 10           → allow
    Step 1: DELETE FROM users                      → deny (no WHERE)
    Step 2: UPDATE users SET active=0 WHERE id=42  → transform
            actually ran: ... AND tenant_id='TENANT-ALICE'
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from lynx import FinalAnswer, Message, Runtime, ToolCall, tool
from lynx.core.mediator import get_registry
from lynx.policy import load_policy_file
from lynx.stores.sqlite import SQLiteStore

# ---------------------------------------------------------------------------
# Tool — a fake SQL executor. In real life this would hit Postgres/MySQL.
# ---------------------------------------------------------------------------


@tool(cost="medium", reversible=True, scope=["db:read", "db:write"])
async def sql_exec(sql: str) -> dict:
    """Execute SQL. The 'rows_affected' is pretend; we just echo the query."""
    return {"executed": sql, "rows_affected": 1}


# ---------------------------------------------------------------------------
# Agent — proposes one SELECT, one bulk DELETE, one scoped UPDATE.
# ---------------------------------------------------------------------------


class SQLAgent:
    def __init__(self) -> None:
        self._i = 0
        self._plan = [
            # 1. SELECT — fine.
            ToolCall("sql_exec", {"sql": "SELECT * FROM users LIMIT 10"}, call_id="c1"),
            # 2. DELETE without WHERE — catastrophic, must be denied.
            ToolCall("sql_exec", {"sql": "DELETE FROM users"}, call_id="c2"),
            # 3. UPDATE with WHERE — runs after Lynx adds tenant_id filter.
            ToolCall(
                "sql_exec", {"sql": "UPDATE users SET active = 0 WHERE id = 42"}, call_id="c3"
            ),
            FinalAnswer(text="Demonstrated SELECT, bulk-DELETE, and scoped UPDATE."),
        ]

    async def step(self, conversation: list[Message]):
        a = self._plan[self._i]
        self._i += 1
        return a


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    import tempfile

    policy_path = Path(__file__).resolve().parent / "policies" / "sql-transform.yaml"

    with tempfile.TemporaryDirectory() as tmp:
        runtime = Runtime(
            store=SQLiteStore(f"{tmp}/state.db"),
            policy=load_policy_file(policy_path),
        )

        result = await runtime.run(
            agent=SQLAgent(),
            task="Demonstrate SELECT, bulk-DELETE-denied, scoped-UPDATE.",
            principal={"kind": "user", "id": "alice@acme.com"},
        )

        print(f"Run: {result.run_id} → {result.status}")
        print()
        print("Step-by-step:")
        for step in runtime.get_steps(result.run_id):
            verdict = step.decision.verdict.value if step.decision else "?"
            proposed = step.action.args.get("sql", "?") if step.action else "?"
            actually_ran = (
                step.result.value.get("executed", "?")
                if step.result and step.result.ok and isinstance(step.result.value, dict)
                else None
            )
            print(f"  #{step.seq}  proposed: {proposed}")
            print(f"      verdict:   {verdict}  ({step.decision.reason if step.decision else ''})")
            if verdict == "transform":
                print(f"      actually:  {actually_ran}")
            elif verdict == "allow":
                print("      ran as-is.")
            elif verdict == "deny":
                print("      ⛔ blocked.")


if __name__ == "__main__":
    get_registry().clear()

    @tool(cost="medium", reversible=True, scope=["db:read", "db:write"])
    async def sql_exec(sql: str) -> dict:
        return {"executed": sql, "rows_affected": 1}

    asyncio.run(main())
