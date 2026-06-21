# examples/

A learning path of 33 examples. Each is **self-contained** and starts with a plain-language SCENARIO explaining the problem it solves.

Read 01–12 in order for the core narrative; 13–33 cover every remaining feature for full coverage.

```
SIMPLE          01 → 02 → 03         "see the system working"
MORE COMPLEX    04 → 05 → 06         "approvals, real LLMs, streaming audit"
ADVANCED        07 → 08 → 09         "production patterns: rules, transforms, web service"
COMPLETE        10                   "the full thing — one realistic DevOps scenario"
INTEGRATIONS    11 (Flask) 12 (Django)   "drop Lynx into your existing web framework"
FULL COVERAGE   13 → 33              "every feature: python rules, transform ops,
                                       custom sinks, cross-process approval,
                                       shadow helpers, sandbox, hot-swap,
                                       MCP, LangGraph, CrewAI, error model,
                                       durable crash-resume, token budgets,
                                       executor seam (BYO sandbox),
                                       handoff graphs, memory gating,
                                       cost attribution, full-stack capstone,
                                       kill-switch + repetition gate,
                                       token-optimization compressor seam,
                                       subagents (run-inside-run)"
```

## Features at a glance — with code

The eight things Lynx does, in priority order, each in its minimal form.
Every snippet is the real API trimmed to its essence; the **example** column
links to a runnable version.

### 1. A safety gate on every agent action
Every real action is checked against rules your team wrote and reviewed —
allow, block, preview, rewrite, or ask a human. The agent can't skip the gate.

```yaml
# policy.yaml — reviewed in a PR like any code
rules:
  - { id: no-root-rm, match: { tool: shell, args.cmd.matches: "rm -rf /" }, decision: deny }
```
```python
result = await run_agent(agent, "clean up old logs",
                         tools=tools, policy=load_policy_file("policy.yaml"))
# the agent's `rm -rf /` is blocked; it sees "[denied] ..." and adapts
```
→ examples **01, 02, 10**

### 2. Never do damage twice
If a run crashes and gets retried, finished steps replay from your journal —
customers aren't double-charged, AI work isn't paid for twice.

```python
result = await run_agent(agent, task, tools=tools, policy=policy,
                         store=my_store,            # 2 methods on YOUR Redis/Postgres
                         run_id="invoice-778")      # stable across retries
# crash → your queue retries the same call → the charge happens exactly once
```
→ example **24**

### 3. A complete audit trail — kept in *your* systems
Every proposal, decision, and outcome streams to the tools you already own.
Lynx itself stores nothing.

```python
with open("audit.jsonl", "a") as f:
    await run_agent(agent, task, tools=tools, policy=policy,
                    sinks=(jsonl_sink(f),))   # every event → your file/SIEM/OTel
```
→ examples **06, 15**

### 4. Humans stay in the loop where it matters
High-stakes actions pause for sign-off (with enforced time limits); risky-but-
routine ones can show a preview before anything is touched.

```yaml
  - { id: big-refunds-ask, match: { tool: refund, args.amount.gt: 100 }, decision: approve_required }
  - { id: preview-deletes, match: { tool: delete_file }, decision: dry_run }
```
```python
await run_agent(agent, task, tools=tools, policy=policy,
                on_approval=cli_prompt_approval())   # or your Slack handler
```
→ examples **03, 04, 16**

### 5. Cost visibility and hard spending brakes
Every step's AI usage is measured and attributable; runaway loops stop before
the bill becomes a story.

```python
result = await run_agent(agent, task, tools=tools, policy=policy,
                         budget=Budget(output_tokens=50_000),  # hard brake
                         sinks=(my_cost_sink,))                # per-step usage → your rates
result.usage   # Usage(input_tokens=..., output_tokens=...) — the receipt
```
→ examples **25, 30**

### 6. Run risky work in a contained place
Approved actions run in-process, in a throwaway process, or inside *your*
sandbox — chosen per tool, refusing to run if the required isolation is missing.

