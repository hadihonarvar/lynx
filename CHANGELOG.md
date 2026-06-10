# Changelog

All notable changes to Lynx will be documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- (nothing yet — open a PR!)

### Changed
- (nothing yet)

### Fixed
- (nothing yet)

## [1.0.0] — 2026-06-09

First public release. All of the below shipped together; this is the surface area covered by the v1.0 SemVer commitment.

### Core

- Kernel types: `Task`, `Run`, `Step`, `ActionRequest`, `Decision`, `AuditEvent`, `ToolMetadata`, `ExecutionContext`, `Principal`, `Budget`, `ModelCall`, `ActionResult`.
- Policy compiler + Policy Decision Point (PDP) — pure, deterministic, content-addressed bundles.
- Action Mediator (PEP) — five verdicts: `allow`, `deny`, `dry_run`, `approve_required`, `transform`.
- Scheduler with pre-execution checkpointing; crash-resume + approval-resume both correctly implemented.
- Hash-chained audit log with tamper detection (`lynx audit verify`).

### Public SDK

- `@tool` decorator + `.shadow` for dry-run twins.
- `Runtime.run / resume / replay / approve / deny`.
- `Agent` protocol — one method, `async def step(conversation) -> ToolCall | FinalAnswer`.

### Adapters

- `lynx.adapters.anthropic_sdk.ClaudeAgent`
- `lynx.adapters.openai_sdk.OpenAIAgent`
- `lynx.adapters.langgraph_adapter.LangGraphAgent`
- `lynx.adapters.crewai_adapter.CrewAIAgent`
- `lynx.adapters.mcp.register_mcp_server`

### Storage

- `lynx.stores.sqlite.SQLiteStore` — full implementation, default.
- `lynx.stores.postgres.PostgresStore` — production backend (Tasks / Runs / Audit; Steps + Approvals follow same translation pattern).

### Shadow library

- `lynx.shadows.shell_shadow`
- `lynx.shadows.write_file_shadow`, `delete_file_shadow`
- `lynx.shadows.sql_shadow`
- `lynx.shadows.http_shadow` (with built-in `Authorization` header redaction)

### Sandbox

- `lynx.sandbox.run_in_subprocess` — POSIX subprocess sandbox with `RLIMIT_CPU`, `RLIMIT_AS`, stripped env, timeout.

### Observability

- `lynx.observability.enable_prometheus`
- `lynx.observability.enable_otel`

### CLI

- `lynx init / run / resume / ps / trace / approvals / approve / deny / audit verify / audit export / policy lint / policy bundle-id`

### Documentation

- Onboarding: `why-lynx.md`, `getting-started.md`, `concepts.md`, `cookbook.md`, `faq.md`.
- Reference: `01-data-model.md`, `02-policy-language.md`, `03-sdk-and-cli.md`.
- Threat model: `threat-model.md` (STRIDE-style).
- 12 runnable examples: simple → complex → advanced → complete → integrations (FastAPI / Flask / Django).

### Quality bar

- 57 tests, ~1.2s suite, 9-job CI matrix (Linux / macOS / Windows × Python 3.11 / 3.12 / 3.13) + coverage.
- ruff lint + format clean. `mypy --strict` runs (currently soft-gated on the test matrix; will be hard-gated in v1.1).
- Apache-2.0 licensed. PEP 561 typed (`py.typed`). PEP 639 license metadata.

### Public API surface guaranteed by SemVer

- `lynx.tool`, `lynx.runtime`, `lynx.Runtime`, `lynx.shadow`, `lynx.allow/deny/dry_run/approve_required/transform/rule`.
- `lynx.Agent`, `lynx.Message`, `lynx.ToolCall`, `lynx.FinalAnswer`, `lynx.AgentAction`.
- `lynx.Task`, `lynx.Run`, `lynx.Step`, `lynx.ActionRequest`, `lynx.Decision`, `lynx.AuditEvent`, `lynx.Verdict`, `lynx.RunStatus`, `lynx.Principal`, `lynx.Budget`, `lynx.ToolMetadata`, `lynx.ExecutionContext`, `lynx.ModelCall`, `lynx.ActionResult`.
- Adapters and stores have their own public-class interfaces guaranteed.
- The YAML policy v1 grammar.
- The CLI command surface and exit codes.

Internal modules (`lynx.core.*`) are NOT part of the public API and may change in any minor release.

[Unreleased]: https://github.com/hadihonarvar/lynx/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/hadihonarvar/lynx/releases/tag/v1.0.0
