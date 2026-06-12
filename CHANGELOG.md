# Changelog

All notable changes to Lynx will be documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed — ⚠ behavior change
- **Budgets default to UNLIMITED.** `Budget()` no longer caps anything (previously `steps` defaulted to 50, and `run_agent`'s default budget was `Budget(steps=50, duration_seconds=600)`). The rule is now: *what you define is enforced; what you don't define is no restriction.* Long-running tasks no longer die mysteriously at step 50 — but an unbudgeted agent that never answers runs forever, so set at least `steps` or `duration_seconds` in production. If you relied on the implicit 50-step / 10-minute cap, pass it explicitly.

### Added (timeouts)
- `Budget.step_timeout_seconds` — wraps each `agent.step()` model call in `asyncio.wait_for`, so a hung provider connection fails the run with `error="agent.step timed out after Ns"` instead of hanging forever. Durability-safe: nothing journals until the step returns, so a timed-out step leaves no record and resume re-asks the model cleanly.
- `inline_executor(timeout_seconds=...)` — bounds each in-process tool call; on expiry the action fails with a structured timeout error and the run continues (the agent sees `[error] ...` and adapts). Cancels cooperative tools only — tight CPU loops need `subprocess_executor`, which kills the child.

### Fixed
- `run_in_subprocess` (and therefore `subprocess_executor`) now works for tools defined in a script's `__main__`: the child loads the parent script under a private module name and aliases it as `__main__` before unpickling (the same mechanism `multiprocessing` uses; the script's `if __name__ == "__main__"` guard does not re-run). Previously every script-defined sandboxed tool failed with `sandbox exited 1`. Interactive-session functions now fail fast with a clear message.

### Added
- **Handoff graphs (optional)** — `lynx.graph`: sequential multi-agent workflows where the edge is a permission boundary. Each `GraphNode` is one complete `run_agent` call with its own policy/tools/budget — role boundaries are enforced by policy, not prompts (an overreaching orchestrator model gets denied, then hands off). Routing is a pure `Router` callable over `NodeOutcome` (status, final answer, steps, **denial counts** — a signal only possible because policy is first-class), or a YAML edge table via `compile_graph`/`load_graph_file` (ReDoS-guarded regexes, compile-time validation with `GraphCompileError`, `done` terminal, first-match-wins). `max_transitions` is always enforced — unbounded recursion is impossible by construction; cycles like fixer ⇄ reviewer are fine. Context passing is explicit (`compose_task`). Durability composes: node runs journal under derived child run_ids, routing decisions journal as `handoff` records, resume replays both, racing graph workers resolve to one winner. New graph-level events: `graph.started`, `graph.handoff`, `graph.exhausted`, `graph.superseded`, `graph.finished`. The kernel knows nothing about graphs — this is sugar over a loop of `run_agent` calls you could write yourself.
- Example 27 (`27_handoff_graph.py`): triage (read-only) → fixer (write) ⇄ reviewer (read-only) until approved, with the triage model's own write attempt denied at hop 0.
- Example 27 expanded: a plain-function `Router` (Python first), `denials_gt` escalation with a per-node `Budget`, and whole-workflow durable resume (zero model calls on re-run).
- Example 24 gains Act 7: a JSONL file-backed `RunStore` built on `step_record_to_json` (the format `lynx trace <file>` reads) and the `run.bundle_changed` warning in action.
- Examples 28-30: the full-stack capstone (every pillar composed in one refund pipeline), memory gating through policy (the OWASP ASI06 recipe — poisoning denied, recalls tenant-scoped via TRANSFORM, deletions previewed + approved; all five verdicts on one surface), and a FinOps attribution sink (per-customer x per-model chargeback joining `run.started` with `step.usage`).
- Example 18 modernized to the v2.3 executor seam: `executor=subprocess_executor(...)` bounds every approved action with zero sandbox code in the tools (the manual `run_in_subprocess` API remains documented as the underlying mechanism).

## [2.3.0] — 2026-06-11

Bring your own sandbox, watch every token. Approved actions now execute
through a pluggable seam (inline / subprocess / your Docker-gVisor-E2B
wrapper, routed per tool, failing closed), and adapters meter token usage
that the kernel streams, totals, and hard-caps — while money never enters
the kernel. Zero new dependencies, zero shipped sandboxes, zero price tables.

