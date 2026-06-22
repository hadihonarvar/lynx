"""Handoff graphs — orchestration where the edge IS a permission boundary.

Entirely optional: ``run_agent`` knows nothing about graphs, and a graph is
nothing but a loop of ``run_agent`` calls. Use this module when you want the
loop declarative; skip it and write the loop yourself when you don't — a
node is just a ``run_agent()`` call, so the hand-rolled version is ~10 lines.

Each node is one complete, policy-gated ``run_agent`` run with **its own
policy, tools, and budget** — the triage node reads, the fixer node writes,
the reviewer node reads again. Edges are pure predicates over the completed
run's outcome: status, final-answer regex, **denial counts** (a routing
signal only possible because policy is first-class), steps taken. The graph
is bounded by construction (``max_transitions``), sequential by design (no
fan-out, no live agent-to-agent messages — nodes communicate only through
completed results), and cycles are fine (fixer ↔ reviewer until approved).

Routing comes in two layers:

  * **Python first**: any ``Router`` — one callable
    ``(NodeOutcome) -> str | None`` (next node name, or None/"done" = stop).
  * **YAML for ops**: ``compile_graph(yaml)`` / ``load_graph_file(path)``
    compile an edge table into a ``GraphSpec`` (which *is* a Router).
    Malformed graphs fail at compile time with ``GraphCompileError``.

Durability composes: pass the same ``store=``/``run_id=`` you'd pass
``run_agent`` and every node run journals under a derived child run_id while
each routing decision journals as a ``handoff`` record under the graph's
run_id — a crashed three-node workflow resumes at the node it died in, and
two workers racing the same graph resolve to one winner (same unique
``(run_id, seq)`` rule, one level up).

Context passing — the part handoff systems usually fumble — is explicit:
the next node's task is built by ``compose_task(original_task, outcome)``
(default: the original task plus the previous node's final answer, clearly
marked). No hidden shared state. Routers must be pure functions of the
outcome: resume replays journaled routing decisions and re-derives the rest.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import yaml

from lynx.core.policy import LayeredPolicyBundle, PolicyBundle, _compile_safe_regex
from lynx.core.scheduler import run_agent
from lynx.core.types import (
    AuditEvent,
    Budget,
    Principal,
    RunResult,
    ToolSet,
    new_correlation_id,
    now_utc,
)
from lynx.durability import DuplicateRecord, StepRecord

if TYPE_CHECKING:
    import re

    from lynx.approvals import ApprovalHandler
    from lynx.cancel import Cancelled
    from lynx.durability import RunStore
    from lynx.executors import Executor
    from lynx.sdk import Agent
    from lynx.sinks import Sink

__all__ = [
    "GraphCompileError",
    "GraphNode",
    "GraphResult",
    "GraphSpec",
    "NodeOutcome",
    "Router",
    "compile_graph",
    "load_graph_file",
    "run_graph",
]

# Reserved terminal name in YAML edges and router return values.
DONE = "done"


class GraphCompileError(ValueError):
    """A graph YAML failed validation. Raised at compile time, never mid-run."""


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One node = one policy-gated ``run_agent`` call with its own powers.

    The per-node ``policy`` / ``tools`` / ``budget`` are the point: the edge
    between nodes is a permission boundary, not just a prompt handoff.
    """

    agent: Agent
    tools: ToolSet
    policy: PolicyBundle | LayeredPolicyBundle
    budget: Budget = field(default_factory=Budget)  # unlimited unless you cap it
    on_approval: ApprovalHandler | None = None


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    """What one node's run produced — everything a Router may route on."""

    node: str
    result: RunResult
    denials: int  # policy denials during this node's run (replay-stable)
    transitions: int  # hops completed before this node ran


@runtime_checkable
class Router(Protocol):
    """Pure routing function: which node works next?

    Return the next node's name, or ``None`` / ``"done"`` to stop. Must be a
    pure function of the outcome — resume re-derives routing from journaled
    decisions and replayed results, so side effects or hidden state here are
    out of contract.
    """

    def __call__(self, outcome: NodeOutcome) -> str | None: ...


@dataclass(frozen=True, slots=True)
class GraphResult:
    """The whole workflow's outcome. ``final`` is the last node's RunResult."""

    final: RunResult | None
    path: tuple[NodeOutcome, ...]
    transitions: int
    error: str | None = None  # max-transitions / unknown node / superseded / store failure


# ---------------------------------------------------------------------------
# YAML edge table -> GraphSpec (which IS a Router)
# ---------------------------------------------------------------------------

_EDGE_KEYS = frozenset({"from", "to", "when"})
_WHEN_KEYS = frozenset({"status", "answer_matches", "error_matches", "denials_gt", "steps_gt"})
_STATUSES = frozenset({"succeeded", "failed"})


