"""
================================================================
EXAMPLE 24 — "Durable runs: crash, resume, never double-charge" (ADVANCED)
================================================================

SCENARIO:
    A payments agent crashes mid-run (here: the journal store goes away
    after a few writes). Without durability, a retry re-runs the whole
    task: the model gets re-called (tokens re-burned) and the customer
    gets charged twice. With a RunStore, the retry resumes at the first
    incomplete step — completed steps replay from the journal.

    Lynx ships NO storage. You implement the two-method RunStore protocol
    over whatever you already run (Redis, Postgres, a file). The whole
    contract is one sentence: `append` must atomically reject a duplicate
    (run_id, seq) by raising DuplicateRecord. The in-memory store below is
    the entire reference implementation.

WHAT THIS EXAMPLE SHOWS:
    - A complete RunStore in ~15 lines (dict-backed)
    - Act 1-3: a crash mid-run, then `run_agent` with the same run_id
      resuming — the model is NOT re-called for completed steps, the
      charge executes exactly once, and a finished run returns the same
      answer forever
    - Act 4: THE CRASH WINDOW — the process dies after the charge executed
      but before its result was journaled. On resume the action is
      *uncertain*: policy sees context.extra.uncertain_retry = true and a
      YAML rule denies the re-run, so the customer is NOT charged twice;
      the agent routes to manual reconciliation instead
    - Act 5: a losing concurrent worker exiting with a `superseded:` error
      before executing anything
    - Act 6: `replay()` reconstructing both runs, including the
      resolved-uncertain marker for forensics
    - Act 7: a FILE-backed store built on step_record_to_json (the format
      `lynx trace <file>` reads), plus the run.bundle_changed warning when
      a run is resumed under a different policy than its journal

RUN WITH:
    python examples/24_durable_resume.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from lynx import (
    DuplicateRecord,
    FinalAnswer,
    Message,
    StepRecord,
    ToolCall,
    ToolSet,
    auto_approve,
    compile_policy,
    replay,
    run_agent,
    step_record_from_json,
    step_record_to_json,
    tool,
)

# ---------------------------------------------------------------------------
# A complete RunStore — your storage, your dependency. Swap the dict for
# Redis HSETNX / Postgres `PRIMARY KEY (run_id, seq)` and nothing else changes.
# ---------------------------------------------------------------------------


class MemoryRunStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, int], StepRecord] = {}

    async def append(self, record: StepRecord) -> None:
        key = (record.run_id, record.seq)
        if key in self.records:  # the one load-bearing line
            raise DuplicateRecord(f"{key} already journaled")
        self.records[key] = record

    async def load(self, run_id: str):
        return sorted(
            (r for (rid, _), r in self.records.items() if rid == run_id),
            key=lambda r: r.seq,
        )


class JsonlRunStore:
    """A FILE-backed store (~12 lines) — survives the process, and its file
    is exactly what `lynx trace <file>` reads. Single process only: a flat
    file cannot enforce (run_id, seq) uniqueness across processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seen: set[tuple[str, int]] = set()
        if path.exists():
            for line in path.read_text().splitlines():
                rec = step_record_from_json(line)
                self.seen.add((rec.run_id, rec.seq))

    async def append(self, record: StepRecord) -> None:
        key = (record.run_id, record.seq)
        if key in self.seen:
            raise DuplicateRecord(f"{key} already journaled")
        with self.path.open("a", encoding="utf-8") as f:
            f.write(step_record_to_json(record) + "\n")
        self.seen.add(key)

    async def load(self, run_id: str):
        if not self.path.exists():
            return []
        records = (step_record_from_json(ln) for ln in self.path.read_text().splitlines())
        return sorted((r for r in records if r.run_id == run_id), key=lambda r: r.seq)


class CrashingStore(MemoryRunStore):
    """Simulates the process dying: the Nth append never happens."""

    def __init__(self, fail_on_append: int) -> None:
        super().__init__()
        self._remaining = fail_on_append

    async def append(self, record: StepRecord) -> None:
        self._remaining -= 1
        if self._remaining == 0:
            raise ConnectionError("simulated crash")
        await super().append(record)


