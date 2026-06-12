# examples/

A learning path of 30 examples. Each is **self-contained** and starts with a plain-language SCENARIO explaining the problem it solves.

Read 01–12 in order for the core narrative; 13–30 cover every remaining feature for full coverage.

```
SIMPLE          01 → 02 → 03         "see the system working"
MORE COMPLEX    04 → 05 → 06         "approvals, real LLMs, streaming audit"
ADVANCED        07 → 08 → 09         "production patterns: rules, transforms, web service"
COMPLETE        10                   "the full thing — one realistic DevOps scenario"
INTEGRATIONS    11 (Flask) 12 (Django)   "drop Lynx into your existing web framework"
FULL COVERAGE   13 → 30              "every feature: python rules, transform ops,
                                       custom sinks, cross-process approval,
                                       shadow helpers, sandbox, hot-swap,
                                       MCP, LangGraph, CrewAI, error model,
                                       durable crash-resume, token budgets,
                                       executor seam (BYO sandbox),
                                       handoff graphs, memory gating,
                                       cost attribution, full-stack capstone"
```

## The 30 examples

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
| 27 | [`27_handoff_graph.py`](27_handoff_graph.py) | `run_graph` + `compile_graph` | "Triage (read-only) → fixer (write) ⇄ reviewer: the edge is a permission boundary, enforced not prompted." |
| 28 | [`28_full_stack_pipeline.py`](28_full_stack_pipeline.py) | **all pillars composed** | "One refund pipeline: graph + policy + approval + executor routing + metering + durable replay — six features, one chokepoint." |
| 29 | [`29_memory_gating.py`](29_memory_gating.py) | memory ops through policy | "Gate remember/recall/forget: poisoning denied, recalls tenant-scoped, deletions previewed + human-approved (OWASP ASI06)." |
| 30 | [`30_cost_attribution.py`](30_cost_attribution.py) | FinOps attribution sink | "Per-customer × per-model chargeback from run.started + step.usage — your rates, your join, no proxy." |

## How to run any of them

```bash
# Set up once
pip install -e ".[dev]"

# Examples 01-04, 06-08, 10, 13-19, 23-30 — no API key, no extras needed
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
| Web service integration | FastAPI / Flask / Django | 09 / 11 / 12 |
| Real LLM | ClaudeAgent / OpenAIAgent (proper `async with` lifetime) | 05 |

## Where to go next

After running through the examples:

| You want to… | Read |
|--------------|------|
| Understand the design | [`docs/v2-rfc.md`](../docs/v2-rfc.md) |
| Understand the vocabulary | [`docs/concepts.md`](../docs/concepts.md) |
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
