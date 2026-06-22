# Lynx

[![PyPI](https://img.shields.io/pypi/v/lynx-agent.svg?v=2.11.0)](https://pypi.org/project/lynx-agent/)
[![Python versions](https://img.shields.io/pypi/pyversions/lynx-agent.svg?v=2.11.0)](https://pypi.org/project/lynx-agent/)
[![License](https://img.shields.io/pypi/l/lynx-agent.svg)](https://github.com/hadihonarvar/lynx/blob/main/LICENSE)
[![CI](https://github.com/hadihonarvar/lynx/actions/workflows/ci.yml/badge.svg)](https://github.com/hadihonarvar/lynx/actions/workflows/ci.yml)

**A stateless, type-safe policy kernel for AI agent tool calls.**

Pure functions over immutable values. No database. No globals. No leaks. Five verdicts. Streaming events to user-owned sinks.

**Lynx is the governance and safety layer for your agent's loop — not the loop itself.** Every iteration of an agent loop proposes a tool call; Lynx checks each one (`allow / deny / dry_run / approve_required / transform`), audits it, and keeps the loop bounded — *before* it touches the real world. It is not an agent framework and won't write your loop's logic. Use it two ways:

- **Bring your own harness** (OpenAI Agents SDK, LangChain, CrewAI, PydanticAI) and drop `ToolGuard` into its tool calls — the framework drives the loop, Lynx governs each action inside it.
- **Or use `run_agent`** as a minimal, stateless loop of your own.

Either way you get the same five-verdict policy boundary on every action, plus the loop-control rails a harness needs: **budgets** (step/token/duration caps), a **kill-switch**, a **repetition gate** (breaks same-tool-same-args infinite loops), and **durable resume** (a crash mid-loop replays completed steps without re-running side effects).

```python
import asyncio
from lynx import (
    ToolSet, tool, load_policy_file, run_agent,
    stdout_sink, auto_deny,
)

@tool(reversible=False, scope=("filesystem:write",))
async def shell(cmd: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode()

result = await run_agent(
    my_agent,
    task="clean up old logs",
    tools=ToolSet.from_functions(shell),
    policy=load_policy_file("policy.yaml"),
    sinks=(stdout_sink(),),
    on_approval=auto_deny("no approvals configured"),
    environment="prod",          # policy can match on context.environment
    # principal=Principal(kind="user", id="hadi"),  # optional
    # workspace=".",                                 # optional
    # budget=Budget(steps=50, duration_seconds=600), # this IS the default; Budget.unlimited() to opt out
    # correlation_id=None,                           # auto-generated if None
)
# result: { correlation_id, bundle_id, final_answer, error, steps_taken }
# Lynx holds NOTHING. No DB. No state. No leaks.
```

## What Lynx does

- **Policy-gated execution** at the tool-call boundary. Five verdicts: `allow / deny / dry_run / approve_required / transform`.
- **Streaming events** to your sinks. We never store events — your sink can buffer, write to disk, ship to OTel, post to a webhook, whatever you choose.
- **Pure functions everywhere.** The kernel is one function: `run_agent(agent, task, *, tools, policy, sinks, on_approval, ...)`. No `Runtime` class. No singleton.
- **Immutable values.** Every public type is `frozen=True, slots=True`. Mutation raises at runtime; mypy catches it at write time.
- **No globals.** No tool registry, no broker, no module-level state. ToolSet is built explicitly at call site.
- **Hot-swappable policy.** Pass a different `PolicyBundle` on the next `run_agent` call — the bundle is an immutable value; the kernel holds nothing between calls. (Mid-run reload is not supported; build a new bundle and use it on the next run.)
- **Layered policy scopes** *(optional)*. Compose independent named policies — `compile_policy([PolicyLayer("org", …), PolicyLayer("team", …), PolicyLayer("user", …)])` — each evaluated on its own, then combined by a developer-chosen `Combiner`. Ships `strict_overrides_loose` (default, fail-closed: broadest layer sets a floor narrower layers can only tighten), `last_layer_wins` (most-specific layer may re-grant), and `first_layer_wins` — or bring your own for any trust model. Layers that match no rule abstain; provenance is layer-tagged (`team:block-http`). Mechanism, not policy: Lynx evaluates the layers; you decide who overrides whom. See example 39 and [`docs/02-policy-language.md`](docs/02-policy-language.md#layered-policy-scopes).
- **Obligations — "allow, *and also* do X"** *(optional)*. Attach mandatory side-actions to any verdict (the XACML/Cedar model), resolved against an `ObligationRegistry` you supply — the kernel ships none. A `pre` obligation runs *before* the action and **gates** it (handler fails → the tool never runs; *"refund only if a scoped credential issued"*); a `post` obligation runs after (notify-finance, write a special audit record). Unknown id or no registry → fail-closed; every obligation streams `obligation.required/fulfilled/failed` audit events. It is not a verdict — it rides on `allow`/`deny`/`transform`/etc. See [`docs/02-policy-language.md`](docs/02-policy-language.md#obligations--allow-and-also-do-x).
- **Durable runs, no double side effects** *(opt-in)*. Pass a `RunStore` you implement over your own storage and a stable `run_id`: a crashed run resumes at the first incomplete step — the model is not re-called for completed steps (no re-burned tokens) and journaled actions are not re-executed (no double charges). Two racing workers resolve to one winner; the loser exits `superseded` before executing anything.
- **Token metering and caps.** Adapters report per-step input/output token counts; the kernel streams them as `step.usage` events, totals them on `RunResult.usage`, and enforces `Budget(tokens=…, input_tokens=…, output_tokens=…)` between steps. The kernel counts and enforces counts — it never converts tokens to money; multiply by your own rates in a sink.
- **Token optimization (the compressor seam)** *(opt-in)*. Metering measures spend; this reduces it. Pass `compressor=` and every fresh tool result is shrunk *before* it enters the conversation, the journal, and any replay — so a 40 KB log dumped once isn't re-sent in full on every later step. Lynx ships pure-Python reference compressors (`truncate_compressor`, `dedup_compressor`, `compose_compressors`, `route_compressor` via `@tool(compress=…)`, `external_filter_compressor`) and **fails open** — a broken compressor never drops a real output. Lynx is *not* a token optimizer; it owns the seam where yours plugs in. Separately, the Claude adapter now enables Anthropic **prompt caching** (`cache_prompt=True`) so a long loop re-reads prior turns from cache instead of re-billing them. *(RTK — github.com/rtk-ai/rtk — has no stdin filter and is wired at the tool level: your shell tool runs `rtk <cmd>`.)*
- **Pluggable execution (the executor seam).** Every approved action flows through one `Executor` — in-process by default, a subprocess with rlimits, or *your* Docker/gVisor/E2B wrapper (one async callable). Route per-tool via `@tool(isolation="container")` + `route_executor({...})`, failing closed when a requested isolation has no route. Lynx defines the seam; the security boundary is whatever you plug in.
- **Handoff graphs** *(optional)*. Sequential multi-agent workflows where **the edge is a permission boundary**: each node is one `run_agent` call with its own policy/tools/budget, and edges route on outcomes — including **denial counts**. Bounded by construction (`max_transitions`), explicit context passing, YAML-declarable, durable via the same `RunStore`. Just sugar over a loop of `run_agent` calls — skip it and write the loop yourself anytime.
- **MCP proxy** *(optional)*. Put Lynx *in front of* any MCP server: the client (Claude Desktop/Code, Cursor, …) points at Lynx instead of the server, and every `call_tool` flows through the same `evaluate → mediate` path — `allow / deny / dry_run / approve_required / transform` — with an audit stream, **zero code change** on client or server. `serve_mcp_proxy(upstream, policy=…, sinks=…)` wires the stdio transport; `GovernedProxy` / `govern_call` are the transport-free, unit-testable core. See example 34. *(`pip install lynx-agent[mcp]`.)*
- **Framework-native governance** *(optional)*. When an agent *framework* owns the loop (OpenAI Agents SDK, LangChain, CrewAI, PydanticAI) instead of Lynx, drop a `ToolGuard` in front of its tool calls — `await guard.check(tool_name, args)` runs the same `evaluate → mediate` kernel and returns a `GovernedCall`, so all five verdicts work at the boundary with no proxy and no rewrite. This is the inverse of an **adapter** (`lynx.adapters`, where Lynx drives the loop): here the framework drives, Lynx governs each call inside it. `governed_function_tools(tools, policy=…)` turns a `ToolSet` into governed OpenAI Agents SDK tools in one line. See example 40. *(`pip install lynx-agent[openai-agents]` for the SDK shim; `ToolGuard` itself is stdlib-only.)*
- **Loop control & operability.** The rails that keep an agent loop bounded: a kill-switch (`cancel=CancelToken()`) checked at every step boundary and before each tool runs — a cancelled run stops after at most one more action; a repetition gate (`Budget(max_repeated_calls=)`) that breaks same-tool-same-args infinite loops; hard `Budget` caps (steps / tokens / duration); and per-step / per-tool timeouts.

## What Lynx does NOT do

- **No storage** — durability journals to a `RunStore` *you* implement on *your* Redis/Postgres/Dynamo (the contract is two methods and one sentence); audit events stream to *your* sinks. Lynx never opens a file or a connection.
- **No process supervision** — Lynx does not restart dead workers; your supervisor (systemd, k8s, a queue) does. Lynx makes the restart cheap and safe.
- **No prompt filtering** — that's [NeMo Guardrails](https://github.com/NVIDIA/NeMo-Guardrails) or [Guardrails AI](https://github.com/guardrails-ai/guardrails).
- **No cluster orchestration** — that's [Temporal](https://temporal.io) or [Inngest](https://www.inngest.com).
- **No agent framework** — that's [LangGraph](https://langchain-ai.github.io/langgraph/) / [CrewAI](https://www.crewai.com); we wrap them via adapters.

## Install

```bash
pip install lynx-agent                    # core (3 deps)
pip install lynx-agent[anthropic]         # Claude adapter
pip install lynx-agent[openai]            # GPT + any OpenAI-compatible provider
pip install lynx-agent[langgraph]
pip install lynx-agent[crewai]
pip install lynx-agent[openai-agents]     # govern the OpenAI Agents SDK (ToolGuard)
pip install lynx-agent[mcp]
pip install lynx-agent[otel]              # OpenTelemetry audit sink
```

The `[openai]` adapter also targets any **OpenAI-compatible** provider — Grok (xAI), Mistral, DeepSeek, Groq, OpenRouter, Together, Fireworks, Perplexity, Ollama — via one registry, and the *same policy* governs every one:

```python
from lynx.adapters.openai_compat import openai_compatible_agent
agent = openai_compatible_agent("deepseek", tools=tools, model="deepseek-chat")
# swap "deepseek" → "grok" / "mistral" / "groq" / … — run_agent(...) is unchanged
```

## Quickstart

```bash
pip install lynx-agent
lynx init           # writes one file: policy.yaml
python examples/01_hello_allow.py
```

## Documentation

| Doc | What's in it |
|---|---|
| [`docs/concepts.md`](docs/concepts.md) | The model end-to-end: the loop, the five verdicts, every seam, and how they compose |
| [`docs/02-policy-language.md`](docs/02-policy-language.md) | Full policy reference — YAML schema, operators, Python rules, layered scopes |
| [`docs/cli.md`](docs/cli.md) | Complete CLI reference — every command, flag, output line, and exit code |
| [`docs/cookbook.md`](docs/cookbook.md) | Copy-pasteable policy patterns (block `rm -rf`, tiered approvals, layered org/team/user) |
| [`docs/integration-cookbook.md`](docs/integration-cookbook.md) | Wiring recipes — sinks (SQLite/Postgres/OTel/Splunk/HTTP), durability stores, Slack approvals, `ToolGuard` in your framework |
| [`docs/what-lynx-is-and-isnt.md`](docs/what-lynx-is-and-isnt.md) | The boundary Lynx owns vs. what to compose it with |
| [`docs/faq.md`](docs/faq.md) | Common questions — performance, MCP, framework support, hot-reload, cleanup |
| [`docs/roadmap.md`](docs/roadmap.md) | Shipped vs. planned, by phase |

## How it works

```
                ┌────────────────────────────────────────────┐
                │  Agent (any framework)                     │
                └──────────────────┬─────────────────────────┘
                                   │  ToolCall
                                   ▼
              ╔═══════════════════════════════════════════╗
              ║  run_agent (pure function)                ║
              ║   1. PDP evaluates → Decision             ║
              ║   2. Mediator dispatches by verdict       ║
              ║   3. Sinks called with each AuditEvent    ║
              ║   4. Approval handler called sync if needed║
              ╚═══════════════════════════════════════════╝
                                   │ side effect
                                   ▼
                ┌────────────────────────────────────────────┐
                │  Real world                                │
                └────────────────────────────────────────────┘
```

Each agent step:
1. Build `ActionRequest` from the agent's `ToolCall`
2. `evaluate(policy, request, context)` returns a `Decision` (pure function)
3. `mediate(request, decision, tools, on_approval)` dispatches
4. Each step emits a few events; sinks consume them
5. Result is appended to a new `conversation` tuple; old tuple is freed

## Tools — `@tool` and `ToolSet`

Every tool is an `async def` decorated with `@tool`. The decorator attaches an
immutable `ToolDef` to the function (no global registry); you bundle decorated
functions into a `ToolSet` explicitly at the call site.

```python
from lynx import tool

@tool(
    cost="low",                     # "low" | "medium" | "high" (default "low")
    reversible=False,               # if False, dry_run requires a .shadow
    scope=("filesystem:write",),    # free-form tags policy can match on
    blast_radius_hint=None,         # int | None — opaque to the kernel; readable by your rules via declared.blast_radius_hint
    name=None,                      # override; default = fn.__name__
    description=None,               # override; default = first line of docstring
)
async def write_file(path: str, content: str) -> str:
    """Save text to a file."""
    Path(path).write_text(content)
    return f"wrote {len(content)} bytes to {path}"
```

### Shadows — pure previews for `dry_run`

If a tool is irreversible and policy chooses `dry_run`, the kernel calls the
**shadow** instead of the real function. Shadows must be pure (no I/O, no side
effects) and return a JSON-serializable preview.

```python
@write_file.shadow
async def _write_file_shadow(path: str, content: str) -> dict:
    p = Path(path)
    return {
        "would_write": path,
        "bytes": len(content.encode()),
        "would_overwrite": p.exists(),
        "preview": content[:120],
    }
```

If no shadow is registered and policy defaults `on_missing_shadow: approve_required`
(the default), an irreversible tool with no rule match falls through to approval
rather than running blind.

Alternative attachment form:

```python
from lynx import shadow

@shadow(write_file)
async def _write_file_shadow(path, content): ...
```

### `ToolSet` — immutable, built at call site

```python
from lynx import ToolSet

tools = ToolSet.from_functions(write_file, shell, get_customer)

tools.names()                        # ("get_customer", "shell", "write_file")
tools.get("write_file")              # ToolDef
tools.with_tool(other_def)           # returns NEW ToolSet
tools.without_tool("shell")          # returns NEW ToolSet
tools.union(other_toolset)           # returns NEW ToolSet
len(tools)                           # 3
```

Every operation returns a new `ToolSet`; the original is untouched.

## Policy — full reference

A policy is a frozen `PolicyBundle` produced by `compile_policy(yaml_str)` or
`load_policy_file(path)`. Bundles are content-addressed by `bundle.id` and safe
to hot-reload — the kernel holds no policy state between calls.

### YAML schema

```yaml
version: 1                        # int; currently only 1 is defined

defaults:
  on_no_match: deny               # verdict when no rule matches a request
  on_missing_shadow: approve_required
                                  # verdict when no rule matches AND the tool
                                  # is irreversible AND has no .shadow

predicates:                       # named, reusable matchers
  in_prod: { context.environment: prod }
  is_kubectl: { tool: kubectl }
  is_destructive_sql:
    tool: sql_exec
    args.sql.matches: '(?i)\b(UPDATE|DELETE)\b'

rules:
  - id: hard-block-rm-rf-root     # str; defaults to "rule_<index>"
    priority: 100                 # int; higher runs first (default 0)
    description: "..."            # optional, free-form
    match: { ... }                # see "Match expressions" below
    decision: deny                # one of the five verdicts
    reason: "rm -rf / is hard-blocked"
    approvers: ["sre-oncall@acme.com"]   # only used by approve_required
    timeout_seconds: 1800                # only used by approve_required
    transform: { ... }                   # only used by transform
```

Rules are sorted by `(-priority, file order)`. The first matching rule wins.
Python rules (see below) are interleaved with YAML rules by priority — a
higher-priority YAML rule beats a lower-priority Python rule, and vice versa.

### The five verdicts

| Verdict | What the mediator does |
|---|---|
| `allow` | Call `tool.fn(**args)` normally. |
| `deny` | Skip execution. Inject a `[denied]` tool message into the conversation. |
| `dry_run` | Call `tool.shadow_fn(**args)` instead of `fn`. Real side effects suppressed. |
| `approve_required` | Call `on_approval(...)` synchronously. On grant, proceed as `allow`; on deny, behave as `deny`. |
| `transform` | Rewrite `args` per the `transform:` block, then call `fn(**rewritten_args)`. |

### Match expressions

Match expressions read fields off the live `ActionRequest` and `ExecutionContext`.

**Paths** (the part before the operator):

| Path prefix | Reads from |
|---|---|
| `tool` | The tool name (string) |
| `args.<name>...` | The arguments the agent proposed |
| `declared.<name>` | Tool metadata: `cost`, `reversible`, `scope`, `blast_radius_hint`, `has_shadow` |
| `context.<name>` | `principal`, `environment`, `workspace`, `correlation_id`, `step_seq`, `timestamp`, `extra` |

**Operators** (suffix the path with `.<op>`):

| Operator | Meaning | Example |
|---|---|---|
| (none) / `.eq` | Equality | `tool: kubectl` |
| `.matches` | Regex `re.search` (RE2-style guards reject catastrophic backtracking) | `args.cmd.matches: '^rm\s+-rf'` |
| `.in` | Value is in the listed sequence | `args.customer_id.in: ["C-789"]` |
| `.contains` | Container contains the value | `declared.scope.contains: filesystem:write` |
| `.contains_any` | Container contains any listed value | `declared.scope.contains_any: [a, b]` |
| `.contains_all` | Container contains all listed values | `declared.scope.contains_all: [a, b]` |
| `.gt` `.ge` `.lt` `.le` | Numeric comparison | `args.amount_usd.gt: 500` |
| `.between` | `lo <= v <= hi` | `args.amount_usd.between: [50, 500]` |
| `.not_between` | Inverse of `between` | |

**Composition** at any level:

```yaml
match:
  all_of:
    - is_kubectl                       # named predicate
    - in_prod
    - args.command.matches: '^(apply|delete|patch)\b'
  # any_of: [ ... ]
  # not: { tool: shell }
```

### `transform:` block

```yaml
decision: transform
transform:
  jsonpath: "$.args.sql"               # default "$.args"; the target arg key
  append: " AND tenant_id = 'TENANT-A'" # one of: set | append | delete
```

- `set: <value>` — replace the value at `jsonpath`
- `append: <value>` — string-concatenate to the existing value
- `delete: true` — remove the key from `args`

### Python rules

Anything you can't express in YAML, write as a Python predicate. Rules are
explicit arguments to `compile_policy`; there is no decorator and no registry.

```python
from lynx import compile_policy
from lynx.policy import allow, deny, dry_run, approve_required, transform

def block_paths_outside_workspace(req, ctx):
    if req.tool != "shell":
        return None                                   # skip — let YAML decide
    if path_escapes(req.args["cmd"], ctx.workspace):
        return deny("path escapes workspace")
    return None

bundle = compile_policy(
    yaml_source,
    python_rules=(block_paths_outside_workspace,),
    python_rule_priorities=(("block_paths_outside_workspace", 100),),
)
```

Each Python rule is `(ActionRequest, ExecutionContext) -> Decision | None`.
Return `None` to defer; the first non-`None` result wins. Python and YAML
rules are interleaved in a single priority-sorted evaluation order (default
priority `0`). If a rule raises during evaluation, it is recorded as a
diagnostic marker in `Decision.matched_rules` (e.g. `<rule_error:my_rule:TypeError>`)
and evaluation continues — buggy rules never silently fail-open.

### Decision constructors

For Python rules and tests:

```python
from lynx.policy import allow, deny, dry_run, approve_required, transform

allow(reason="", matched_rules=())
deny(reason, matched_rules=())
dry_run(reason="", matched_rules=())
approve_required(approvers=(), timeout_seconds=1800, reason="", matched_rules=())
transform(transform_args={"sql": "..."}, reason="", matched_rules=())
```

### Default behavior when no rule matches

1. If the tool is **irreversible AND has no shadow** → `defaults.on_missing_shadow`
   (default `approve_required`).
2. Otherwise → `defaults.on_no_match` (default `deny`).

The matched rule id will be `"<default:on_missing_shadow>"` or
`"<default:on_no_match>"` so you can see the fall-through in audit events.

### Layered policy scopes

For org/team/user-style composition, pass a list of `PolicyLayer` to
`compile_policy` instead of one source. Each layer is evaluated independently and
a developer-chosen `Combiner` resolves disagreements:

```python
from lynx import PolicyLayer, compile_policy, last_layer_wins

bundle = compile_policy(
    [PolicyLayer("org", org_yaml), PolicyLayer("team", team_yaml), PolicyLayer("user", user_yaml)],
    merge=last_layer_wins,   # optional; defaults to strict_overrides_loose (fail-closed)
)
```

`strict_overrides_loose` (default) takes the most-restrictive verdict;
`last_layer_wins` lets the most-specific layer re-grant; `first_layer_wins` makes
the broadest authoritative — or pass your own `Combiner`. Non-matching layers
abstain; provenance is layer-tagged. Full reference:
[`docs/02-policy-language.md`](docs/02-policy-language.md#layered-policy-scopes).

### `run_agent` — all kwargs

```python
result = await run_agent(
    agent,                              # implements async step(conv) -> ToolCall | FinalAnswer
    task,                               # str — becomes the first user Message
    *,
    tools,                              # ToolSet
    policy,                             # PolicyBundle
    sinks=(),                           # Iterable[Sink]
    on_approval=None,                   # ApprovalHandler; defaults to auto_deny
    budget=Budget(steps=50, duration_seconds=600),
    principal=Principal(kind="user", id="anonymous"),
    environment="dev",                  # policy reads this via context.environment
    workspace=".",                      # policy reads this via context.workspace
    correlation_id=None,                # auto-generated UUID4 if None
)
```

## Sinks — the audit replacement

```python
from lynx import stdout_sink, jsonl_sink, multi_sink

# Pretty-print + persist to jsonl in one go
with open("audit.jsonl", "a") as f:
    sink = multi_sink(stdout_sink(), jsonl_sink(f))
    await run_agent(..., sinks=(sink,))
# File is yours. You close it. You rotate it. You ship it where you want.
```

Built-in sinks:

| Sink | What it does |
|------|-------------|
| `stdout_sink(stream=...)` | Pretty-print events |
| `jsonl_sink(handle)` | One JSON line per event |
| `hash_chained_sink(handle)` | One JSON line per event, **tamper-evident** (hash-chained) |
| `otel_sink(tracer=...)` | Emit each event as an OpenTelemetry span (`pip install lynx-agent[otel]`) |
| `noop_sink()` | Discard (for tests) |
| `multi_sink(*sinks)` | Fan out concurrently |
| `callback_sink(fn)` | Wrap any async callable |

Write your own — it's just `async def __call__(event: AuditEvent) -> None`.

### Tamper-evident audit

An audit log you can quietly edit isn't an audit log. `hash_chained_sink` is a
drop-in for `jsonl_sink` that fingerprints every line and chains it to the line
before it — `hash = sha256(prev_hash + canonical_json(event))` — so editing a
body, dropping a denial, or reordering events breaks every fingerprint
downstream. It's a pure sink (no kernel change, stdlib-only) and composes with
`multi_sink`.

```python
from lynx import hash_chained_sink, verify_chain

with open("audit.jsonl", "a") as f:
    await run_agent(..., sinks=(hash_chained_sink(f),))

verify_chain("audit.jsonl")   # VerifyResult(intact=True, lines=42, ...)
```

```console
$ lynx verify audit.jsonl
intact: 42 events, chain verified
# tamper with one line, then:
$ lynx verify audit.jsonl
broken at line 17: hash mismatch (line was modified)   # exits 1
```

This is tamper-*evident* (proves nobody altered the log). See example 37.

### OpenTelemetry

Already running OTel? `otel_sink` turns every governance decision into a span so
it lands in your existing backend (Datadog / Honeycomb / Grafana Tempo / Jaeger)
next to the rest of your telemetry — no custom plumbing. Each `AuditEvent`
becomes one short span named by `event.kind` with `lynx.*` attributes, and it
nests under the ambient trace automatically when the agent runs inside an
instrumented request. Stateless: every span is ended immediately, so nothing
accumulates over a long run.

```python
from lynx import otel_sink

await run_agent(..., sinks=(otel_sink(),))   # uses trace.get_tracer("lynx")
```

`pip install lynx-agent[otel]`. See example 38.

## Approvals — synchronous handlers

```python
from lynx import cli_prompt_approval, callback_approval, ApprovalDecision

# Built-in: prompt on stdin
await run_agent(..., on_approval=cli_prompt_approval())

# Or bring your own
async def slack_approval(req):
    msg = await slack.post(f"Approve {req.request.tool}?")
    button = await slack.wait_for_click(msg, timeout=3600)
    return ApprovalDecision(granted=button == "approve", approver=button.user)

await run_agent(..., on_approval=callback_approval(slack_approval))
```

The `run_agent` call blocks on the handler. No queue. No broker. No cross-process resume. Your handler decides how to wait.

## Durability — crash-resume without double side effects

Opt in by passing a `RunStore` (your storage, your dependency) and a stable `run_id`:

```python
result = await run_agent(
    agent, task,
    tools=tools, policy=policy,
    store=my_store,                 # you implement two methods (below)
    run_id="invoice-2026-0611",     # stable across retries
)
# Process dies mid-run? Your supervisor retries the same call.
# Completed steps replay from the journal: the model is NOT re-called,
# journaled actions are NOT re-executed. A finished run returns the same
# answer forever. Two racing workers resolve to one; the loser returns
# error="superseded: ..." having executed nothing.
```

The whole `RunStore` contract:

```python
class MyStore:                       # Redis / Postgres / Dynamo / a dict
    async def append(self, record: StepRecord) -> None:
        # MUST atomically raise DuplicateRecord if (run_id, seq) exists.
        # Postgres: PRIMARY KEY (run_id, seq). Redis: HSETNX. That's it.
        ...
    async def load(self, run_id: str) -> Sequence[StepRecord]:
        ...                          # ordered by seq
```

That one uniqueness rule is the concurrency story: the write-ahead intent
journaled before every action *is* the claim — no leases, no TTLs, nothing
to clean up when a worker dies. See
[`examples/24_durable_resume.py`](examples/24_durable_resume.py) for a
complete ~15-line store plus crash, resume, and supersede in action, and
[`docs/integration-cookbook.md`](docs/integration-cookbook.md) for Redis /
Postgres / file-backed recipes.

**The crash window, handled honestly.** If a worker dies *between* executing
an action and journaling its result, the action *may* have run. On resume,
Lynx re-proposes it to policy with `context.extra.uncertain_retry: true` —
so your policy decides: re-run it (idempotent tools), deny it, or escalate
to a human:

```yaml
- id: never-rerun-uncertain-payments
  match: { context.extra.uncertain_retry: true, declared.reversible: false }
  decision: approve_required
```

Inspect any journal with `replay(records)` (pure function) or `lynx trace
records.jsonl` (for file-backed stores).

## Execution isolation — the executor seam

Policy decides *whether* an action runs; the executor decides *where and
how*. By default approved tools run in-process. Pass an `Executor` and all
real execution (allow / transform / approval-granted) flows through it
instead:

```python
from lynx import inline_executor, route_executor, subprocess_executor

@tool(reversible=False, scope=("compute:exec",), isolation="container")
async def run_code(snippet: str) -> str: ...

result = await run_agent(
    agent, task, tools=tools, policy=policy,
    executor=route_executor({
        None:        inline_executor(),        # default route
        "subprocess": subprocess_executor(),   # rlimits — crash protection
        "container":  my_docker_executor,      # YOURS (~20 lines, see cookbook)
    }),
)
```

A custom executor is one async callable — `(request, tool) -> ActionResult`
— so Docker, gVisor, Firecracker, E2B, or Modal plug in without Lynx
shipping any of them as dependencies. Routing **fails closed**: a tool that
declares `isolation="microvm"` when no microvm route exists gets a failed
action, never a silent fallback to the host. Dry-runs bypass the seam
(shadows are side-effect-free by contract), and a raising executor fails
the action — never the run.

Honesty, as always: Python has no reliable in-language sandbox, and
`subprocess_executor()` is **crash/runaway protection, not a security
boundary** (see [SECURITY.md](SECURITY.md)). Lynx is the chokepoint where
isolation attaches; the boundary itself is whatever you put behind the
seam — the same stance as "you bring the database."

## Handoff graphs — the edge is a permission boundary

Optional, and deliberately thin: a node is just a `run_agent()` call, so the
graph module is declarative sugar over a loop you could write yourself.
What it adds is the part multi-agent frameworks fumble — **enforced role
boundaries** and bounded, explicit routing:

```python
from lynx import GraphNode, compile_graph, run_graph

nodes = {
    "triage":   GraphNode(agent=triage,   tools=tools, policy=read_only),
    "fixer":    GraphNode(agent=fixer,    tools=tools, policy=can_write),
    "reviewer": GraphNode(agent=reviewer, tools=tools, policy=read_only),
}
graph = compile_graph("""
start: triage
max_transitions: 8                  # mandatory bound — runaway loops impossible
edges:
  - { from: triage,   when: { answer_matches: "(?i)needs fix" }, to: fixer }
  - { from: triage,   to: done }
  - { from: fixer,    to: reviewer }
  - { from: reviewer, when: { answer_matches: "(?i)approved" },  to: done }
  - { from: reviewer, when: { denials_gt: 2 }, to: privileged }  # policy as a routing signal
  - { from: reviewer, to: fixer }   # rejected → loop back; cycles are fine
""")
result = await run_graph(nodes, "Fix the bug", router=graph)
```

- **Per-node policy is enforced, not prompted**: if the triage model tries to
  write, *its node's policy denies it* — the orchestrator can't bypass its
  role (the failure mode every role-based framework suffers).
- **Denial counts route**: `denials_gt` is a predicate no other orchestrator
  has, because nobody else makes policy first-class.
- **Context passing is explicit**: the next node's task = the original goal +
  the previous node's result, clearly marked (`compose_task=` to customize).
  No hidden shared state, no live agent-to-agent messages, sequential only.
- **Python first**: skip YAML entirely — any `(NodeOutcome) -> str | None`
  callable is a `Router`.
- **Durability composes**: pass `store=`/`run_id=` and node runs + routing
  decisions journal; a crashed 3-node workflow resumes at the node it died
  in, and racing graph workers resolve to one winner.

See [`examples/27_handoff_graph.py`](examples/27_handoff_graph.py) for the
triage → fixer ⇄ reviewer loop with an enforced role boundary.

## Token usage & budgets

Adapters (`ClaudeAgent`, `OpenAIAgent`) attach a `Usage` record to every model
step — input/output/cache token counts plus the model name. The kernel then:

```python
result = await run_agent(
    agent, task, tools=tools, policy=policy,
    budget=Budget(
        steps=50,
        duration_seconds=600,
        input_tokens=500_000,       # separate caps — input and output
        output_tokens=100_000,      # are priced differently
        tokens=550_000,             # or one combined cap
        step_timeout_seconds=120,   # a hung model call fails, never hangs
    ),
    sinks=(my_cost_sink,),        # step.usage events stream here
)
result.usage   # Usage(input_tokens=..., output_tokens=...) — lifetime totals
```

- **`step.usage` events** carry per-step counts + running totals — your sink
  multiplies by *your* rates for dollars, alerts, and attribution
  (per-customer = group by `correlation_id`). Lynx ships no price tables;
  they go stale weekly and your negotiated rates aren't list rates.
- **Caps are enforced between steps**, exactly like `steps` — when crossed,
  the run stops with `error="output token budget exhausted (…)"`. Honest
  caveat: like every in-loop limiter, a cap stops the *next* model call; the
  step that crossed the line already happened.
- **Unmetered agents are unmetered.** A hand-rolled `Agent` that attaches no
  `usage` produces no events and no enforcement — Lynx enforces what it can
  see and nothing else. With durability, journal-replayed steps count toward
  totals and caps (they were real spend in a prior attempt).

Scope, honestly: Lynx does not restart dead processes (your supervisor does);
durability needs no database, but *distributed* durability — runs surviving
machine loss, multiple workers — needs *your* database. Budgets count
replayed steps (resume a budget-exhausted run by passing a larger budget);
`duration_seconds` is per-attempt. Tool args/results should be
JSON-serializable (LLM tool calls always are). Resuming under a different
policy emits a `run.bundle_changed` warning; resuming with a different
ToolSet, or with an agent that isn't a pure function of the conversation
(e.g. the single-shot CrewAI adapter), is out of contract.

## Examples

| # | File | What it shows |
|---|------|--------------|
| 01 | [`01_hello_allow.py`](examples/01_hello_allow.py) | Smallest possible run |
| 02 | [`02_block_dangerous.py`](examples/02_block_dangerous.py) | DENY for `rm -rf /` |
| 03 | [`03_preview_writes.py`](examples/03_preview_writes.py) | DRY_RUN with file shadow |
| 04 | [`04_human_approval.py`](examples/04_human_approval.py) | Sync approval via stdin |
| 05 | [`05_real_llm_blocked.py`](examples/05_real_llm_blocked.py) | Real Claude / GPT |
| 06 | [`06_streaming_to_jsonl.py`](examples/06_streaming_to_jsonl.py) | Audit replacement: jsonl sink |
| 07 | [`07_refund_workflow.py`](examples/07_refund_workflow.py) | Multi-tier refund rules |
| 08 | [`08_sql_transform.py`](examples/08_sql_transform.py) | TRANSFORM verdict |
| 09 | [`09_fastapi_service.py`](examples/09_fastapi_service.py) | FastAPI integration |
| 10 | [`10_devops_assistant.py`](examples/10_devops_assistant.py) | All five verdicts (one policy, run in staging + prod) |
| 11 | [`11_flask_service.py`](examples/11_flask_service.py) | Flask integration |
| 12 | [`12_django_service.py`](examples/12_django_service.py) | Django integration |
| 13 | [`13_python_rules.py`](examples/13_python_rules.py) | Python rules + `<rule_error:…>` diagnostics |
| 17 | [`17_shadow_helpers.py`](examples/17_shadow_helpers.py) | Built-in fs/http/shell/sql shadows |
| 18 | [`18_sandboxed_tool.py`](examples/18_sandboxed_tool.py) | `subprocess_executor` resource caps |
| 24 | [`24_durable_resume.py`](examples/24_durable_resume.py) | Crash → resume, never double-charge |
| 26 | [`26_executor_seam.py`](examples/26_executor_seam.py) | Bring-your-own sandbox (`route_executor`) |
| 27 | [`27_handoff_graph.py`](examples/27_handoff_graph.py) | Handoff graph — the edge is a policy boundary |
| 32 | [`32_token_optimization.py`](examples/32_token_optimization.py) | Compressor seam |
| 33 | [`33_subagents.py`](examples/33_subagents.py) | A tool that runs an agent |
| 34 | [`34_mcp_proxy.py`](examples/34_mcp_proxy.py) | Govern any MCP server, zero code change |
| 35 | [`35_multi_provider.py`](examples/35_multi_provider.py) | One policy, any model provider |
| 36 | [`36_fastmcp_governed.py`](examples/36_fastmcp_governed.py) | Build with FastMCP, govern with Lynx |
| 37 | [`37_tamper_evident_audit.py`](examples/37_tamper_evident_audit.py) | Hash-chained audit + `verify_chain` |
| 38 | [`38_otel_audit.py`](examples/38_otel_audit.py) | OpenTelemetry audit sink |
| 39 | [`39_layered_policy.py`](examples/39_layered_policy.py) | Layered policy scopes + combiners |
| 40 | [`40_framework_native_governance.py`](examples/40_framework_native_governance.py) | `ToolGuard` — govern a framework's tool calls |

All 40 with one-line descriptions: [`examples/README.md`](examples/README.md).

## CLI — seven commands

```
lynx --version
lynx init [--dir <path>] [--force]    # write a starter policy.yaml (creates the dir)
lynx run <script>                     # run a script's async main()
lynx verify <audit.jsonl>             # check a hash-chained audit log (exits 1 if broken)
lynx trace <records.jsonl> [--run-id <id>]   # reconstruct a durable run journal
lynx policy lint [path]               # compile-check a policy + rule summary (default policy.yaml)
lynx policy bundle-id [path]          # print a policy's content-addressed id
```

Every command also has `--help`. Full reference — flags, output lines, exit
codes, and the audit-log-vs-run-journal distinction — in
[`docs/cli.md`](docs/cli.md).

## Status

**Public API committed; SemVer.** Production-ready for the documented scope. The kernel — `run_agent`, the five verdicts, the policy language, the sink/executor/compressor/durability seams — is stable; new capabilities land as additive seams and adapters (recent: the MCP proxy, OpenAI-compatible providers, FastMCP), never as breaking changes to that core.

## Design

- [`docs/concepts.md`](docs/concepts.md) — vocabulary
- [`docs/what-lynx-is-and-isnt.md`](docs/what-lynx-is-and-isnt.md) — what Lynx owns vs. what it composes with (mem0/Zep, Langfuse, LiteLLM, MCP gateways, Temporal)
- [`docs/cookbook.md`](docs/cookbook.md) — policy patterns (YAML)
- [`docs/integration-cookbook.md`](docs/integration-cookbook.md) — wiring patterns for sinks (SQLite / Postgres / Splunk / OTel / HTTP) + approval handlers (Slack / email / webhook) + durability `RunStore` backends (Redis / Postgres / files / Temporal)
- [`docs/faq.md`](docs/faq.md) — common questions

## License

Apache 2.0.