# ---------------------------------------------------------------------------
# Tools — note the charge counter: the whole demo is about it never being
# higher than one per invoice.
# ---------------------------------------------------------------------------

CHARGES: dict[str, int] = {}  # invoice id -> times charged


@tool(reversible=True, scope=("crm:read",))
async def look_up_customer(email: str) -> str:
    return f"customer 1337 ({email}), balance due: $42"


@tool(reversible=False, scope=("payments:write",))
async def charge_customer(customer_id: int, amount: int, invoice: str) -> str:
    CHARGES[invoice] = CHARGES.get(invoice, 0) + 1
    return f"charged customer {customer_id}: ${amount} for {invoice}"


# ---------------------------------------------------------------------------
# A scripted "model" so the demo is deterministic and offline.
# ---------------------------------------------------------------------------


class PaymentsAgent:
    """Decides the next action from the conversation — never from hidden
    state, which is what makes resume work without serialize hooks."""

    def __init__(self, invoice: str) -> None:
        self._invoice = invoice

    async def step(self, conv: tuple[Message, ...]):
        text = " ".join(m.content for m in conv)
        if "may have already executed" in text:
            # The runtime told us the charge is in an unknown state.
            # A well-behaved agent does NOT blindly retry money movement.
            return FinalAnswer(
                text="charge state unknown after crash — flagged for manual reconciliation"
            )
        if "customer 1337" not in text:
            return ToolCall("look_up_customer", {"email": "ada@example.com"}, call_id="c1")
        if "charged customer" not in text:
            return ToolCall(
                "charge_customer",
                {"customer_id": 1337, "amount": 42, "invoice": self._invoice},
                call_id="c2",
            )
        return FinalAnswer(text=f"collected $42 from customer 1337 ({self._invoice})")


POLICY = """
version: 1
defaults: { on_no_match: allow, on_missing_shadow: allow }
rules:
  - id: never-rerun-uncertain-payments
    description: an interrupted charge may have gone through — a human decides
    match:
      context.extra.uncertain_retry: true
      declared.reversible: false
    decision: deny
    reason: action may have already executed in a crashed attempt
"""


async def run(agent_invoice: str, store, **kw):
    return await run_agent(
        PaymentsAgent(agent_invoice),
        task=f"Collect the $42 Ada owes us ({agent_invoice})",
        tools=ToolSet.from_functions(look_up_customer, charge_customer),
        policy=compile_policy(POLICY),
        on_approval=auto_approve(),
        store=store,
        run_id=agent_invoice,
        **kw,
    )


