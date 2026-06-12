# Concepts

Vocabulary for Lynx v2 in one page.

---

## Tool

An async function decorated with `@tool`. Attaches metadata via `__lynx_meta__`. Does not register globally.

```python
@tool(reversible=False, scope=("filesystem:write",))
async def shell(cmd: str) -> str: ...
```

A tool can have a `.shadow` twin — same signature, no side effects, used when policy returns `dry_run`.

## ToolSet

An immutable, explicit collection of tools. Built at the call site.

```python
tools = ToolSet.from_functions(shell, write_file, delete_file)
```

Operations return new ToolSets: `.with_tool(...)`, `.without_tool(...)`, `.union(...)`. Never mutated.

## ActionRequest

The agent's proposed tool call, normalized for policy evaluation. Frozen.

## Verdict

The five possible policy outcomes:

| Verdict | What the kernel does |
|---------|---------------------|
| `allow` | Calls the real tool function |
| `deny` | Returns a denial; agent sees `[denied]` as a tool result |
| `dry_run` | Calls the tool's `.shadow` twin; returns preview as the result |
| `approve_required` | Calls `on_approval(req)`; runs the action if granted |
| `transform` | Runs the tool with rewritten arguments |

## Decision

Frozen dataclass returned by the PDP. Includes the verdict, reason, matched rule IDs, and optional approvers / timeout / transform_args.

## Policy

