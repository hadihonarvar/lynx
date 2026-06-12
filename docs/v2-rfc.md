# Lynx 2.0 — Design RFC

**Status:** ACCEPTED — implementation in progress
**Author:** Lynx contributors
**Date:** 2026-06-10
**Supersedes:** v1.0.x

---

## 1. Identity

> **Lynx 2.0 is a stateless, type-safe policy kernel for AI agent tool calls.**
> Pure functions over immutable values. No database. No globals. No leaks.
> Five verdicts. Streaming events to user-owned sinks. Hot-swappable per call.

What v2 is:
- A function: `(agent, tools, policy) → RunResult` plus events streamed to sinks
- An immutable data model (every type frozen, no mutation)
- A small library — ~2,500 lines of source, fully typed (`mypy src` runs in CI as an advisory check today; tightening to `--strict` and making it a hard gate is tracked for a follow-up release).

What v2 is NOT:
- Not a database. Not a runtime daemon. Not a control plane.
- Not the durability layer (that's Temporal / Restate). *(Since v2.2 the kernel can journal runs to a user-implemented `RunStore` for crash-resume and idempotent retries — but storage itself remains yours; Lynx still ships none.)*
- Not the observability stack (that's OTel / Prometheus / Datadog).
- Not the audit storage (that's whatever your sink writes to).
- Not the prompt-injection filter (that's NeMo / Guardrails AI / Lakera).

What survives from v1: the five verdicts, the YAML policy grammar, the adapter pattern, the shadow library, the subprocess sandbox.

What dies: persistent storage, cross-process resume, hash-chained audit, the singleton runtime, every module-level mutable variable.

---

## 2. Public API surface (the entire contract v2 ships)

```python
# === Core types — all frozen, all slots, all typed ===
class ToolMetadata: ...
class ToolDef: ...
class ToolSet:
    @classmethod
    def from_functions(cls, *fns: Callable[..., Awaitable[Any]]) -> "ToolSet": ...
    def with_tool(self, t: ToolDef) -> "ToolSet": ...
    def without_tool(self, name: str) -> "ToolSet": ...
    def union(self, other: "ToolSet") -> "ToolSet": ...
    def names(self) -> tuple[str, ...]: ...

class Principal: ...
class Budget: ...
class ExecutionContext: ...
class ActionRequest: ...
class Decision: ...
class ActionResult: ...
class AuditEvent: ...
class RunResult: ...

class Verdict(StrEnum): ALLOW, DENY, DRY_RUN, APPROVE_REQUIRED, TRANSFORM

# === Tool decoration ===
def tool(*, cost, reversible, scope, ...): ...  # attaches __lynx_meta__ to fn

# === Policy ===
class PolicyBundle: ...
def compile_policy(source: str, *, python_rules: tuple = ()) -> PolicyBundle: ...
def load_policy_file(path: Path, *, python_rules: tuple = ()) -> PolicyBundle: ...
def evaluate(bundle: PolicyBundle, req: ActionRequest, ctx: ExecutionContext) -> Decision: ...
def allow(...) -> Decision: ...
def deny(...) -> Decision: ...
def dry_run(...) -> Decision: ...
def approve_required(...) -> Decision: ...
def transform(...) -> Decision: ...

# === Sinks ===
class Sink(Protocol):
    async def __call__(self, event: AuditEvent) -> None: ...

def stdout_sink() -> Sink: ...
def jsonl_sink(handle: IO[str]) -> Sink: ...
def noop_sink() -> Sink: ...
def multi_sink(*sinks: Sink) -> Sink: ...
def callback_sink(fn: Callable[[AuditEvent], Awaitable[None]]) -> Sink: ...

# Deferred to v2.1 — each depends on an optional package and can be added
# without breaking the v2.0 API:
#   def otel_sink(tracer: Tracer) -> Sink: ...     # [sinks-otel]
#   def prometheus_sink(port: int) -> Sink: ...    # [sinks-prom]
#   def http_sink(url: str) -> Sink: ...           # [sinks-http]
#   def kafka_sink(...) -> Sink: ...               # [sinks-kafka]

# === Approvals ===
class ApprovalRequest: ...                    # frozen
class ApprovalDecision: ...                   # frozen

class ApprovalHandler(Protocol):
    async def __call__(self, req: ApprovalRequest) -> ApprovalDecision: ...

def auto_approve(approver: str = "auto") -> ApprovalHandler: ...
def auto_deny(reason: str) -> ApprovalHandler: ...
def cli_prompt_approval(approver: str = "local") -> ApprovalHandler: ...
def callback_approval(fn: Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]) -> ApprovalHandler: ...

# === Agent protocol ===
class Agent(Protocol):
    async def step(self, conversation: tuple[Message, ...]) -> ToolCall | FinalAnswer: ...

class Message: ...                            # frozen
class ToolCall: ...                           # frozen
class FinalAnswer: ...                        # frozen

# === THE entry point ===
async def run_agent(
    agent: Agent,
    task: str,
    *,
    tools: ToolSet,
    policy: PolicyBundle,
    sinks: Sequence[Sink] = (),
    on_approval: ApprovalHandler | None = None,   # defaults to auto_deny(...) inside
    budget: Budget = Budget(steps=50, duration_seconds=600),
    principal: Principal = Principal(kind="user", id="anonymous"),
    environment: str = "dev",
    workspace: str = ".",
    correlation_id: str | None = None,
) -> RunResult: ...
```

That's it. Everything else is internal.

### No more

- `Runtime` class — gone
- `runtime` singleton — gone
- `runtime.run/resume/approve/deny/get_run/get_steps/audit_chain/verify_audit/list_runs` — gone
- `enable_prometheus`, `enable_otel`, `trace_step` — replaced by sinks
- `get_registry`, `get_broker` — gone (no globals)
- `compute_idempotency_key`, `GENESIS_HASH` — gone
- `AuditEvent.id` content-hash, `AuditEvent.prev` chain — gone (sinks add their own correlation if they want)
- `Step.checkpoint_blob`, `Run.resume_token`, `Run.last_step_seq`, `RunStatus.PAUSED` — gone

---

## 3. Type system

### Hard requirements

- `mypy src` runs in CI as an advisory check; tightening to `--strict src tests` as a hard gate is a follow-up release goal
- Zero `Any` in the public API
- Every public function has explicit return type
- Every dataclass is `frozen=True, slots=True`
- Mutable containers in public types use `Mapping[K, V]` / `tuple[T, ...]`, never `dict[K, V]` / `list[T]`

### Internal use of `Any`

Allowed only at framework boundaries (adapter wrappers receiving SDK response objects). Marked with `# pyright: ignore[reportAny]` or similar so we can grep for it.

### Generics

```python
T = TypeVar("T")

class Sink(Protocol):
    async def __call__(self, event: AuditEvent) -> None: ...

class ApprovalHandler(Protocol):
    async def __call__(self, req: ApprovalRequest) -> ApprovalDecision: ...

class Agent(Protocol):
    async def step(self, conversation: tuple[Message, ...]) -> "ToolCall | FinalAnswer": ...
```

---

## 4. Immutability rules

### Frozen by construction

Every public dataclass:
```python
@dataclass(frozen=True, slots=True)
class Foo: ...
```

### Mutations return new values

```python
# Allowed — return a new value
new_tools = tools.with_tool(extra_tool_def)

# NOT allowed
tools.tools["x"] = extra_tool_def  # TypeError at runtime (MappingProxyType is read-only)
```

### Builders

For types built up across multiple steps (the conversation buffer inside `run_agent`), use local tuples that get re-bound, not mutated in place:

```python
async def run_agent(...) -> RunResult:
    conv: tuple[Message, ...] = (Message(role="user", content=task),)
    while True:
        action = await agent.step(conv)
        ...
        conv = (*conv, new_msg)   # rebind, not append
```

---

## 5. Memory leak prevention

### Bounded lifetimes

| Source | v1 problem | v2 solution |
|--------|-----------|-------------|
| Tool registry | Module-level dict that grows per import; never cleared | `ToolSet` is an immutable value created at call site; freed when call returns |
| Approval broker | Module-level dict that accumulates pending/resolved | Synchronous `on_approval` callback; no persistence |
| Python rules | Module-level list that grows per `@rule` import | Explicit `python_rules=(...)` argument to `compile_policy` |
| OTel tracer | Module-level reference held forever | User holds the `Tracer`; passes it into their own sink closure; no global ref. (See [integration cookbook](integration-cookbook.md) for the wiring pattern.) |
| Prometheus counters | Module-level `Counter`/`Histogram` objects | Same — user owns the registry; references it from inside their sink closure. |
| Conversation buffer | Lived in `Scheduler._loop` for the run duration | Same lifetime, but freed at `run_agent` return |
| Step checkpoint blob | Persisted to disk forever | Doesn't exist |
| Audit chain | Persisted to disk forever | Streamed; sinks fire-and-forget; user owns retention |

### No file handles owned by Lynx

Every sink that needs a file handle takes one from the user:

```python
# Lynx never opens this file. User does:
with open("audit.jsonl", "a") as f:
    sink = jsonl_sink(f)
    result = await run_agent(..., sinks=(sink,))
# File closed automatically.
```

### No subprocess refs held

The sandbox already cleans up via `tempfile.TemporaryDirectory()` context manager. No change needed.

---

## 6. Functional decomposition

### v1 (classes with state):
```python
class Scheduler:
    def __init__(self, store, bundle): ...
    async def start(self, agent, ...): self._loop(...)
    async def _loop(self, ...): self.store.save_step(...); self._audit(...)
```

### v2 (free functions):
```python
async def run_agent(
    agent: Agent, task: str, *, tools: ToolSet, policy: PolicyBundle, ...
) -> RunResult:
    """Pure-ish entry point. No class. No state."""
    ...

async def _do_step(
    request: ActionRequest, decision: Decision, tools: ToolSet, ...
) -> ActionResult:
    """Pure step execution. No store calls. No globals."""
    ...

async def _emit(sinks: tuple[Sink, ...], event: AuditEvent) -> None:
    """Fan out to all sinks. No buffering. Sink failures are logged to
    stderr; they do not abort the run."""
    results = await asyncio.gather(*(s(event) for s in sinks), return_exceptions=True)
    for sink_obj, outcome in zip(sinks, results, strict=True):
        if isinstance(outcome, BaseException):
            print(f"[lynx] sink failed: {outcome!r}", file=sys.stderr)
```

The mediator becomes a function:
```python
async def mediate(
    request: ActionRequest,
    decision: Decision,
    tools: ToolSet,
    on_approval: ApprovalHandler,
) -> ActionResult: ...
```

The PDP stays a function (already pure in v1):
```python
def evaluate(bundle: PolicyBundle, req: ActionRequest, ctx: ExecutionContext) -> Decision: ...
```

---

## 7. Sinks

### Protocol

```python
class Sink(Protocol):
    async def __call__(self, event: AuditEvent) -> None: ...
```

### Built-in sink factories

```python
def stdout_sink() -> Sink:
    """Pretty-print events. No state. Closes nothing."""
    async def sink(event: AuditEvent) -> None:
        print(_format(event))
    return sink

def jsonl_sink(handle: IO[str]) -> Sink:
    """One JSON line per event. User owns the file handle."""
    async def sink(event: AuditEvent) -> None:
        handle.write(canonical_json(event) + "\n")
    return sink

def noop_sink() -> Sink:
    """Discard everything. For tests."""
    async def sink(event: AuditEvent) -> None: pass
    return sink

def multi_sink(*sinks: Sink) -> Sink:
    """Fan out to several sinks concurrently. Failures in one don't kill the
    others; they're logged to stderr."""
    async def sink(event: AuditEvent) -> None:
        results = await asyncio.gather(
            *(s(event) for s in sinks), return_exceptions=True
        )
        for sub, outcome in zip(sinks, results, strict=True):
            if isinstance(outcome, BaseException):
                print(f"[lynx] sink failed: {outcome!r}", file=sys.stderr)
    return sink
```

### User-written sinks

Storage adapters (Postgres, Redis, Splunk, OTel, Datadog, S3, ...) are not
shipped in the kernel. Users write their own with the `Sink` protocol; the
[integration cookbook](integration-cookbook.md) has 5–15 line recipes that
show the wiring pattern for each. Lynx imports nothing from those packages
and stays at three runtime dependencies (`click`, `pyyaml`, `rich`).

### Events

```python
@dataclass(frozen=True, slots=True)
class AuditEvent:
    correlation_id: str            # UUID4 for this run
    bundle_id: str                 # content-addressed policy hash in effect
    seq: int                       # monotonic within run
    kind: str                      # "step.proposed" / "policy.evaluated" / ...
    timestamp: datetime
    body: Mapping[str, Any]        # frozen mapping
```

Event kinds (closed set):
- `run.started`
- `step.proposed`
- `policy.evaluated`
- `action.started`
- `action.dry_run` — shadow about to run
- `action.completed` — real tool returned ok
- `action.dry_run_completed` — shadow returned ok (distinct so consumers don't conflate previews with real side effects)
- `action.failed` — real tool raised / shadow raised / unknown tool
- `action.denied` — policy `deny` verdict, OR an `approve_required` whose handler refused / timed out / raised
- `approval.requested`
- `approval.granted`
- `approval.denied`
- `run.succeeded`
- `run.failed`

No more `run.paused`, `run.resumed` — those concepts don't exist in v2.

---

## 8. Approvals (synchronous)

### Protocol

```python
@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request: ActionRequest
    decision: Decision
    correlation_id: str

@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    granted: bool
    approver: str
    reason: str = ""

class ApprovalHandler(Protocol):
    async def __call__(self, req: ApprovalRequest) -> ApprovalDecision: ...
```

### Built-in handlers

```python
def auto_approve(approver: str = "auto") -> ApprovalHandler:
    async def h(req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(granted=True, approver=approver)
    return h

def auto_deny(reason: str) -> ApprovalHandler:
    async def h(req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(granted=False, approver="system", reason=reason)
    return h

def cli_prompt_approval(approver: str = "local") -> ApprovalHandler:
    async def h(req: ApprovalRequest) -> ApprovalDecision:
        # asyncio.to_thread so the blocking read doesn't freeze the event loop
        ans = await asyncio.to_thread(input, f"Approve {req.request.tool}? [y/N] ")
        return ApprovalDecision(granted=ans.lower() == "y", approver=approver)
    return h

def callback_approval(fn: Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]) -> ApprovalHandler:
    return fn
```

### Behavior

When policy returns `approve_required`, `mediate` calls `on_approval(req)` wrapped in `asyncio.wait_for(..., decision.timeout_seconds)`. The result determines what happens next:

- Handler returns `granted=True` → action runs as if `allow`
- Handler returns `granted=False` → returned as a denial
- Handler exceeds the timeout → auto-deny with `"approval handler timed out after Ns"`
- Handler raises → auto-deny with the exception class in the message

The `run_agent` loop blocks on the handler — no queue, no broker, no resume. Cross-process humans-in-the-loop (Slack, email, webhook) live inside the handler, which can take hours to return.

If a user wants cross-process approval (Slack, web UI), their handler does the cross-process wait:

```python
async def slack_approval(req: ApprovalRequest) -> ApprovalDecision:
    msg_id = await slack.post(f"Approve {req.request.tool}?")
    result = await slack.wait_for_button(msg_id, timeout=3600)
    return ApprovalDecision(granted=result == "approve", approver=result.user)
```

That's the user's code. Lynx is stateless.

---

## 9. ToolSet

### Construction

```python
@tool(reversible=False, scope=("filesystem:write",))
async def shell(cmd: str) -> str: ...

# After decoration, shell has shell.__lynx_meta__: ToolMetadataAttachment
# NOT registered globally.

@shell.shadow
async def _shell_shadow(cmd: str) -> dict: ...

# Build explicit immutable set at call site:
tools = ToolSet.from_functions(shell, write_file, delete_file)
```

### Operations (all return new ToolSet)

```python
tools.with_tool(another_tool_def)        # add one
tools.without_tool("shell")              # remove by name
tools.union(other_toolset)               # combine
tools.names()                            # tuple of names
tools.get("shell")                       # ToolDef or KeyError
```

### Immutability

```python
@dataclass(frozen=True, slots=True)
class ToolSet:
    tools: Mapping[str, ToolDef] = field(default_factory=lambda: types.MappingProxyType({}))
```

Internal `Mapping` is `MappingProxyType` (read-only view) so even `.tools["x"] = y` raises.

---

## 10. Policy

### YAML grammar — unchanged

```yaml
version: 1
defaults:
  on_missing_shadow: approve_required
  on_no_match: deny
predicates:
  is_destructive: ...
rules:
  - id: block-prod
    priority: 100
    match: { tool: shell, context.environment: prod }
    decision: deny
    reason: "..."
```

### Python rules — explicit, not global

```python
# v1 (DEPRECATED — module-level _python_rules accumulates)
@policy.rule(id="block-paths", priority=10)
def block_paths(req, ctx): ...

# v2 (explicit)
def block_paths(req: ActionRequest, ctx: ExecutionContext) -> Decision | None:
    if req.tool != "shell": return None
    if path_escapes(req.args.get("cmd", ""), ctx.workspace):
        return deny(reason="path escapes workspace")
    return None

bundle = compile_policy(yaml_source, python_rules=(block_paths,))
```

### Bundle ID

Content-addressed hash of the compiled bundle. Pinned per run. Surfaced in every event so downstream attestation has it.

---

## 11. Adapters (unchanged behavior, may need API tweaks)

| Adapter | Status |
|---------|--------|
| `ClaudeAgent` | Keep — already stateless. Update to take ToolSet explicitly. |
| `OpenAIAgent` | Keep — already stateless. Update to take ToolSet explicitly. |
| `LangGraphAgent` | Keep — minor type cleanup. |
| `CrewAIAgent` | Keep — minor type cleanup. |
| `register_mcp_server` | Becomes `mcp_tools(command)` — an async context manager that starts the MCP child process, yields a `ToolSet`, and tears the process down on exit. No global registration. |

---

## 12. CLI

Five commands. Everything else gone.

```bash
lynx --version
lynx init               # writes policy.yaml only (no .lynx/, no toml)
lynx run <script>       # runs a script that uses run_agent
lynx policy lint        # validate a YAML
lynx policy bundle-id   # print content-addressed ID
```

`lynx run` expects the script to define a `main()` coroutine. It just imports + runs it. Lynx doesn't introspect runs, doesn't store anything, doesn't track approvals — all of that is in the user's script.

---

## 13. Testing

### Coverage targets

- PDP: property tests via `hypothesis` proving determinism (same input → same Decision)
- Mediator: unit tests for each verdict's behavior
- `run_agent`: integration tests using `noop_sink` and `auto_approve` / `auto_deny`
- ToolSet: laws (associativity, idempotency, immutability)
- Sinks: contract tests (every sink takes an AuditEvent, returns None)
- Approval handlers: contract tests

### Test count target

v1 had 57 tests. v2 trims the surface; the suite focuses on the kernel + adapters + CLI. Actual count drifts as features land — don't quote a number. Drop everything related to: stores, audit chain, resume flow, broker behavior, the CLI commands that don't exist anymore.

### CI gates

- `mypy src` (advisory in 2.0; `--strict src tests` as a hard gate is tracked for a follow-up release)
- `ruff check src tests examples`
- `ruff format --check`
- `pytest` — must be < 2s suite
- Build wheel + sdist; install in clean venv; smoke-test `lynx --version`

---

## 14. Examples

12 examples, all rewritten for the new API. Numbering preserved for continuity:

| # | File | What it shows |
|---|------|---------------|
| 01 | `01_hello_allow.py` | `run_agent` + stdout_sink + auto_approve — simplest possible |
| 02 | `02_block_dangerous.py` | DENY verdict with stdout_sink |
| 03 | `03_preview_writes.py` | DRY_RUN with file shadow |
| 04 | `04_human_approval.py` | sync `cli_prompt_approval` handler — no resume needed |
| 05 | `05_real_llm_blocked.py` | ClaudeAgent / OpenAIAgent + ToolSet |
| 06 | `06_streaming_to_jsonl.py` | Replaces compliance-audit; shows jsonl_sink |
| 07 | `07_refund_workflow.py` | Three customers, three verdicts, three runs — each separate |
| 08 | `08_sql_transform.py` | TRANSFORM verdict |
| 09 | `09_fastapi_service.py` | `run_agent` inside a FastAPI endpoint; one ScriptedRefund per request; denials surface as HTTP 403 |
| 10 | `10_devops_assistant.py` | All five verdicts; full scenario |
| 11 | `11_flask_service.py` | Same as 09 but Flask (asyncio.run inside view) |
| 12 | `12_django_service.py` | Same as 09 but Django async view |

---

## 15. Migration story

There is none. v2.0 is a clean break.

- v1.0.x stays on PyPI forever (PyPI never deletes versions)
- v2.0.0 is a separate install; users opt in by upgrading
- README has a "Migrating from v1.x" section pointing users at the new API
- CHANGELOG has the complete v1→v2 diff
- No compat shim. No deprecation warnings (we'd need state to track them).

---

## 16. Non-goals for v2.0

Explicitly deferred to v2.x (no commitment, no schedule):
- Container sandbox mode
- MCP server / "lynx mcp serve" mode
- Skill governance primitives
- Memory governance primitives
- Hot policy reload (`watch=True`)
- Signed attestations via Sigstore
- TypeScript SDK

These are good ideas; they go on the roadmap but don't ship in 2.0.

---

## 17. Accepted

This RFC is the source of truth for the v2.0 implementation. Any change to the API surface during implementation requires updating this doc first.

Implementation order:
1. Kernel rewrite (types, policy, mediator, scheduler → `run_agent`)
2. Sinks + approvals modules
3. ToolSet + decorator update
4. Delete stores/, observability.py, runtime.py
5. CLI slim-down
6. Tests rewrite
7. Examples rewrite
8. Docs rewrite
9. pyproject + workflows update
10. Review (mypy strict, ruff, pytest, wheel)
11. Tag v2.0.0, push, PyPI publish
