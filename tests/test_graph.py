"""Handoff graph — sequential multi-node workflows with per-node policy.

Contracts under test: per-node permission boundaries, explicit context
passing, denial-count routing, mandatory transition bound, YAML compile
validation, and durable resume of both node runs and routing decisions.
"""

from __future__ import annotations

from typing import Any

import pytest

from lynx import (
    FinalAnswer,
    GraphCompileError,
    GraphNode,
    Message,
    NodeOutcome,
    ToolCall,
    ToolSet,
    compile_graph,
    compile_policy,
    run_graph,
    tool,
)
from tests.test_durability import MemoryStore

ALLOW_ALL = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []")
DENY_WRITES = compile_policy(
    """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: read-only
    match: { declared.scope.contains: "fs:write" }
    decision: deny
    reason: this node is read-only
    """
)

CALLS: dict[str, int] = {}


@tool(reversible=True, scope=("fs:read",))
async def read_doc(name: str) -> str:
    CALLS["read"] = CALLS.get("read", 0) + 1
    return f"contents of {name}"


@tool(reversible=True, scope=("fs:write",))
async def write_doc(name: str, text: str) -> str:
    CALLS["write"] = CALLS.get("write", 0) + 1
    return f"wrote {name}"


TOOLS = ToolSet.from_functions(read_doc, write_doc)


class Scripted:
    """Returns canned actions; records the tasks it was asked to do."""

    def __init__(self, *actions: Any) -> None:
        self._actions = list(actions)
        self.tasks: list[str] = []
        self.step_calls = 0

    async def step(self, conversation: tuple[Message, ...]):
        if not self.tasks or conversation[0].content != self.tasks[-1]:
            self.tasks.append(conversation[0].content)
        self.step_calls += 1
        return self._actions.pop(0)


def node(agent, policy=ALLOW_ALL, **kw) -> GraphNode:
    return GraphNode(agent=agent, tools=TOOLS, policy=policy, **kw)


@pytest.fixture(autouse=True)
def _reset():
    CALLS.clear()
    yield


# --- python router -----------------------------------------------------------


async def test_two_node_flow_with_explicit_context_passing() -> None:
    triage = Scripted(FinalAnswer(text="needs fix in auth.py"))
    fixer = Scripted(
        ToolCall(tool="write_doc", args={"name": "auth.py", "text": "x"}, call_id="c"),
        FinalAnswer(text="fixed"),
    )

    def router(o: NodeOutcome) -> str | None:
        if o.node == "triage" and "needs fix" in (o.result.final_answer or ""):
            return "fixer"
        return None

    result = await run_graph(
        {"triage": node(triage, DENY_WRITES), "fixer": node(fixer)},
        "Repair the build",
        router=router,
        start="triage",
    )
    assert result.error is None
    assert result.final is not None and result.final.final_answer == "fixed"
    assert [o.node for o in result.path] == ["triage", "fixer"]
    # Context passing is explicit: the fixer's task carries the handoff.
    assert "Repair the build" in fixer.tasks[0]
    assert "[handoff from triage]" in fixer.tasks[0]
    assert "needs fix in auth.py" in fixer.tasks[0]


async def test_per_node_policy_is_a_permission_boundary() -> None:
    # The SAME write attempt: denied in the read-only node, allowed in fixer.
    triage = Scripted(
        ToolCall(tool="write_doc", args={"name": "a", "text": "x"}, call_id="c1"),
        FinalAnswer(text="blocked, handing off"),
    )
    fixer = Scripted(
        ToolCall(tool="write_doc", args={"name": "a", "text": "x"}, call_id="c2"),
        FinalAnswer(text="done"),
    )
    seen_denials: list[int] = []

    def router(o: NodeOutcome) -> str | None:
        seen_denials.append(o.denials)
        return "fixer" if o.node == "triage" else None

    result = await run_graph(
        {"triage": node(triage, DENY_WRITES), "fixer": node(fixer)},
        "write the doc",
        router=router,
        start="triage",
    )
    assert result.error is None
    assert seen_denials == [1, 0]  # denial count is a routing signal
    assert CALLS.get("write", 0) == 1  # executed once — in the node allowed to


