# Security Policy

## Supported versions

Lynx follows SemVer. We provide security fixes for the **latest two minor releases**. Major-version bumps include a documented overlap so users can upgrade safely.

## Reporting a vulnerability

**Please do not file public GitHub issues for security vulnerabilities.**

Use **[GitHub Security Advisories](https://github.com/hadihonarvar/lynx/security/advisories/new)** to file a private vulnerability report. This is the preferred channel — it gives us a private workspace to triage the issue and coordinate disclosure.

If you cannot use GitHub for the report, open a regular issue **titled only "Security contact request"** (no details) and a maintainer will reach out with a private channel within 48 hours.

Include:

1. A description of the issue
2. Steps to reproduce
3. The version / commit hash you tested against
4. Any potential mitigations you've identified
5. Whether you'd like public credit when the fix ships

## What to expect

- **First reply within 48 hours** confirming we received the report.
- **Severity triage within 7 days.** We use a four-tier system: Critical, High, Medium, Low.
- **Fix targeted within 30 days** for Critical/High; 90 days for Medium/Low.
- **Coordinated disclosure** — we'll work with you on a public disclosure date once the fix is shipped, with a minimum 14-day window after release to let users upgrade.

## Scope

In scope:

- Policy-bypass: any input that causes the PDP to return ALLOW when policy intended otherwise
- Verdict-routing bugs: the mediator running real side effects under `dry_run` / `approve_required` / `deny`
- Regex-DoS / parser-DoS in the policy compiler
- Approval-handler misrouting: the kernel calling the wrong handler, or skipping it
- Resource limits in `lynx.sandbox.run_in_subprocess` failing to bound CPU / memory / timeout as documented
- Executor-routing bugs: a tool declaring `isolation="x"` reaching a different route, an unrouted isolation hint silently falling back to the default route instead of failing closed, or TRANSFORM args not reaching the executor as decided
- Credential leaks in `AuditEvent.body` content emitted by the kernel (note: what your sinks do with events is your concern)
- Dependency chain attacks (typosquatting, supply-chain)

Out of scope:

- **`lynx.sandbox` as a security boundary.** It runs a tool in a fresh interpreter with best-effort `RLIMIT_CPU` / `RLIMIT_AS` / cwd + env stripping; it is NOT filesystem or network isolation, NOT a syscall filter, and the tool body is shipped via `pickle` and runs as the same OS user. Use a container / microVM / nsjail for real isolation. We will fix documented-behavior bugs (timeouts not firing, processes not reaped); we will not treat "sandboxed tool reached the filesystem" as a vuln. The same applies to `subprocess_executor()`, which is the same mechanism behind the executor seam; isolation guarantees of the executor seam are those of the executor *you* plug in (Docker / gVisor / E2B), not Lynx's.
- What your sinks do with events (file permissions on `audit.jsonl`, retention, downstream tampering — those are your concerns; Lynx holds nothing)
- Vulnerabilities in third-party tools you wrap with `@tool` (those are your dependency's problem)
- Misconfigured policies that allow dangerous actions (this is the operator's responsibility)
- Issues in the optional adapters (`lynx/adapters/*`) that depend on bugs in the wrapped SDK
- Findings against end-of-life versions (only the latest minor release line receives security backports)

## Threat model

The kernel's trust boundaries:

1. **Agent → kernel.** The agent is untrusted. The kernel validates every action through the PDP before any side effect.
2. **Kernel → tool.** The tool author is trusted (you wrote / imported it). The kernel only invokes tools that your `ToolSet` includes.
3. **Kernel → sink.** Sinks receive events and own retention. The kernel itself holds no state beyond a single `run_agent` call.
4. **Kernel → approval handler.** The handler is called synchronously and is fully trusted. Cross-process / cross-host approval is the handler's design problem.

What Lynx does NOT defend against:

- A compromised tool function (you decide what tools are in the `ToolSet`).
- A handler that lies about its decision (it's your code).
- A sink that drops or alters events (it's your code; the kernel emits faithfully).
- Prompt-injection attacks at the LLM layer (out of scope; use NeMo Guardrails / Lakera in addition).