A YAML document plus optional Python rules, compiled into a `PolicyBundle`. The bundle has a content-addressed `id` (first 16 hex chars of sha256 over the canonical compiled form: version + defaults + every rule body + every Python rule's (name, priority)). Pass to `run_agent` for the kernel to consult.

## PolicyBundle

Frozen, immutable. The `id` is a deterministic hash over the full compiled content — two policies that differ only in a rule's body produce different IDs, even when the rule names match. Surfaced in every event for attestation.

## PDP

The Policy Decision Point: `evaluate(bundle, request, context) -> Decision`. Pure function. Same inputs → same Decision. No I/O.

## Mediator

The Policy Enforcement Point: `mediate(request, decision, tools, on_approval) -> ActionResult`. Pure async function that dispatches by verdict.

## Run

Conceptually, one execution of `run_agent`. Not a stored entity — there is no `Run` class in v2. Each call generates a `correlation_id` (UUID4) that ties all its events together. (With a `RunStore`: a fresh run's `correlation_id` defaults to the `run_id`; any re-invocation gets `"<run_id>#<suffix>"` so `(correlation_id, seq)` never collides across attempts while staying groupable by prefix.)

## RunStore / StepRecord (durability, opt-in)

`RunStore` is the two-method protocol you implement over your own storage to make a run durable. Lynx ships no implementation — see the [integration cookbook](integration-cookbook.md) for Redis / Postgres / in-memory recipes.

```python
class RunStore(Protocol):
    async def append(self, record: StepRecord) -> None: ...   # MUST raise DuplicateRecord on a duplicate (run_id, seq)
    async def load(self, run_id: str) -> Sequence[StepRecord]: ...
```

`StepRecord` is one journal entry. `seq` is a per-record log offset (not a step number — the step lives in `body["step"]`); `(run_id, seq)` is the uniqueness key. Record kinds: `run.started`, `run.resumed`, `model.output`, `action.intent` (the write-ahead claim, journaled *before* execution), `action.result`, `final`.

The kernel derives everything from the journal on resume: journaled model outputs replay without re-calling the model; journaled results replay without re-executing the action; an `action.intent` without a matching `action.result` marks the action *uncertain* and policy re-decides it with `context.extra.uncertain_retry: true`. `DuplicateRecord` from the store means another worker owns the run; the kernel returns `error="superseded: ..."` without executing anything.

`replay(records)` (pure function) reconstructs a `RunView` of any journal; `idempotency_key(run_id, step, tool, args)` is the stable identity stamped on intent/result records.

## RunResult

Minimal frozen dataclass returned by `run_agent`:

```python
@dataclass(frozen=True, slots=True)
class RunResult:
    correlation_id: str
    bundle_id: str
    final_answer: str | None
    error: str | None
    steps_taken: int
```

No history. No event list. No persistent state.

## Sink

A callable taking one `AuditEvent` at a time. Lynx never buffers; sinks are fired per event.

```python
async def my_sink(event: AuditEvent) -> None: ...
```

Built-in: `stdout_sink`, `jsonl_sink`, `noop_sink`, `multi_sink`, `callback_sink`.

## ApprovalHandler

A callable taking one `ApprovalRequest` and returning an `ApprovalDecision`. Called synchronously by the kernel when policy returns `approve_required`.

```python
async def my_handler(req: ApprovalRequest) -> ApprovalDecision: ...
```

Built-in: `auto_approve`, `auto_deny`, `cli_prompt_approval`, `callback_approval`.

## AuditEvent

What the sinks receive. Frozen. Minimal.

```python
@dataclass(frozen=True, slots=True)
class AuditEvent:
    correlation_id: str       # UUID4 grouping events from one run
    bundle_id: str            # policy hash in effect
    seq: int                  # monotonic within the run
    kind: str                 # "step.proposed" / "policy.evaluated" / ...
    timestamp: datetime
    body: Mapping[str, Any]
```

No hash chain. No content addressing. Your sink decides retention.

## Event kinds

| Kind | When emitted |
|------|-------------|
| `run.started` | At the start of `run_agent` |
| `step.proposed` | Agent returned a `ToolCall` |
| `policy.evaluated` | PDP returned a Decision |
| `action.started` | Real tool about to run (allow / transform / approval-granted) |
| `action.dry_run` | Shadow about to run |
| `action.completed` | Real tool returned ok |
| `action.dry_run_completed` | Shadow returned ok — distinct so consumers don't conflate previews with side effects |
| `action.failed` | Real tool raised, OR shadow raised, OR unknown tool |
| `action.denied` | Policy denied — `deny` verdict, OR an `approve_required` verdict whose handler refused (or timed out, or raised) |
| `approval.requested` | `approve_required` verdict, before calling the handler |
| `approval.granted` | Handler returned `granted=True` |
| `approval.denied` | Handler returned `granted=False` |
| `run.succeeded` | Agent returned FinalAnswer (body has `replayed: true` when a completed run was resumed) |
| `run.failed` | Budget exhausted / agent.step raised / the RunStore failed mid-run |
| `run.resumed` | A journaled run was picked up again (store + same run_id) |
| `run.superseded` | This worker lost the journal race to another worker and exited without executing anything |
| `run.bundle_changed` | A resume is running under a different policy bundle than the journal was written with — warn-and-continue |
| `step.replayed` | A completed step was fed back from the journal — no policy re-evaluation, no execution |
| `action.uncertain` | An intent was journaled without a result in a prior attempt — the action *may* have executed; policy sees `context.extra.uncertain_retry: true` |

The last five only occur when a `RunStore` is passed to `run_agent`.

## Principal

Frozen. Who the agent is acting on behalf of.

```python
Principal(kind="user" | "service" | "agent", id="...", name="...")
```

## Budget

Frozen. Hard caps the kernel enforces between steps. Both fields are optional except `steps`, which defaults to 50:

```python
@dataclass(frozen=True, slots=True)
class Budget:
    duration_seconds: int | None = None
    steps: int | None = 50
```

The scheduler uses a monotonic clock for `duration_seconds`, so wall-clock NTP jumps cannot exhaust (or extend) the budget. Checks happen between steps; a single hung tool call is not interrupted by `duration_seconds` — use a tool-level timeout for that.

`run_agent`'s default for its `budget=` parameter is `Budget(steps=50, duration_seconds=600)` — a 50-step / 10-minute cap. Override per call to widen or tighten.

> v2.0 removed the `usd` and `tokens` fields that v1 carried: neither was enforced by the kernel. Token/spend accounting belongs in a sink (or an adapter wrapping the LLM call), not in the policy boundary.

## ExecutionContext

Frozen. Set by the kernel for each step:

```python
ExecutionContext(principal, environment, workspace, correlation_id, step_seq, timestamp, extra)
```

Policy rules can match on any field via `context.<field>`.

## Agent protocol

The single contract every agent must satisfy:

```python
class Agent(Protocol):
    async def step(self, conversation: tuple[Message, ...]) -> ToolCall | FinalAnswer: ...
```

The runtime never mutates the conversation; each step rebinds the tuple. No buffer is held outside the function.

## How the pieces fit

```
       Agent                              Real world
         │                                    ▲
         │ ToolCall                           │ ActionResult
         ▼                                    │
   ┌──────────────────────────────────────────────────────┐
   │  run_agent (single pure async function)             │
   │      build ActionRequest                            │
   │            ▼                                         │
   │      PDP → Decision           (pure)                │
   │            ▼                                         │
   │      Mediator (PEP)            (pure async)         │
   │            ▼  emit events                            │
   │      Sinks: stdout / jsonl / OTel / yours            │
   └──────────────────────────────────────────────────────┘
```

No `Runtime` class. No `Scheduler` class. No `ApprovalBroker`. No globals.