async def test_max_transitions_bounds_cycles() -> None:
    def forever(o: NodeOutcome) -> str:
        return "a" if o.node == "b" else "b"

    def make():  # each visit consumes one FinalAnswer
        return Scripted(*[FinalAnswer(text="ping") for _ in range(10)])

    result = await run_graph(
        {"a": node(make()), "b": node(make())},
        "loop forever",
        router=forever,
        start="a",
        max_transitions=4,
    )
    assert result.error == "max_transitions exhausted (4)"
    assert result.transitions == 4
    assert result.final is not None  # last completed node's result is kept


async def test_router_returning_unknown_node_fails_cleanly() -> None:
    result = await run_graph(
        {"a": node(Scripted(FinalAnswer(text="x")))},
        "t",
        router=lambda o: "nope",
        start="a",
    )
    assert result.error is not None and "unknown node 'nope'" in result.error


# --- YAML --------------------------------------------------------------------

GRAPH_YAML = """
version: 1
start: triage
max_transitions: 6
edges:
  - from: triage
    when: { status: succeeded, answer_matches: "(?i)needs.fix" }
    to: fixer
  - from: triage
    to: done
  - from: fixer
    to: reviewer
  - from: reviewer
    when: { answer_matches: "(?i)approved" }
    to: done
  - from: reviewer
    to: fixer
"""


async def test_yaml_graph_review_loop() -> None:
    spec = compile_graph(GRAPH_YAML)
    assert spec.start == "triage"
    triage = Scripted(FinalAnswer(text="needs fix"))
    fixer = Scripted(FinalAnswer(text="patched v1"), FinalAnswer(text="patched v2"))
    reviewer = Scripted(FinalAnswer(text="rejected: style"), FinalAnswer(text="approved!"))

    result = await run_graph(
        {"triage": node(triage), "fixer": node(fixer), "reviewer": node(reviewer)},
        "fix the bug",
        router=spec,  # start + max_transitions come from the spec
    )
    assert result.error is None
    # triage -> fixer -> reviewer(reject) -> fixer -> reviewer(approve) -> done
    assert [o.node for o in result.path] == ["triage", "fixer", "reviewer", "fixer", "reviewer"]
    assert result.final is not None and result.final.final_answer == "approved!"


async def test_yaml_first_matching_edge_wins() -> None:
    spec = compile_graph(GRAPH_YAML)
    o = NodeOutcome(
        node="triage",
        result=__import__("lynx").RunResult(
            correlation_id="c", bundle_id="b", final_answer="all good"
        ),
        denials=0,
        transitions=0,
    )
    assert spec(o) is None  # fallback edge to done


async def test_yaml_denials_gt_routing() -> None:
    spec = compile_graph(
        """
start: worker
edges:
  - from: worker
    when: { denials_gt: 1 }
    to: privileged
  - from: worker
    to: done
"""
    )
    worker = Scripted(
        ToolCall(tool="write_doc", args={"name": "a", "text": "x"}, call_id="c1"),
        ToolCall(tool="write_doc", args={"name": "b", "text": "x"}, call_id="c2"),
        FinalAnswer(text="kept getting blocked"),
    )
    privileged = Scripted(FinalAnswer(text="done with power"))
    result = await run_graph(
        {"worker": node(worker, DENY_WRITES), "privileged": node(privileged)},
        "t",
        router=spec,
    )
    assert [o.node for o in result.path] == ["worker", "privileged"]
    assert result.path[0].denials == 2


