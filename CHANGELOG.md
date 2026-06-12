# Changelog

All notable changes to Lynx will be documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
