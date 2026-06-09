"""Crash-resume + approval-resume tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from gazelle import FinalAnswer, ToolCall, tool
from gazelle.core.mediator import get_registry
from gazelle.core.types import RunStatus
from gazelle.policy import compile_policy
from gazelle.runtime import Runtime
from gazelle.stores.sqlite import SQLiteStore


@pytest.fixture
def fresh(tmp_path):
    get_registry().clear()

    @tool(cost="low", reversible=False, scope=["filesystem:write"])
    async def make_marker(path: str) -> str:
        Path(path).write_text("hello")
        return f"wrote {path}"

    @make_marker.shadow
    async def _make_marker_shadow(path: str) -> dict:
        return {"would_write": path}

    store = SQLiteStore(tmp_path / "state.db")
    bundle = compile_policy(
        """
version: 1
defaults:
  on_no_match: allow
rules:
  - id: irreversible-needs-approval
    match:
      declared.reversible: false
    decision: approve_required
    approvers: ["@oncall"]
        """
    )
    yield Runtime(store=store, policy=bundle), tmp_path
    get_registry().clear()


class _Agent:
    """Conversation-aware so it survives being instantiated freshly on resume."""

    def __init__(self, marker_path: Path):
        self.marker_path = marker_path

    async def step(self, conversation):
        # If we've already seen a tool result for make_marker, we're done.
        for msg in conversation:
            if msg.role == "tool" and msg.name == "make_marker":
                return FinalAnswer(text="done")
        return ToolCall("make_marker", {"path": str(self.marker_path)}, call_id="c1")


async def test_approve_then_resume_executes_action(fresh):
    runtime, tmp = fresh
    marker = tmp / "marker.txt"

    # 1. First run: pauses for approval.
    result1 = await runtime.run(
        agent=_Agent(marker),
        task="touch a file",
        principal={"kind": "user", "id": "tester"},
    )
    assert result1.status == RunStatus.PAUSED
    assert result1.paused_approval_id is not None
    assert not marker.exists()

    # 2. Approve from a "different process" — bypass the in-memory broker.
    await runtime.approve(result1.paused_approval_id, approver="ops")

    # 3. Resume: should execute the approved action and finish.
    result2 = await runtime.resume(_Agent(marker), run_id=result1.run_id)
    assert result2.status == RunStatus.SUCCEEDED, f"error: {result2.error}"
    assert result2.final_answer == "done"
    assert marker.exists(), "approved action should have executed"


async def test_deny_then_resume_continues_loop(fresh):
    runtime, tmp = fresh
    marker = tmp / "marker.txt"

    result1 = await runtime.run(
        agent=_Agent(marker),
        task="touch a file",
        principal={"kind": "user", "id": "tester"},
    )
    assert result1.status == RunStatus.PAUSED

    await runtime.deny(result1.paused_approval_id, approver="ops", reason="no")

    result2 = await runtime.resume(_Agent(marker), run_id=result1.run_id)
    # The agent ran out of plan after the denial → final answer, succeeded run.
    assert result2.status == RunStatus.SUCCEEDED
    assert not marker.exists(), "denied action must NOT have executed"


async def test_idempotency_key_is_deterministic_across_runs():
    """Same (run_id, seq, tool, args) → same key, every time."""
    from gazelle.core.types import compute_idempotency_key

    k1 = compute_idempotency_key("R1", 0, "shell", {"cmd": "ls -la"})
    k2 = compute_idempotency_key("R1", 0, "shell", {"cmd": "ls -la"})
    k3 = compute_idempotency_key("R2", 0, "shell", {"cmd": "ls -la"})
    k4 = compute_idempotency_key("R1", 1, "shell", {"cmd": "ls -la"})
    assert k1 == k2
    assert k1 != k3 and k1 != k4