```python
@tool(scope=("compute:exec",), isolation="container")
async def run_code(snippet: str) -> str: ...

await run_agent(agent, task, tools=tools, policy=policy,
                executor=route_executor({
                    None: inline_executor(),
                    "container": my_docker_executor,   # ~20 lines, yours
                }))
# a tool asking for isolation you didn't provide fails closed — never on the host
```
→ examples **18, 26**

### 7. Multi-agent teamwork with enforced job descriptions
Workflows where each agent's permissions are *enforced*, not suggested. An
agent that oversteps its role is blocked and the work routes onward; loops are capped.

```python
nodes = {
    "triage":   GraphNode(agent=triage,   tools=tools, policy=read_only),
    "fixer":    GraphNode(agent=fixer,    tools=tools, policy=can_write),
    "reviewer": GraphNode(agent=reviewer, tools=tools, policy=read_only),
}
graph = compile_graph("""
start: triage
max_transitions: 8
edges:
  - { from: triage,   when: { answer_matches: "needs fix" }, to: fixer }
  - { from: fixer,    to: reviewer }
  - { from: reviewer, when: { answer_matches: "approved" },  to: done }
  - { from: reviewer, to: fixer }
""")
result = await run_graph(nodes, "Fix the bug", router=graph)
# if triage tries to write, ITS policy blocks it — role enforced, not prompted
```
→ examples **27, 28**

### 8. Works with what you already have
Plugs into the frameworks teams already use, and brings zero infrastructure of
its own — no database, no server, three small dependencies.

```bash
pip install lynx-agent                       # 3 dependencies, no server, no DB
```
```python
agent = ClaudeAgent(tools=tools)             # or OpenAIAgent / LangGraphAgent /
result = await run_agent(agent, task,        #    CrewAIAgent / mcp_tools / your own
                         tools=tools, policy=policy)
```
→ examples **05, 20, 21, 22**

### It all composes — one function call
Every feature above is a keyword argument on a single call (this is example 28
in miniature):

```python
result = await run_graph(nodes, task, router=graph,        # 7: teamwork
                         executor=executor,                # 6: containment
                         sinks=(audit, cost_sink),         # 3 + 5: audit & cost
                         store=store, run_id="ticket-42")  # 2: never twice
# 1 & 4 live in each node's policy YAML; 8 is whatever agents you brought
```
→ example **28** (the full-stack capstone)

## The 33 examples

