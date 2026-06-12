"""Durability tests — RunStore journaling, resume, idempotency, supersede.

The in-memory store here is also the reference implementation of the
RunStore contract: ``append`` atomically rejects a duplicate ``(run_id,
seq)`` with ``DuplicateRecord``.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from lynx import (
    DuplicateRecord,
    FinalAnswer,
    Message,
    StepRecord,
    ToolCall,
    ToolSet,
    auto_approve,
    auto_deny,
    callback_sink,
    compile_policy,
    replay,
    run_agent,
    step_record_from_json,
    step_record_to_json,
    tool,
)
from lynx.cli.main import cli
from lynx.core.types import now_utc
from lynx.durability import idempotency_key

# --- reference store ------------------------------------------------------


class MemoryStore:
    """Reference RunStore: a dict keyed by (run_id, seq)."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, int], StepRecord] = {}

    async def append(self, record: StepRecord) -> None:
        key = (record.run_id, record.seq)
        if key in self.records:
            raise DuplicateRecord(f"({record.run_id}, {record.seq}) already journaled")
        self.records[key] = record

    async def load(self, run_id: str):
        return sorted(
            (r for (rid, _), r in self.records.items() if rid == run_id),
            key=lambda r: r.seq,
        )


class FailingStore(MemoryStore):
    """Raises on the Nth append — simulates a crash mid-run."""

    def __init__(self, fail_on_append: int) -> None:
        super().__init__()
        self._fail_on = fail_on_append
        self._appends = 0

    async def append(self, record: StepRecord) -> None:
        self._appends += 1
        if self._appends == self._fail_on:
            raise ConnectionError("store went away")
        await super().append(record)


# --- tools / agents -------------------------------------------------------

CALLS: dict[str, int] = {}


@tool(reversible=True, scope=("compute:exec",))
async def counted(msg: str) -> str:
    """Echo, counting invocations."""
    CALLS["counted"] = CALLS.get("counted", 0) + 1
    return f"echo: {msg}"


@tool(reversible=False, scope=("payments:write",))
async def charge(amount: int) -> str:
    """Pretend to charge a customer."""
    CALLS["charge"] = CALLS.get("charge", 0) + 1
    return f"charged {amount}"


class CountingAgent:
    def __init__(self, *actions: Any) -> None:
        self._actions = list(actions)
        self.step_calls = 0

    async def step(self, conversation: tuple[Message, ...]):
        self.step_calls += 1
        return self._actions.pop(0)


ALLOW_ALL = "version: 1\ndefaults: { on_no_match: allow }\nrules: []"


@pytest.fixture(autouse=True)
def _reset_calls():
    CALLS.clear()
    yield


def _collector():
    seen: list[Any] = []

    async def collect(ev):
        seen.append(ev)

    return seen, callback_sink(collect)


async def _seed_orphaned_intent(store, run_id: str, tool_name: str, args: dict) -> None:
    """Hand-craft the crash-window journal: started, proposal, intent — no result."""
    key = idempotency_key(run_id, 0, tool_name, args)
    seed = [
        ("run.started", {"task": "t"}, ""),
        (
            "model.output",
            {"step": 0, "type": "tool_call", "tool": tool_name, "args": args, "call_id": "c1"},
            "",
        ),
        ("action.intent", {"step": 0, "tool": tool_name, "verdict": "allow"}, key),
    ]
    for seq, (kind, body, k) in enumerate(seed):
        await store.append(
            StepRecord(
                run_id=run_id, seq=seq, kind=kind, idempotency_key=k, body=body, timestamp=now_utc()
            )
        )


# --- basics ---------------------------------------------------------------


async def test_store_requires_run_id() -> None:
    policy = compile_policy(ALLOW_ALL)
    for bad_run_id in (None, ""):
        with pytest.raises(ValueError, match="run_id is required"):
            await run_agent(
                CountingAgent(FinalAnswer(text="x")),
                task="t",
                tools=ToolSet.from_functions(counted),
                policy=policy,
                store=MemoryStore(),
                run_id=bad_run_id,
            )