### Added
- **The executor seam** — every approved action (allow / transform / approval-granted) now flows through an `Executor`: one async callable `(request, tool) -> ActionResult`. Default is `inline_executor()` — bit-for-bit the pre-seam in-process behavior. `subprocess_executor()` repackages the rlimits sandbox (crash/runaway protection, explicitly NOT a security boundary); `route_executor({...})` picks an executor per tool via the new `@tool(isolation="...")` / `ToolMetadata.isolation` hint and **fails closed** when a declared isolation has no route. TRANSFORM verdicts rebuild the request so executors see the *effective* args; dry-runs bypass the seam (shadows are side-effect-free by contract); a raising executor fails the action, never the run. Real isolation (Docker / gVisor / E2B) is user-implemented — cookbook recipes included.
- Example 26 (`26_executor_seam.py`): three tools, three execution destinations, including the fail-closed unrouted case.
- **Token metering and budgets** — adapters (`ClaudeAgent`, `OpenAIAgent`) now attach a `Usage` record (input/output/cache token counts + model, OTel GenAI-aligned field names) to every step; the scheduler emits `step.usage` events with per-step counts and running totals, reports lifetime totals on `RunResult.usage` and in the `run.succeeded` body, and enforces new `Budget` caps: `input_tokens`, `output_tokens`, `tokens` (combined) — checked between steps exactly like `steps`. The kernel counts and enforces counts; it never converts tokens to money — multiply by your own rates in a sink (no price tables shipped, ever).
- `Usage` exported from `lynx`; `ToolCall` / `FinalAnswer` gain an optional `usage` field (non-breaking).
- Durability interplay: usage rides in journaled `model.output` records — resumed runs report accurate lifetime totals, replayed spend counts toward token budgets, and `step.usage` is not re-emitted for replayed steps.
- Example 25 (`25_token_budget.py`): metering, a cost sink with user-supplied rates, and a token cap stopping a runaway loop.

### Changed
- `Budget.tokens` returns (it was removed in v2.0 as unenforced) — this time enforced against adapter-reported counts. `Budget.usd` stays gone permanently.

## [2.2.0] — 2026-06-11

Durability release. The kernel can now journal runs to user-owned storage for
crash-resume, idempotent retries, and no-double-side-effects — while shipping
zero storage and zero new dependencies itself.

### Added
- **Durability (opt-in)** — `run_agent(..., store=, run_id=)` journals the run to a user-implemented `RunStore` (Lynx ships **no storage**; the protocol is two methods over your Redis/Postgres/Dynamo/dict). Re-invoking with the same `run_id` resumes: journaled model outputs replay without re-calling the model, journaled action results replay without re-executing the action, and a completed run returns the same `RunResult` forever.
- `StepRecord`, `RunStore`, `DuplicateRecord`, `idempotency_key()`, `step_record_to_json()` / `step_record_from_json()` — the journal vocabulary, exported from `lynx`.
- **Write-ahead intents + the unique-append concurrency model**: every action journals an `action.intent` before executing; `append` must atomically reject a duplicate `(run_id, seq)` with `DuplicateRecord`, so two racing workers resolve to one winner — the loser returns `error="superseded: ..."` having executed nothing. No leases, no TTLs.
- **Uncertain-action handling**: an intent journaled without a result (crash mid-action) is re-proposed to policy on resume with `context.extra.uncertain_retry: true`, so policy decides whether it re-runs, is denied, or escalates to approval.
- `replay(records)` — pure function reconstructing a `RunView` (steps, verdicts, outcomes, uncertain actions, attempts) from any journal.
- `lynx trace <records.jsonl>` CLI command rendering a journaled run.
- New audit event kinds: `run.resumed`, `run.superseded`, `run.bundle_changed` (resume under a different policy than the journal — warn-and-continue), `step.replayed`, `action.uncertain`; `run.succeeded` body gains `replayed: true` when a finished run is re-invoked. Store-less runs emit exactly the same events as before.
- `replay()` keeps forensics honest: a result that resolved an uncertain retry is marked `resolved_uncertain` in its `StepView` — a denied retry does not erase the fact that the original attempt may have executed.
- `lynx trace` refuses to merge multiple runs from one file (asks for `--run-id`) and detects audit-sink files passed by mistake.
- Example 24 (`24_durable_resume.py`): crash → resume → exactly one charge, plus supersede and `replay()`.
- Integration cookbook: `RunStore` recipes for Redis (`HSETNX`), Postgres (`PRIMARY KEY` + unique-violation), in-memory, and JSONL files.