@dataclass(frozen=True, slots=True)
class _CompiledEdge:
    src: str
    dst: str
    status: str | None
    answer_re: re.Pattern[str] | None
    error_re: re.Pattern[str] | None
    denials_gt: int | None
    steps_gt: int | None

    def matches(self, o: NodeOutcome) -> bool:
        r = o.result
        succeeded = r.error is None
        if self.status == "succeeded" and not succeeded:
            return False
        if self.status == "failed" and succeeded:
            return False
        if self.answer_re is not None and not self.answer_re.search(r.final_answer or ""):
            return False
        if self.error_re is not None and not self.error_re.search(r.error or ""):
            return False
        if self.denials_gt is not None and not o.denials > self.denials_gt:
            return False
        if self.steps_gt is not None and not r.steps_taken > self.steps_gt:
            return False
        return True


@dataclass(frozen=True, slots=True)
class GraphSpec:
    """A compiled edge table. Callable — a ``GraphSpec`` IS a ``Router``.

    First matching edge wins (file order), no matching edge means terminal.
    """

    start: str
    max_transitions: int
    edges: tuple[_CompiledEdge, ...]

    def __call__(self, outcome: NodeOutcome) -> str | None:
        for e in self.edges:
            if e.src == outcome.node and e.matches(outcome):
                return None if e.dst == DONE else e.dst
        return None

    def node_names(self) -> frozenset[str]:
        names = {e.src for e in self.edges} | {e.dst for e in self.edges}
        names.discard(DONE)
        return frozenset(names)


def compile_graph(text: str) -> GraphSpec:
    """Compile a graph YAML into a ``GraphSpec``. Fails loudly at compile time.

    Shape::

        version: 1
        start: triage
        max_transitions: 12          # optional, default 12 — mandatory bound
        edges:
          - from: triage
            when: { status: succeeded, answer_matches: "(?i)needs.fix" }
            to: fixer
          - from: triage
            to: done                  # fallback — first matching edge wins
          - from: fixer
            to: reviewer
          - from: reviewer
            when: { answer_matches: "(?i)approved" }
            to: done
          - from: reviewer
            when: { denials_gt: 2 }   # policy kept blocking it
            to: privileged
          - from: reviewer
            to: fixer                 # rejected -> loop back (cycles are fine)

    ``when`` predicates (all AND-ed): ``status`` (succeeded|failed),
    ``answer_matches`` / ``error_matches`` (safe regex — same ReDoS guard as
    policy), ``denials_gt``, ``steps_gt``.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GraphCompileError(f"invalid YAML: {exc}") from exc
    if not isinstance(doc, Mapping):
        raise GraphCompileError("graph file must be a mapping with 'start' and 'edges'")

    start = doc.get("start")
    if not isinstance(start, str) or not start:
        raise GraphCompileError("'start' is required and must be a node name")
    if start == DONE:
        raise GraphCompileError(f"'start' cannot be the reserved terminal {DONE!r}")

    max_transitions = doc.get("max_transitions", 12)
    if not isinstance(max_transitions, int) or max_transitions < 1:
        raise GraphCompileError("'max_transitions' must be a positive integer")

    raw_edges = doc.get("edges")
    if not isinstance(raw_edges, list) or not raw_edges:
        raise GraphCompileError("'edges' is required and must be a non-empty list")

    edges: list[_CompiledEdge] = []
    for i, raw in enumerate(raw_edges):
        where = f"edges[{i}]"
        if not isinstance(raw, Mapping):
            raise GraphCompileError(f"{where}: each edge must be a mapping")
        unknown = set(raw) - _EDGE_KEYS
        if unknown:
            raise GraphCompileError(
                f"{where}: unknown keys {sorted(unknown)}; allowed: from, to, when"
            )
        src, dst = raw.get("from"), raw.get("to")
        if not isinstance(src, str) or not src:
            raise GraphCompileError(f"{where}: 'from' is required")
        if not isinstance(dst, str) or not dst:
            raise GraphCompileError(f"{where}: 'to' is required")
        if src == DONE:
            raise GraphCompileError(f"{where}: cannot route FROM the terminal {DONE!r}")

        when = raw.get("when") or {}
        if not isinstance(when, Mapping):
            raise GraphCompileError(f"{where}: 'when' must be a mapping")
        unknown = set(when) - _WHEN_KEYS
        if unknown:
            raise GraphCompileError(
                f"{where}: unknown predicate(s) {sorted(unknown)}; allowed: {sorted(_WHEN_KEYS)}"
            )
        status = when.get("status")
        if status is not None and status not in _STATUSES:
            raise GraphCompileError(f"{where}: status must be one of {sorted(_STATUSES)}")
        try:
            answer_re = (
                _compile_safe_regex(when["answer_matches"]) if "answer_matches" in when else None
            )
            error_re = (
                _compile_safe_regex(when["error_matches"]) if "error_matches" in when else None
            )
        except Exception as exc:
            raise GraphCompileError(f"{where}: {exc}") from exc
        for key in ("denials_gt", "steps_gt"):
            if key in when and (not isinstance(when[key], int) or isinstance(when[key], bool)):
                raise GraphCompileError(f"{where}: {key} must be an integer")

        edges.append(
            _CompiledEdge(
                src=src,
                dst=dst,
                status=status,
                answer_re=answer_re,
                error_re=error_re,
                denials_gt=when.get("denials_gt"),
                steps_gt=when.get("steps_gt"),
            )
        )

    return GraphSpec(start=start, max_transitions=max_transitions, edges=tuple(edges))


def load_graph_file(path: str) -> GraphSpec:
    with open(path, encoding="utf-8") as handle:
        return compile_graph(handle.read())


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


def _default_compose_task(original_task: str, outcome: NodeOutcome) -> str:
    """Explicit context passing: original goal + the previous node's output."""
    prev = outcome.result.final_answer
    if prev is None:
        prev = f"(node failed: {outcome.result.error})"
    return f"{original_task}\n\n[handoff from {outcome.node}] previous result:\n{prev}"