async def test_no_store_behavior_unchanged() -> None:
    policy = compile_policy(ALLOW_ALL)
    agent = CountingAgent(
        ToolCall(tool="counted", args={"msg": "hi"}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(counted),
        policy=policy,
        on_approval=auto_deny("no"),
    )
    assert result.final_answer == "done"
    assert CALLS["counted"] == 1


async def test_fresh_run_journals_expected_records() -> None:
    policy = compile_policy(ALLOW_ALL)
    store = MemoryStore()
    agent = CountingAgent(
        ToolCall(tool="counted", args={"msg": "hi"}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(counted),
        policy=policy,
        on_approval=auto_deny("no"),
        store=store,
        run_id="r1",
    )
    assert result.final_answer == "done"
    assert result.correlation_id == "r1"  # cid defaults to run_id

    records = await store.load("r1")
    kinds = [r.kind for r in records]
    assert kinds == [
        "run.started",
        "model.output",
        "action.intent",
        "action.result",
        "model.output",
        "final",
    ]
    assert [r.seq for r in records] == list(range(6))

    intent = records[2]
    res = records[3]
    expected_key = idempotency_key("r1", 0, "counted", {"msg": "hi"})
    assert intent.idempotency_key == expected_key
    assert res.idempotency_key == expected_key
    assert intent.body["verdict"] == "allow"
    assert res.body["ok"] is True
    assert res.body["message"].startswith("[ok]")


# --- resume ---------------------------------------------------------------


async def test_resume_completed_run_is_idempotent() -> None:
    policy = compile_policy(ALLOW_ALL)
    store = MemoryStore()
    tools = ToolSet.from_functions(counted)
    first = CountingAgent(
        ToolCall(tool="counted", args={"msg": "hi"}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    await run_agent(first, task="t", tools=tools, policy=policy, store=store, run_id="r1")
    assert CALLS["counted"] == 1

    again = CountingAgent()  # would raise IndexError if stepped
    result = await run_agent(again, task="t", tools=tools, policy=policy, store=store, run_id="r1")
    assert result.final_answer == "done"
    assert again.step_calls == 0  # model never re-called
    assert CALLS["counted"] == 1  # tool never re-executed


async def test_resume_after_crash_replays_completed_steps() -> None:
    policy = compile_policy(ALLOW_ALL)
    tools = ToolSet.from_functions(counted, charge)
    # Records: 0 run.started, 1 model.output(step0), 2 intent, 3 result,
    # 4 model.output(step1) <- fail here, after step 0 fully journaled.
    store = FailingStore(fail_on_append=5)
    first = CountingAgent(
        ToolCall(tool="counted", args={"msg": "a"}, call_id="c1"),
        ToolCall(tool="charge", args={"amount": 42}, call_id="c2"),
        FinalAnswer(text="done"),
    )
    crashed = await run_agent(
        first,
        task="t",
        tools=tools,
        policy=policy,
        on_approval=auto_approve(),
        store=store,
        run_id="r1",
    )
    assert crashed.error is not None and "store.append failed" in crashed.error
    assert CALLS["counted"] == 1
    assert CALLS.get("charge", 0) == 0  # never journaled, never executed

    # Resume: step 0 replays from the journal; agent only drives steps 1..2.
    seen, sink = _collector()
    second = CountingAgent(
        ToolCall(tool="charge", args={"amount": 42}, call_id="c2"),
        FinalAnswer(text="done"),
    )
    result = await run_agent(
        second,
        task="t",
        tools=tools,
        policy=policy,
        on_approval=auto_approve(),
        sinks=(sink,),
        store=store,
        run_id="r1",
    )
    assert result.final_answer == "done"
    assert CALLS["counted"] == 1  # NOT re-executed
    assert CALLS["charge"] == 1
    assert second.step_calls == 2  # steps 1 and 2 only; step 0 replayed
    kinds = [ev.kind for ev in seen]
    assert "run.resumed" in kinds
    assert "step.replayed" in kinds


async def test_crash_between_intent_and_result_marks_uncertain() -> None:
    """The crash window: intent journaled, result missing → uncertain retry
    flagged in context.extra so policy can gate it."""
    deny_uncertain = compile_policy(
        """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: gate-uncertain-retries
    match: { context.extra.uncertain_retry: true }
    decision: deny
    reason: action may have already executed
        """
    )
    store = MemoryStore()
    # The crash-window journal: intent journaled with verdict allow, no result.
    await _seed_orphaned_intent(store, "r1", "charge", {"amount": 42})

    seen, sink = _collector()
    agent = CountingAgent(FinalAnswer(text="gave up"))  # only drives post-denial
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(charge),
        policy=deny_uncertain,
        sinks=(sink,),
        store=store,
        run_id="r1",
    )
    assert result.final_answer == "gave up"
    assert CALLS.get("charge", 0) == 0  # the uncertain action was NOT re-run
    kinds = [ev.kind for ev in seen]
    assert "action.uncertain" in kinds
    evaluated = next(ev for ev in seen if ev.kind == "policy.evaluated")
    assert evaluated.body["verdict"] == "deny"
    assert "gate-uncertain-retries" in evaluated.body["matched_rules"]


async def test_uncertain_retry_reruns_under_permissive_policy() -> None:
    """Without an uncertain_retry rule, policy decides as usual — here ALLOW,
    so the action re-executes (at-least-once, by the user's own policy)."""
    policy = compile_policy(ALLOW_ALL)
    store = MemoryStore()
    await _seed_orphaned_intent(store, "r1", "counted", {"msg": "x"})

    agent = CountingAgent(FinalAnswer(text="done"))
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(counted),
        policy=policy,
        store=store,
        run_id="r1",
    )
    assert result.final_answer == "done"
    assert CALLS["counted"] == 1  # re-ran exactly once on resume


# --- concurrency ----------------------------------------------------------


class RacingStore(MemoryStore):
    """load() sees an empty journal, but another worker has already written
    seq 0 — the first append collides. Models the re-dispatch race."""

    def __init__(self) -> None:
        super().__init__()
        self.records[("r1", 0)] = StepRecord(
            run_id="r1",
            seq=0,
            kind="run.started",
            idempotency_key="",
            body={"task": "t"},
            timestamp=now_utc(),
        )

    async def load(self, run_id: str):
        return []


async def test_losing_worker_is_superseded_before_any_side_effect() -> None:
    policy = compile_policy(ALLOW_ALL)
    seen, sink = _collector()
    agent = CountingAgent(
        ToolCall(tool="charge", args={"amount": 42}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    result = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(charge),
        policy=policy,
        sinks=(sink,),
        store=RacingStore(),
        run_id="r1",
    )
    assert result.error is not None and result.error.startswith("superseded:")
    assert result.final_answer is None
    assert CALLS.get("charge", 0) == 0  # loser executed NOTHING
    assert agent.step_calls == 0  # and burned no model calls
    assert "run.superseded" in [ev.kind for ev in seen]


# --- replay / serialization / CLI ------------------------------------------


async def test_replay_reconstructs_run_view() -> None:
    policy = compile_policy(ALLOW_ALL)
    store = MemoryStore()
    agent = CountingAgent(
        ToolCall(tool="counted", args={"msg": "hi"}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(counted),
        policy=policy,
        store=store,
        run_id="r1",
    )
    view = replay(await store.load("r1"))
    assert view.run_id == "r1"
    assert view.attempts == 1
    assert view.final_answer == "done"
    assert len(view.steps) == 2
    first = view.steps[0]
    assert first.tool == "counted"
    assert first.verdict == "allow"
    assert first.ok is True
    assert first.uncertain is False
    assert view.steps[1].tool is None  # the final answer step


async def test_step_record_json_roundtrip() -> None:
    rec = StepRecord(
        run_id="r1",
        seq=3,
        kind="action.intent",
        idempotency_key="abc123",
        body={"step": 1, "tool": "x", "args": {"a": 1}, "verdict": "allow"},
        timestamp=now_utc(),
    )
    back = step_record_from_json(step_record_to_json(rec))
    assert back.run_id == rec.run_id
    assert back.seq == rec.seq
    assert back.kind == rec.kind
    assert back.idempotency_key == rec.idempotency_key
    assert dict(back.body) == dict(rec.body)
    assert back.timestamp == rec.timestamp


async def test_cli_trace_renders_journal(tmp_path) -> None:
    policy = compile_policy(ALLOW_ALL)
    store = MemoryStore()
    agent = CountingAgent(
        ToolCall(tool="counted", args={"msg": "hi"}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(counted),
        policy=policy,
        store=store,
        run_id="r1",
    )
    records_file = tmp_path / "r1.jsonl"
    records_file.write_text(
        "\n".join(step_record_to_json(r) for r in await store.load("r1")) + "\n"
    )
    out = CliRunner().invoke(cli, ["trace", str(records_file)])
    assert out.exit_code == 0, out.output
    assert "run r1" in out.output
    assert "counted" in out.output
    assert "final: done" in out.output


# --- review-fix regressions -------------------------------------------------


async def test_resumed_attempt_gets_distinct_correlation_id() -> None:
    """(correlation_id, seq) must never collide across attempts — sinks key on it."""
    policy = compile_policy(ALLOW_ALL)
    store = MemoryStore()
    first = CountingAgent(
        ToolCall(tool="counted", args={"msg": "hi"}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    r1 = await run_agent(
        first,
        task="t",
        tools=ToolSet.from_functions(counted),
        policy=policy,
        store=store,
        run_id="r1",
    )
    assert r1.correlation_id == "r1"  # fresh journaled run: cid IS the run_id

    r2 = await run_agent(
        CountingAgent(),
        task="t",
        tools=ToolSet.from_functions(counted),
        policy=policy,
        store=store,
        run_id="r1",
    )
    assert r2.correlation_id != "r1"
    assert r2.correlation_id.startswith("r1#")  # groupable by prefix


async def test_bundle_change_on_resume_emits_warning() -> None:
    policy_a = compile_policy(ALLOW_ALL)
    policy_b = compile_policy(
        "version: 1\ndefaults: { on_no_match: allow }\nrules:\n"
        "  - id: extra\n    match: { tool: nothing }\n    decision: deny\n"
    )
    assert policy_a.id != policy_b.id
    store = MemoryStore()
    first = CountingAgent(
        ToolCall(tool="counted", args={"msg": "hi"}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    await run_agent(
        first,
        task="t",
        tools=ToolSet.from_functions(counted),
        policy=policy_a,
        store=store,
        run_id="r1",
    )

    seen, sink = _collector()
    await run_agent(
        CountingAgent(),
        task="t",
        tools=ToolSet.from_functions(counted),
        policy=policy_b,
        sinks=(sink,),
        store=store,
        run_id="r1",
    )
    changed = [ev for ev in seen if ev.kind == "run.bundle_changed"]
    assert len(changed) == 1
    assert changed[0].body["journaled_bundle_id"] == policy_a.id
    assert changed[0].body["current_bundle_id"] == policy_b.id


async def test_store_failure_keeps_steps_taken_and_emits_run_started_first() -> None:
    policy = compile_policy(ALLOW_ALL)
    seen, sink = _collector()

    class LoadFails(MemoryStore):
        async def load(self, run_id: str):
            raise ConnectionError("load down")

    result = await run_agent(
        CountingAgent(),
        task="t",
        tools=ToolSet.from_functions(counted),
        policy=policy,
        sinks=(sink,),
        store=LoadFails(),
        run_id="r1",
    )
    assert result.error is not None and "store.load failed" in result.error
    kinds = [ev.kind for ev in seen]
    assert kinds[0] == "run.started"  # never an orphan run.failed
    assert kinds[-1] == "run.failed"

    # And a mid-run store crash reports the steps actually taken.
    crash_store = FailingStore(fail_on_append=5)
    agent = CountingAgent(
        ToolCall(tool="counted", args={"msg": "a"}, call_id="c1"),
        ToolCall(tool="counted", args={"msg": "b"}, call_id="c2"),
        FinalAnswer(text="done"),
    )
    crashed = await run_agent(
        agent,
        task="t",
        tools=ToolSet.from_functions(counted),
        policy=policy,
        store=crash_store,
        run_id="r2",
    )
    assert "store.append failed" in (crashed.error or "")
    assert crashed.steps_taken == 1  # step 0 completed before the crash


async def test_replay_deny_orphan_is_not_uncertain() -> None:
    """A deny-verdict intent never executed — replay must agree with the kernel."""
    store = MemoryStore()
    key = idempotency_key("r1", 0, "charge", {"amount": 1})
    seed = [
        ("run.started", {"task": "t"}, ""),
        (
            "model.output",
            {
                "step": 0,
                "type": "tool_call",
                "tool": "charge",
                "args": {"amount": 1},
                "call_id": "c",
            },
            "",
        ),
        ("action.intent", {"step": 0, "tool": "charge", "verdict": "deny"}, key),
    ]
    for seq, (kind, body, k) in enumerate(seed):
        await store.append(
            StepRecord(
                run_id="r1", seq=seq, kind=kind, idempotency_key=k, body=body, timestamp=now_utc()
            )
        )
    view = replay(await store.load("r1"))
    assert view.steps[0].uncertain is False  # deny never executes


async def test_replay_marks_resolved_uncertain_retry() -> None:
    """A denied uncertain retry must keep the 'may have executed' fact visible."""
    deny_uncertain = compile_policy(
        """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: gate
    match: { context.extra.uncertain_retry: true }
    decision: deny
    reason: action may have already executed
        """
    )
    store = MemoryStore()
    await _seed_orphaned_intent(store, "r1", "charge", {"amount": 42})
    await run_agent(
        CountingAgent(FinalAnswer(text="reconcile")),
        task="t",
        tools=ToolSet.from_functions(charge),
        policy=deny_uncertain,
        store=store,
        run_id="r1",
    )
    view = replay(await store.load("r1"))
    step0 = view.steps[0]
    assert step0.ok is False and step0.verdict == "deny"
    assert step0.uncertain is False  # resolved now...
    assert step0.resolved_uncertain is True  # ...but the history is not erased


async def test_cli_trace_refuses_mixed_runs_and_audit_files(tmp_path) -> None:
    rec = StepRecord(
        run_id="a", seq=0, kind="run.started", idempotency_key="", body={}, timestamp=now_utc()
    )
    rec2 = StepRecord(
        run_id="b", seq=0, kind="run.started", idempotency_key="", body={}, timestamp=now_utc()
    )
    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text(step_record_to_json(rec) + "\n" + step_record_to_json(rec2) + "\n")
    out = CliRunner().invoke(cli, ["trace", str(mixed)])
    assert out.exit_code == 1
    assert "pick one with --run-id" in out.output

    ok = CliRunner().invoke(cli, ["trace", str(mixed), "--run-id", "a"])
    assert ok.exit_code == 0, ok.output
    assert "run a" in ok.output

    audit = tmp_path / "audit.jsonl"
    audit.write_text('{"correlation_id": "x", "seq": 0, "kind": "run.started", "body": {}}\n')
    out2 = CliRunner().invoke(cli, ["trace", str(audit)])
    assert out2.exit_code == 1
    assert "audit-sink file" in out2.output