| # | File | Verdict shown | Problem in one line |
|---|------|--------------|---------------------|
| 01 | [`01_hello_allow.py`](01_hello_allow.py) | `allow` | "Just confirm my install works." |
| 02 | [`02_block_dangerous.py`](02_block_dangerous.py) | `deny` | "Block `rm -rf /` before it can run." |
| 03 | [`03_preview_writes.py`](03_preview_writes.py) | `dry_run` | "Show me the file BEFORE saving it." |
| 04 | [`04_human_approval.py`](04_human_approval.py) | `approve_required` | "Pause for my OK before wiring money." |
| 05 | [`05_real_llm_blocked.py`](05_real_llm_blocked.py) | `allow` + `deny` | "Use a REAL LLM (Claude / GPT) — does Lynx still gate it?" |
| 06 | [`06_streaming_to_jsonl.py`](06_streaming_to_jsonl.py) | (focus: sinks) | "Stream every event to a jsonl file — your audit trail." |
| 07 | [`07_refund_workflow.py`](07_refund_workflow.py) | `allow` + `approve` + `deny` | "Customer support: small refunds auto, big ones ask, fraud denies." |
| 08 | [`08_sql_transform.py`](08_sql_transform.py) | `transform` | "Auto-add `WHERE tenant_id = X` to every multi-tenant SQL query." |
| 09 | [`09_fastapi_service.py`](09_fastapi_service.py) | full HTTP service | "Wrap Lynx in FastAPI for production deployment." |
| 10 | [`10_devops_assistant.py`](10_devops_assistant.py) | **all five verdicts** (one policy, run in staging + prod) | "An AI DevOps assistant — every safety rule in one realistic scenario." |
| 11 | [`11_flask_service.py`](11_flask_service.py) | sync HTTP service | "Same as 09 but for Flask — sync framework, `asyncio.run(...)` inside view." |
| 12 | [`12_django_service.py`](12_django_service.py) | Django 4.1+ async views | "Same as 09 but as a single-file Django app." |
| 13 | [`13_python_rules.py`](13_python_rules.py) | python_rules + diagnostics | "When YAML can't express it, drop into a Python rule — and surface rule errors via `matched_rules`." |
| 14 | [`14_transform_ops.py`](14_transform_ops.py) | TRANSFORM `set` + `append` + `delete` | "All three transform operations in one policy, with proof of what the tool actually received." |
| 15 | [`15_sqlite_sink.py`](15_sqlite_sink.py) | custom sink + `multi_sink` resilience | "Write a SQLite audit sink yourself in 10 lines; one bad sink in `multi_sink` doesn't kill the run." |
| 16 | [`16_async_approval.py`](16_async_approval.py) | cross-process approval (mocked) | "The Slack/webhook pattern with a real `asyncio.Event` — including timeout enforcement." |
| 17 | [`17_shadow_helpers.py`](17_shadow_helpers.py) | `lynx.shadows.*` helpers | "Don't reinvent shadows — wire the built-in filesystem/HTTP/shell/SQL previews directly." |
| 18 | [`18_sandboxed_tool.py`](18_sandboxed_tool.py) | `subprocess_executor` | "One line on the executor seam bounds EVERY tool's CPU/memory/wall-clock. Not a security boundary — see SECURITY.md." |
| 19 | [`19_hot_swap.py`](19_hot_swap.py) | hot-swap + budget + unknown tool | "Different policy on the next call. `Budget.steps` exhaustion. Unknown tool — survives." |
| 20 | [`20_mcp_tools.py`](20_mcp_tools.py) | MCP integration | "Discover an MCP server's tools and pipe them through Lynx's policy, with proper child-process lifecycle." |
| 21 | [`21_langgraph_demo.py`](21_langgraph_demo.py) | LangGraph adapter | "Wrap a compiled LangGraph state graph in `LangGraphAgent` so its tool nodes go through policy." |
| 22 | [`22_crewai_demo.py`](22_crewai_demo.py) | CrewAI adapter | "Wrap a Crew in `CrewAIAgent`. Single-shot tradeoff documented inline." |
| 23 | [`23_compile_errors.py`](23_compile_errors.py) | `PolicyCompileError` | "Every bad policy now fails loudly at compile time instead of silently never matching — drop into CI." |
| 24 | [`24_durable_resume.py`](24_durable_resume.py) | `RunStore` durability | "Crash mid-run, retry with the same run_id — the model isn't re-called and the customer isn't double-charged." |
| 25 | [`25_token_budget.py`](25_token_budget.py) | `Usage` + token `Budget` caps | "Meter every step, price it in YOUR sink at YOUR rates, and stop a runaway loop with `Budget(output_tokens=...)`." |
| 26 | [`26_executor_seam.py`](26_executor_seam.py) | `Executor` + `route_executor` | "Policy decides WHETHER; the executor decides WHERE — inline, subprocess, or your container, routed per tool, failing closed." |
| 27 | [`27_handoff_graph.py`](27_handoff_graph.py) | `run_graph` + `compile_graph` (state machine) | "A finite state machine over agents: triage (read-only) → fixer (write) ⇄ reviewer, with `done` terminal and `max_transitions` bounding the walk — the edge is a permission boundary, enforced not prompted." |
| 28 | [`28_full_stack_pipeline.py`](28_full_stack_pipeline.py) | **all pillars composed** | "One refund pipeline: graph + policy + approval + executor routing + metering + durable replay — six features, one chokepoint." |
| 29 | [`29_memory_gating.py`](29_memory_gating.py) | memory ops through policy | "Gate remember/recall/forget: poisoning denied, recalls tenant-scoped, deletions previewed + human-approved (OWASP ASI06)." |
| 30 | [`30_cost_attribution.py`](30_cost_attribution.py) | FinOps attribution sink | "Per-customer × per-model chargeback from run.started + step.usage — your rates, your join, no proxy." |
| 31 | [`31_kill_switch.py`](31_kill_switch.py) | `CancelToken` + `Budget(max_repeated_calls)` | "Stop a runaway mid-run after one more action; break a same-tool-same-args loop — clean structured stops, no crash." |
| 32 | [`32_token_optimization.py`](32_token_optimization.py) | `compressor=` + `route_compressor` + `@tool(compress=)` | "Trim a tool's output once at the boundary and it's not re-sent in full every step — dedup/truncate per tool, one tool opting out, savings on the audit stream." |
| 33 | [`33_subagents.py`](33_subagents.py) | subagent-as-tool (`run_agent` inside a `@tool`) | "A lead agent delegates to workers by calling a tool that runs an agent — spawn gated by the lead's policy, each worker its own boundary, sequential and parallel (`asyncio.gather`), with the audit tree." |
| 34 | [`34_mcp_proxy.py`](34_mcp_proxy.py) | MCP proxy (`lynx.proxy.mcp_proxy`) | "Sit Lynx in front of any MCP server — every `call_tool` flows through `evaluate`→`mediate` (allow/deny/dry_run) with an audit stream, zero code change on client or server. Reads allowed, writes previewed, deletes blocked." |
| 35 | [`35_multi_provider.py`](35_multi_provider.py) | OpenAI-compatible providers (`lynx.adapters.openai_compat`) | "One policy, any model: Grok / Mistral / DeepSeek / Groq / OpenRouter / Ollama via `openai_compatible_agent(provider, ...)` — the governance boundary is identical no matter which model proposes the calls." |
| 36 | [`36_fastmcp_governed.py`](36_fastmcp_governed.py) | FastMCP server + Lynx proxy | "Build an MCP server the popular way (FastMCP `@mcp.tool()`), then govern it with Lynx — reads allowed, writes previewed, deletes denied, the denied delete never touching the real filesystem. Dual-mode: `--serve` is the server, no-args is the governed demo." |
| 37 | [`37_tamper_evident_audit.py`](37_tamper_evident_audit.py) | Tamper-evident audit (`hash_chained_sink` / `verify_chain`) | "Stream audit events through a hash-chained sink, then edit one line — `verify_chain` (and `lynx verify`) catches it and names the broken line. An audit log you can quietly edit isn't an audit log." |
| 38 | [`38_otel_audit.py`](38_otel_audit.py) | OpenTelemetry sink (`otel_sink`) | "Turn every governance decision into an OpenTelemetry span so verdicts show up in Datadog / Honeycomb / Tempo next to the rest of your traces — stateless, nests under the ambient trace, zero custom plumbing." |