async def run_graph(
    nodes: Mapping[str, GraphNode],
    task: str,
    *,
    router: Router | GraphSpec,
    start: str | None = None,
    max_transitions: int | None = None,
    compose_task: Callable[[str, NodeOutcome], str] | None = None,
    sinks: Sequence[Sink] = (),
    principal: Principal = Principal(kind="user", id="anonymous"),
    environment: str = "dev",
    workspace: str = ".",
    executor: Executor | None = None,
    store: RunStore | None = None,
    run_id: str | None = None,
    cancel: Cancelled | None = None,
) -> GraphResult:
    """Run a sequential, policy-bounded multi-node workflow.

    Args:
        nodes:        node name -> GraphNode (agent + ITS OWN tools/policy/budget).
        task:         the original goal; later nodes receive it re-composed
                      with the previous node's result (see ``compose_task``).
        router:       a ``Router`` callable or a compiled ``GraphSpec``.
        start:        first node. Defaults to ``router.start`` for a GraphSpec.
        max_transitions: hard hop bound. Defaults to ``router.max_transitions``
                      for a GraphSpec, else 12. Always enforced.
        compose_task: ``(original_task, outcome) -> next node's task``.
                      Default appends the previous node's final answer,
                      clearly marked. Context passing is explicit — there is
                      no hidden shared state between nodes.
        store/run_id: same durability contract as ``run_agent``. Node runs
                      journal under ``"<run_id>::<hop>:<node>"``; routing
                      decisions journal as ``handoff`` records under
                      ``run_id`` itself. Resume replays both; a racing graph
                      worker exits with ``error="superseded: ..."``.
        sinks/principal/environment/workspace/executor: passed to every node run.

    Returns:
        ``GraphResult`` — the last node's RunResult, the full path of
        ``NodeOutcome``s, and an error for max-transitions / unknown-node /
        superseded / store-failure terminations.
    """
    spec = router if isinstance(router, GraphSpec) else None
    start = start or (spec.start if spec else None)
    if not start:
        raise ValueError("start node is required (pass start= or use a GraphSpec)")
    if start not in nodes:
        raise ValueError(f"start node {start!r} not in nodes {sorted(nodes)}")
    if spec is not None:
        missing = spec.node_names() - set(nodes)
        if missing:
            raise ValueError(f"graph routes to unknown node(s): {sorted(missing)}")
    if store is not None and not run_id:
        raise ValueError("a non-empty run_id is required when store is provided")
    bound = (
        max_transitions if max_transitions is not None else (spec.max_transitions if spec else 12)
    )
    if bound < 1:
        raise ValueError("max_transitions must be >= 1")
    compose = compose_task or _default_compose_task

    gcid = run_id or new_correlation_id()
    seq = 0
    sinks_tuple = tuple(sinks)

    async def emit(kind: str, body: dict[str, Any]) -> None:
        nonlocal seq
        event = AuditEvent(
            correlation_id=gcid,
            bundle_id="graph",
            seq=seq,
            kind=kind,
            timestamp=now_utc(),
            body=body,
        )
        seq += 1
        for s in sinks_tuple:
            try:
                await s(event)
            except Exception:  # sinks never kill the run; node runs report loudly
                pass

    # ---- resume: load journaled routing decisions (kind="handoff")
    handoffs: dict[int, Mapping[str, Any]] = {}
    if store is not None:
        try:
            prior = await store.load(run_id or "")
        except Exception as exc:
            return GraphResult(
                final=None,
                path=(),
                transitions=0,
                error=f"store.load failed: {type(exc).__name__}: {exc}",
            )
        for rec in sorted(prior, key=lambda r: r.seq):
            if rec.kind == "handoff":
                handoffs[int(rec.body["hop"])] = rec.body

    await emit("graph.started", {"task": task, "start": start, "resumed": bool(handoffs)})

    current = start
    current_task = task
    hops = 0
    path: list[NodeOutcome] = []

    while True:
        # ---- kill-switch: stop between nodes (each node run also honors it)
        if cancel is not None and cancel.cancelled:
            reason = cancel.reason or "cancelled by caller"
            await emit("graph.cancelled", {"reason": reason, "hops": hops})
            return GraphResult(
                final=path[-1].result if path else None,
                path=tuple(path),
                transitions=hops,
                error=f"cancelled: {reason}",
            )

        if hops >= bound:
            await emit("graph.exhausted", {"max_transitions": bound})
            return GraphResult(
                final=path[-1].result if path else None,
                path=tuple(path),
                transitions=hops,
                error=f"max_transitions exhausted ({bound})",
            )

        node = nodes[current]
        denials = 0

        async def count_denials(event: AuditEvent) -> None:
            nonlocal denials
            # Replay-stable: a denied step counts whether it happens live
            # (action.denied) or replays from the journal (step.replayed
            # with a deny-shaped verdict and ok=False).
            if event.kind == "action.denied":
                denials += 1
            elif event.kind == "step.replayed":
                if event.body.get("ok") is False and event.body.get("verdict") in (
                    "deny",
                    "approve_required",
                ):
                    denials += 1

        child_run_id = f"{run_id}::{hops}:{current}" if store is not None else None
        result = await run_agent(
            node.agent,
            current_task,
            tools=node.tools,
            policy=node.policy,
            budget=node.budget,
            on_approval=node.on_approval,
            sinks=(*sinks_tuple, count_denials),
            principal=principal,
            environment=environment,
            workspace=workspace,
            executor=executor,
            store=store,
            run_id=child_run_id,
            cancel=cancel,
        )

        outcome = NodeOutcome(node=current, result=result, denials=denials, transitions=hops)
        path.append(outcome)

        if result.error is not None and result.error.startswith("cancelled:"):
            await emit("graph.cancelled", {"reason": result.error, "hops": hops})
            return GraphResult(final=result, path=tuple(path), transitions=hops, error=result.error)

        if result.error is not None and result.error.startswith("superseded:"):
            return GraphResult(
                final=result,
                path=tuple(path),
                transitions=hops,
                error=f"superseded: node {current!r} was overtaken by another worker",
            )
        if result.error is not None and "store." in result.error:
            return GraphResult(final=result, path=tuple(path), transitions=hops, error=result.error)

        # ---- route: replay the journaled decision for this hop, else ask
        # the router and journal what it said.
        recorded = handoffs.get(hops)
        if recorded is not None and recorded.get("node") == current:
            nxt: str | None = recorded.get("next")
        else:
            nxt = router(outcome)
            if nxt == DONE:
                nxt = None
            if store is not None:
                record = StepRecord(
                    run_id=run_id or "",
                    seq=hops,
                    kind="handoff",
                    idempotency_key="",
                    body={
                        "hop": hops,
                        "node": current,
                        "next": nxt,
                        "denials": denials,
                        "status": "failed" if result.error else "succeeded",
                    },
                    timestamp=now_utc(),
                )
                try:
                    await store.append(record)
                except DuplicateRecord:
                    await emit("graph.superseded", {"hop": hops})
                    return GraphResult(
                        final=result,
                        path=tuple(path),
                        transitions=hops,
                        error=f"superseded: another worker is executing graph {run_id!r}",
                    )
                except Exception as exc:
                    return GraphResult(
                        final=result,
                        path=tuple(path),
                        transitions=hops,
                        error=f"store.append failed: {type(exc).__name__}: {exc}",
                    )

        await emit(
            "graph.handoff",
            {
                "hop": hops,
                "node": current,
                "next": nxt,
                "denials": denials,
                "ok": result.error is None,
            },
        )

        if nxt is None:
            await emit("graph.finished", {"hops": hops + 1, "final_node": current})
            return GraphResult(final=result, path=tuple(path), transitions=hops)
        if nxt not in nodes:
            return GraphResult(
                final=result,
                path=tuple(path),
                transitions=hops,
                error=f"router returned unknown node {nxt!r}; nodes: {sorted(nodes)}",
            )

        current_task = compose(task, outcome)
        current = nxt
        hops += 1