def test_yaml_compile_errors() -> None:
    with pytest.raises(GraphCompileError, match="'start' is required"):
        compile_graph("edges: [{from: a, to: done}]")
    with pytest.raises(GraphCompileError, match="unknown predicate"):
        compile_graph("start: a\nedges: [{from: a, to: done, when: {answre_matches: x}}]")
    with pytest.raises(GraphCompileError, match="status must be one of"):
        compile_graph("start: a\nedges: [{from: a, to: done, when: {status: ok}}]")
    with pytest.raises(GraphCompileError, match="edges\\[0\\]"):
        compile_graph("start: a\nedges: [{from: a, to: done, when: {answer_matches: '('}}]")
    with pytest.raises(GraphCompileError, match="cannot route FROM"):
        compile_graph("start: a\nedges: [{from: done, to: a}]")
    with pytest.raises(GraphCompileError, match="max_transitions"):
        compile_graph("start: a\nmax_transitions: 0\nedges: [{from: a, to: done}]")


async def test_run_graph_validates_spec_targets_against_nodes() -> None:
    spec = compile_graph("start: a\nedges: [{from: a, to: missing}]")
    with pytest.raises(ValueError, match="unknown node"):
        await run_graph({"a": node(Scripted(FinalAnswer(text="x")))}, "t", router=spec)


# --- durability ---------------------------------------------------------------


async def test_graph_resume_replays_nodes_and_routing() -> None:
    spec = compile_graph(GRAPH_YAML)
    store = MemoryStore()
    nodes1 = {
        "triage": node(Scripted(FinalAnswer(text="needs fix"))),
        "fixer": node(Scripted(FinalAnswer(text="patched"))),
        "reviewer": node(Scripted(FinalAnswer(text="approved!"))),
    }
    first = await run_graph(nodes1, "fix it", router=spec, store=store, run_id="g1")
    assert first.error is None
    assert first.final is not None and first.final.final_answer == "approved!"

    # Re-invoke with agents that would CRASH if stepped: everything replays.
    nodes2 = {
        "triage": node(Scripted()),
        "fixer": node(Scripted()),
        "reviewer": node(Scripted()),
    }
    second = await run_graph(nodes2, "fix it", router=spec, store=store, run_id="g1")
    assert second.error is None
    assert second.final is not None and second.final.final_answer == "approved!"
    assert [o.node for o in second.path] == [o.node for o in first.path]
    for n in nodes2.values():
        assert n.agent.step_calls == 0  # no model re-calls anywhere

    # The graph journal holds one handoff record per hop.
    graph_records = await store.load("g1")
    assert [r.kind for r in graph_records] == ["handoff"] * len(first.path)


async def test_racing_graph_worker_superseded() -> None:
    spec = compile_graph("start: a\nedges: [{from: a, to: done}]")

    class StaleLoad(MemoryStore):
        async def load(self, run_id: str):
            # The graph's own journal looks empty, but another worker already
            # wrote hop 0 — child runs are unaffected in this scenario.
            if run_id == "g1":
                return []
            return await super().load(run_id)

    store = StaleLoad()
    from lynx import StepRecord
    from lynx.core.types import now_utc

    await MemoryStore.append(
        store,
        StepRecord(
            run_id="g1",
            seq=0,
            kind="handoff",
            idempotency_key="",
            body={"hop": 0, "node": "a", "next": None},
            timestamp=now_utc(),
        ),
    )
    result = await run_graph(
        {"a": node(Scripted(FinalAnswer(text="x")))},
        "t",
        router=spec,
        store=store,
        run_id="g1",
    )
    assert result.error is not None and result.error.startswith("superseded:")


async def test_graph_events_emitted() -> None:
    seen: list[Any] = []

    async def sink(ev):
        seen.append(ev)

    spec = compile_graph("start: a\nedges: [{from: a, to: done}]")
    await run_graph(
        {"a": node(Scripted(FinalAnswer(text="x")))},
        "t",
        router=spec,
        sinks=(sink,),
    )
    kinds = [e.kind for e in seen]
    assert "graph.started" in kinds
    assert "graph.handoff" in kinds
    assert "graph.finished" in kinds
    assert "run.started" in kinds  # node-level events flow to the same sinks