## How to run any of them

```bash
# Set up once
pip install -e ".[dev]"

# Examples 01-04, 06-08, 10, 13-19, 23-31 — no API key, no extras needed
python examples/01_hello_allow.py
python examples/02_block_dangerous.py
python examples/03_preview_writes.py
python examples/04_human_approval.py    # type "y" + Enter at the prompt
python examples/06_streaming_to_jsonl.py
python examples/07_refund_workflow.py
python examples/08_sql_transform.py
python examples/10_devops_assistant.py
python examples/13_python_rules.py
python examples/14_transform_ops.py
python examples/15_sqlite_sink.py
python examples/16_async_approval.py
python examples/17_shadow_helpers.py
python examples/18_sandboxed_tool.py
python examples/19_hot_swap.py
python examples/23_compile_errors.py
python examples/24_durable_resume.py
python examples/25_token_budget.py
python examples/26_executor_seam.py
python examples/27_handoff_graph.py
python examples/28_full_stack_pipeline.py
python examples/29_memory_gating.py
python examples/30_cost_attribution.py
python examples/31_kill_switch.py
python examples/32_token_optimization.py
python examples/33_subagents.py

# Example 05 — needs a real LLM API key
export ANTHROPIC_API_KEY=sk-ant-...     # or OPENAI_API_KEY=sk-...
python examples/05_real_llm_blocked.py

# Example 09 — runs as a web service
pip install fastapi uvicorn
uvicorn examples.09_fastapi_service:app --reload
# Then POST to http://localhost:8000/agent/run

# Example 11 — Flask web service
pip install flask
# Use the file-path form: the digit-prefixed filename is not a valid Python
# module name, so the dotted "examples.11_flask_service" form will not work.
flask --app examples/11_flask_service.py run --debug

# Example 12 — Django web service (single-file)
pip install django
python examples/12_django_service.py runserver

# Example 20 — MCP adapter (needs a real MCP server to connect to)
pip install lynx-agent[mcp]
python examples/20_mcp_tools.py 'npx -y @modelcontextprotocol/server-filesystem .'

# Example 21 — LangGraph adapter
pip install lynx-agent[langgraph] langchain-core
python examples/21_langgraph_demo.py

# Example 22 — CrewAI adapter
pip install lynx-agent[crewai]
python examples/22_crewai_demo.py

# Example 34 — MCP proxy (core demo runs standalone; live proxy needs a server)
python examples/34_mcp_proxy.py
pip install lynx-agent[mcp]   # then uncomment serve_mcp_proxy(...) for the live proxy

# Example 35 — OpenAI-compatible providers (lists registry; live call needs a key)
pip install lynx-agent[openai]
python examples/35_multi_provider.py
DEEPSEEK_API_KEY=sk-... python examples/35_multi_provider.py deepseek deepseek-chat

# Example 37 — tamper-evident audit (stdlib only, no extra deps)
python examples/37_tamper_evident_audit.py
lynx verify <the temp file it prints>

# Example 38 — OpenTelemetry sink (needs the OTel SDK for the console exporter)
pip install lynx-agent[otel] opentelemetry-sdk
python examples/38_otel_audit.py
```