async def main() -> None:
    # ---- Act 1: crash mid-run, before the charge ---------------------------
    print("=" * 64)
    print("Act 1 — invoice-0611: crash after the lookup step is journaled")
    print("=" * 64)
    store = CrashingStore(fail_on_append=5)  # dies journaling the charge proposal
    crashed = await run("invoice-0611", store)
    print(f"  run error : {crashed.error}")
    print(f"  charges   : {CHARGES.get('invoice-0611', 0)}  (charge never reached)")

    # ---- Act 2: the supervisor retries — same run_id ----------------------
    print()
    print("=" * 64)
    print("Act 2 — retry with the same run_id: resume, don't redo")
    print("=" * 64)
    result = await run("invoice-0611", store)  # journal survived the 'crash'
    print(f"  final     : {result.final_answer}")
    print(f"  charges   : {CHARGES['invoice-0611']}  <- exactly one, across crash + retry")

    # ---- Act 3: retry once more — completed runs are idempotent -----------
    result2 = await run("invoice-0611", store)
    print(f"  rerun     : {result2.final_answer!r}, charges still {CHARGES['invoice-0611']}")

    # ---- Act 4: the crash WINDOW — charge executed, result lost -----------
    print()
    print("=" * 64)
    print("Act 4 — invoice-0612: crash AFTER the charge ran, result lost")
    print("=" * 64)
    store2 = CrashingStore(fail_on_append=7)  # dies journaling the charge RESULT
    crashed2 = await run("invoice-0612", store2)
    print(f"  run error : {crashed2.error}")
    print(f"  charges   : {CHARGES['invoice-0612']}  (the charge DID happen; journal doesn't know)")

    print("  ...resuming: the orphaned intent makes the action UNCERTAIN;")
    print("  policy rule never-rerun-uncertain-payments denies the re-run:")
    resumed2 = await run("invoice-0612", store2)
    print(f"  final     : {resumed2.final_answer}")
    print(f"  charges   : {CHARGES['invoice-0612']}  <- still one; no double charge")

    # ---- Act 5: a racing worker loses cleanly -----------------------------
    print()
    print("=" * 64)
    print("Act 5 — a second worker on the same run_id is superseded")
    print("=" * 64)

    class StaleLoadStore(MemoryRunStore):
        # Models the race: this worker loaded before the other worker wrote.
        def __init__(self, inner: MemoryRunStore) -> None:
            super().__init__()
            self.records = inner.records

        async def load(self, run_id: str):
            return []

    loser = await run("invoice-0611", StaleLoadStore(store))
    print(f"  loser     : {loser.error}")
    print(f"  charges   : {CHARGES['invoice-0611']}  (the loser executed nothing)")

    # ---- Act 6: inspect the journals ---------------------------------------
    print()
    print("=" * 64)
    print("Act 6 — replay() reconstructs what happened, forensics included")
    print("=" * 64)
    for invoice, st in [("invoice-0611", store), ("invoice-0612", store2)]:
        view = replay(await st.load(invoice))
        print(f"  run {view.run_id}: {view.records} records, {view.attempts} attempt(s)")
        for s in view.steps:
            if s.tool is None:
                print(f"    step {s.step}: final answer: {s.message}")
                continue
            note = " [resolved uncertain retry]" if s.resolved_uncertain else ""
            print(f"    step {s.step}: {s.tool} verdict={s.verdict} ok={s.ok}{note}")

    # ---- Act 7: a FILE-backed store + `lynx trace` + bundle-change warning --
    print()
    print("=" * 64)
    print("Act 7 — JSONL file store, `lynx trace`, and run.bundle_changed")
    print("=" * 64)
    jsonl_path = Path("invoice-0613.jsonl")
    jsonl_path.unlink(missing_ok=True)
    file_store = JsonlRunStore(jsonl_path)
    done = await run("invoice-0613", file_store)
    print(f"  final     : {done.final_answer}")
    print(
        f"  journal   : {jsonl_path} ({len(jsonl_path.read_text().splitlines())} records on disk)"
    )
    print(f"  inspect it: $ lynx trace {jsonl_path}")

    # Resume the COMPLETED run under a different policy: the journal records
    # which bundle it was written with, so Lynx warns instead of guessing.
    looser = compile_policy(
        "version: 1\ndefaults: { on_no_match: allow, on_missing_shadow: allow }\nrules: []"
    )
    warnings: list[str] = []

    async def watch(event):
        if event.kind == "run.bundle_changed":
            warnings.append(
                f"{event.body['journaled_bundle_id'][:8]} -> {event.body['current_bundle_id'][:8]}"
            )

    again = await run_agent(
        PaymentsAgent("invoice-0613"),
        task="Collect the $42 Ada owes us (invoice-0613)",
        tools=ToolSet.from_functions(look_up_customer, charge_customer),
        policy=looser,  # different bundle than the journal was written with
        on_approval=auto_approve(),
        sinks=(watch,),
        store=file_store,
        run_id="invoice-0613",
    )
    print(f"  re-run    : {again.final_answer!r} (replayed; charges {CHARGES['invoice-0613']})")
    print(f"  warning   : run.bundle_changed emitted: {warnings[0]}")
    print("  (the journal is left on disk — try the lynx trace command, then rm it)")


if __name__ == "__main__":
    asyncio.run(main())