### Changed
- `correlation_id` defaults: a fresh journaled run uses the `run_id`; any re-invocation gets `"<run_id>#<suffix>"` so `(correlation_id, seq)` stays unique across attempts (sinks that key on it never overwrite a prior attempt's events) while remaining groupable by prefix.
- If the `RunStore` itself fails mid-run (`append`/`load` raised), the run stops with a structured `store.append failed` / `store.load failed` error instead of continuing to execute side effects that cannot be journaled. `steps_taken` is preserved on all durability exit paths, and `run.started` is always the first audit event even when `store.load` fails.

## [2.1.0] — 2026-06-11

Audit followup release. Hardens the kernel, fixes ~50 bugs found in the
post-2.0 audit, ships an integration cookbook, and adds 11 new examples
so every public feature has a runnable demo.

### Added
- `PolicyCompileError` raised for malformed YAML, unknown operators (with typo suggestions), unknown predicate names, invalid `transform` blocks, malformed `between` / `in` operands, and ReDoS-guard rejections.
- `Message.tool_call_args` field — the scheduler now records the assistant's tool-call shape so Anthropic / OpenAI adapters can re-emit a well-formed `assistant→tool` alternation on the next step.
- `action.dry_run_completed` audit event kind, distinct from `action.completed`. Tool-side denials emit `action.denied` (was `action.failed`) so consumers can bucket denials separately.
- `mcp_tools` now returns an `async with` context manager that keeps the MCP child process alive for the lifetime of the run.
- Sink failures (in `run_agent` and in `multi_sink`) are reported to stderr instead of being silently swallowed.
- `ClaudeAgent` and `OpenAIAgent` are async context managers and expose `aclose()`. Auto-created HTTP clients are released on `__aexit__`; user-supplied clients are left alone.

### Fixed
- TRANSFORM verdict no longer silently degrades to ALLOW when `transform_args` is missing.
- Python rules and YAML rules now share a single priority-sorted evaluation order; a higher-priority YAML rule no longer loses to a lower-priority Python rule.
- `bundle_id` now hashes rule bodies (and defaults / python-rule priorities), not just rule IDs. Two policies with the same IDs but different verdicts now produce different IDs.
- Equal-priority rules sort by integer file order, not by lexicographic source location (`rule[10]` no longer sorts before `rule[2]`).
- `approve_required` `timeout_seconds` is enforced by the mediator: a hanging handler now times out into a deny instead of hanging the run forever. Exceptions in the handler convert to a deny.
- `cli_prompt_approval` no longer blocks the event loop while waiting for stdin.
- Sandbox subprocess kill path now reaps the child; PYTHONPATH no longer leaks empty `sys.path` entries.
- `Verdict` parsing in YAML accepts mixed case.
- `in` / `between` / `not_between` operators validate their right-hand side at compile time.
- Operator typos (`args.cmd.matchess`) raise `PolicyCompileError` instead of silently becoming a never-matching field path.
- `canonical_json` falls back to `repr()` for non-serializable values instead of crashing sinks.
- `ToolSet.from_functions` / `with_tool` / `union` raise on duplicate tool names instead of silently overwriting.
- `Budget.duration_seconds` uses `time.monotonic()` instead of `time.time()`.
- `_annotation_to_schema` understands `list[int]`, `Literal[...]`, `Optional[X]`, `Union[...]`, `tuple[...]`, and `bytes` instead of flattening every non-primitive to `{"type": "string"}`.
- Service examples (FastAPI / Flask / Django) inspect events for `action.denied` and return HTTP 403 instead of reporting a misleading 200.
- Example 10 + `examples/policies/devops.yaml` now exercise all five verdicts (run once in staging + once in prod). The docstring matches reality.
- Django example puts the project root on `sys.path` before `django.setup()` so the documented invocation actually works.

### Removed
- `Budget.usd` and `Budget.tokens` fields — neither was enforced; token/spend accounting belongs in a sink.

### Leak fixes
- `shadows/sql.py`: cursor opened against a user-supplied `conn` was never closed; now closed in a `finally` block.
- `sandbox.py`: the sandboxed child is now killed and reaped in a `finally` block, so cancellation or any post-exec exception cannot leave a zombie process or open stdout/stderr pipes.
- `adapters/anthropic_sdk.py` + `adapters/openai_sdk.py`: when the agent auto-created the SDK client, the HTTP/2 connection pool had no shutdown path. `aclose()` + `__aenter__` / `__aexit__` close it cleanly. User-supplied clients are untouched.

### Documentation
- New `docs/integration-cookbook.md` — wiring patterns for sinks (SQLite, PostgreSQL, OpenTelemetry, Splunk HEC, generic HTTP), approval handlers (Slack, email-link, queue), and durability (Temporal). All recipes are ~5–20 lines of user code; Lynx imports nothing from those packages.
- `docs/v2-rfc.md`: reconciled drift after the audit pass — hot-swap wording, removed contradictory mypy claim, fixed `run_agent` default signature, removed stale `RunStatus.RUNNING` example, removed the doubly-listed deferred sinks block, updated event-kinds list (`action.dry_run_completed` added; `action.denied` semantics expanded), updated `cli_prompt_approval` sketch to use `asyncio.to_thread`, documented approval timeout + handler-exception semantics, updated MCP adapter signature to the new async-context-manager shape, removed lingering "Runtime per request" reference in the examples table.
- `docs/faq.md`, `examples/README.md`, `README.md`: cross-linked the new integration cookbook.

### Examples (full-coverage pass)
- **Fixed**: `examples/05_real_llm_blocked.py` now uses `async with ClaudeAgent(...)` / `async with OpenAIAgent(...)` instead of leaking the adapter's auto-created HTTP client.
- **Added 11 new examples** so every feature has a runnable demo:
  - `13_python_rules.py` — `python_rules=` argument; demonstrates rule-error markers (`<rule_error:rule_id:ExceptionName>`) surfaced in `Decision.matched_rules`; shows Python/YAML rules interleaved by priority.
  - `14_transform_ops.py` — all three transform operations (`set` + `append` + `delete`) in one policy, with proof of what the tool actually received.
  - `15_sqlite_sink.py` — a custom SQLite audit sink (your code, your connection, your retention); plus a `multi_sink` with one intentionally-broken sink to prove the run still completes.
  - `16_async_approval.py` — the cross-process approval pattern (Slack-style) with a real `asyncio.Event` mock; demonstrates `timeout_seconds` enforcement.
  - `17_shadow_helpers.py` — uses the pre-built `lynx.shadows.{write_file, shell, http, sql}_shadow` helpers instead of inline shadows.
  - `18_sandboxed_tool.py` — `lynx.sandbox.run_in_subprocess` bounding CPU + memory + wall-clock; demonstrates the timeout path (child killed and reaped, no zombies).
  - `19_hot_swap.py` — same agent + tools, two different `PolicyBundle`s, different verdicts; plus `Budget.steps` exhaustion and unknown-tool resilience in the same file.
  - `20_mcp_tools.py` — `async with mcp_tools(command) as remote` with proper child-process lifecycle.
  - `21_langgraph_demo.py` — a minimal compiled LangGraph state graph wrapped in `LangGraphAgent`.
  - `22_crewai_demo.py` — a minimal `Crew` wrapped in `CrewAIAgent`; documents the single-shot tradeoff inline.
  - `23_compile_errors.py` — eight different malformed policies, each caught at compile time by `PolicyCompileError` (typo'd operators, unknown predicates, missing transform blocks, ReDoS regex shapes, etc.).
- `examples/README.md` reorganized: now teaches 23 examples in four tiers (SIMPLE / MORE COMPLEX / ADVANCED / COMPLETE / INTEGRATIONS / FULL COVERAGE); the "What each example demonstrates" coverage table now maps every public feature to its example.

## [2.0.0] — 2026-06-10

**Breaking rewrite.** Lynx becomes a stateless, type-safe policy kernel. Pure functions over immutable values. No SQLite. No globals. No leaks. v1.0.x is preserved on PyPI for users who need durability + audit storage.

### Identity (changed)

> v1: "Policy + durable execution + hash-chained audit at the tool-call boundary."
> v2: "**A stateless, type-safe policy kernel for AI agent tool calls.** Pure functions. Streaming events. No DB."

### Public API

#### Added
- `run_agent(agent, task, *, tools, policy, sinks, on_approval, ...)` — the single entry point. Pure async function.
- `ToolSet` — immutable mapping built from `@tool`-decorated functions; `ToolSet.from_functions(*fns)`, `.with_tool(...)`, `.union(...)`.
- `Sink` protocol + `stdout_sink`, `jsonl_sink`, `noop_sink`, `multi_sink`, `callback_sink`.
- `ApprovalHandler` protocol + `auto_approve`, `auto_deny`, `cli_prompt_approval`, `callback_approval`.
- `ApprovalRequest`, `ApprovalDecision` frozen types.
- `RunResult` minimal frozen type (`correlation_id`, `bundle_id`, `final_answer`, `error`, `steps_taken`).
- `AuditEvent` simplified: `correlation_id`, `bundle_id`, `seq`, `kind`, `timestamp`, `body`.
- `compile_policy(..., python_rules=...)` — explicit Python rules.

#### Removed
- `Runtime` class (and the module-level `runtime` singleton).
- `runtime.run / resume / approve / deny / get_run / get_steps / audit_chain / verify_audit / list_runs`.
- SQLiteStore, PostgresStore, the whole `stores/` package.
- `ApprovalBroker` — replaced by synchronous `on_approval` callback.
- Global tool registry — replaced by explicit `ToolSet`.
- Global `@policy.rule` registration — replaced by `python_rules=` argument.
- `enable_prometheus`, `enable_otel`, `trace_step` — replaced by sinks (Prometheus/OTel sinks land in 2.1).
- Pre-execution checkpointing.
- Idempotency-key dedupe (`compute_idempotency_key`, `GENESIS_HASH`).
- Hash-chained `AuditEvent.id` / `.prev`.
- `Step.checkpoint_blob`, `Run.resume_token`, `Run.last_step_seq`, `RunStatus.PAUSED`.

### CLI

#### Kept
- `lynx --version`
- `lynx init` — writes policy.yaml only (no `.lynx/`, no `lynx.toml`)
- `lynx run <script>` — runs an async `main()` from any Python script
- `lynx policy lint`
- `lynx policy bundle-id`

#### Removed
- `lynx ps`
- `lynx trace <run-id>`
- `lynx audit verify / export`
- `lynx resume`
- `lynx approvals / approve / deny`

### Type system

- Every public type is `frozen=True, slots=True`.
- Public API uses `Mapping` / `tuple` / `Sequence`, never `dict` / `list`.
- Zero `Any` in the public API surface; internal `Any` only at adapter boundaries.
- `mypy src` runs in CI as an advisory check; tightening to `--strict` and making it a hard gate is tracked for a follow-up release.

### Dependencies

- Dropped: `msgpack`, `python-ulid` (now using stdlib `uuid`).
- Dropped extras: `[postgres]`.
- Optional extras kept: `[anthropic]`, `[openai]`, `[langgraph]`, `[crewai]`, `[mcp]`.
- New optional extras coming in 2.1: `[sinks-otel]`, `[sinks-prom]`, `[sinks-kafka]`, `[sinks-http]`.

### Testing

- Test suite rewritten around the new surface. Removed: store, audit-chain, resume, broker, idempotency tests. Added: ToolSet immutability tests, sink contract tests, approval handler tests, `run_agent` integration tests (including TRANSFORM end-to-end, approval timeout, sink failures, and policy hot-swap).

### Documentation

- New: `docs/v2-rfc.md` — the formal RFC this implementation follows.
- Rewritten: README, examples (12), concepts, FAQ, cookbook.
- Removed: data-model deep dive (the new model is small enough to live in the RFC).

## [1.0.1] — 2026-06-10

Docs-only release. Aligned docs with v1.0 surface. See git history for details.

## [1.0.0] — 2026-06-09

First public release. v1 design preserved on PyPI for users needing durability + audit chain.

[Unreleased]: https://github.com/hadihonarvar/lynx/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/hadihonarvar/lynx/releases/tag/v2.1.0
[2.0.0]: https://github.com/hadihonarvar/lynx/releases/tag/v2.0.0
[1.0.1]: https://github.com/hadihonarvar/lynx/releases/tag/v1.0.1
[1.0.0]: https://github.com/hadihonarvar/lynx/releases/tag/v1.0.0