## After running anything

In Lynx the audit goes to **your sinks**, not to a Lynx-managed database. Inspect the events as they happen via `stdout_sink()`, or stream them to a file via `jsonl_sink(...)` (see example 06).

There is no `lynx ps` / `lynx audit` — Lynx itself holds no past runs. (`lynx trace <records.jsonl>` exists, but it reads a journal file *you* kept via a RunStore — see example 24.)

## What each example demonstrates

| | Concept | Where to learn |
|--|---------|----------------|
| ALLOW    | Policy lets the action through unchanged | 01, 02, 05, 07, 10 |
| DENY     | Policy refuses; agent sees the denial as a tool result | 02, 05, 07, 08, 10 |
| DRY_RUN  | Tool's `.shadow` runs instead of the real function; preview only | 03, 10, 17 |
| APPROVE_REQUIRED | Sync `on_approval` handler is called; the run blocks until it returns | 04, 07, 10, 16 |
| TRANSFORM | Policy rewrites the action's args (e.g. injects a `WHERE` clause) | 08, 10 (staging kubectl apply), 14 (all three ops) |
| Python rules + diagnostics | `python_rules=` argument; `<rule_error:...>` markers in `matched_rules` | 13 |
| Custom sinks | Write your own sink to any storage backend | 15 (SQLite) — also see `docs/integration-cookbook.md` |
| `multi_sink` resilience | Failures in one sink don't abort the run | 15 |
| Cross-process approval | `callback_approval` blocking on an async signal (Slack pattern) | 16 |
| Approval timeout enforcement | Mediator wraps handler in `asyncio.wait_for` | 16 |
| Streaming events via sinks | Every step emits events; sinks consume them | All; explicit focus in 06 |
| Multiple sinks (stdout + jsonl) | `multi_sink(...)` fans out | 06, 15 |
| Built-in shadow helpers | `lynx.shadows.{filesystem, http, shell, sql}_shadow` | 17 |
| Subprocess sandbox | `lynx.sandbox.run_in_subprocess` with CPU/memory/wall-clock caps | 18 |
| Hot-swap policy between runs | Same agent + tools, different `PolicyBundle` | 19 |
| Budget exhaustion | `RunResult.error` with structured budget message, not a crash | 19 |
| Unknown tool handling | `action.failed` + `[error]` injected to conversation; run continues | 19 |
| MCP integration | `mcp_tools(command)` async context manager | 20 |
| MCP proxy (govern any MCP server) | `serve_mcp_proxy(...)` / `GovernedProxy` — policy + audit in front of an upstream server | 34 |
| OpenAI-compatible providers | `openai_compatible_agent(provider, ...)` — Grok / Mistral / DeepSeek / Groq / OpenRouter / Ollama, one policy for all | 35 |
| LangGraph integration | `LangGraphAgent(compiled_graph=...)` | 21 |
| CrewAI integration | `CrewAIAgent(crew=...)` — single-shot tradeoff | 22 |
| `PolicyCompileError` | Every malformed policy fails at compile time, not at runtime | 23 |
| Durability / `RunStore` | Crash-resume, idempotent re-runs, `superseded` losers, `replay()` | 24 |
| `lynx trace` + JSONL store | `step_record_to_json` file journal, `run.bundle_changed` warning | 24 |
| Token metering / `Usage` + caps | `step.usage` events, cost sink with user rates, `Budget(output_tokens=...)` | 25 |
| Executor seam / `route_executor` | Per-tool execution routing, fail-closed isolation, BYO sandbox | 26 |
| Handoff graphs / `run_graph` | Per-node policy boundaries, denial-count routing, Python + YAML routers, durable workflow resume | 27 |
| Full-stack composition | Graph + policy + approvals + executors + metering + durability in ONE run | 28 |
| Memory gating (ASI06) | remember/recall/forget through policy — all five verdicts on one surface | 29 |
| Cost attribution / FinOps | Per-customer chargeback sink joining run.started + step.usage | 30 |
| Kill-switch + repetition gate | `cancel=CancelToken()`, `Budget(max_repeated_calls=)` — stop runaways cleanly | 31 |
| Web service integration | FastAPI / Flask / Django | 09 / 11 / 12 |
| Real LLM | ClaudeAgent / OpenAIAgent (proper `async with` lifetime) | 05 |

## Where to go next

After running through the examples:

| You want to… | Read |
|--------------|------|
| Understand the vocabulary | [`docs/concepts.md`](../docs/concepts.md) |
| Know what Lynx is / isn't (scope, what it composes with) | [`docs/what-lynx-is-and-isnt.md`](../docs/what-lynx-is-and-isnt.md) |
| Build your own policy from scratch | [`docs/02-policy-language.md`](../docs/02-policy-language.md) |
| Copy-paste common policy patterns | [`docs/cookbook.md`](../docs/cookbook.md) |
| Wire sinks / approvals into your stack (SQLite, Postgres, Splunk, OTel, Slack, Temporal, ...) | [`docs/integration-cookbook.md`](../docs/integration-cookbook.md) |
| Get unstuck | [`docs/faq.md`](../docs/faq.md) |

## Want to contribute another example?

See [CONTRIBUTING.md](../CONTRIBUTING.md). Good examples are:
- Self-contained — one Python file + (optionally) one YAML
- Lead with a plain-language SCENARIO at the top of the docstring
- Use a scripted agent for the offline path; document the API key for the LLM path
- Print enough output that the demo tells you what happened
- Demonstrate a verdict, sink, or capability that no existing example covers
