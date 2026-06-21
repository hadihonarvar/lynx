# Lynx Roadmap — becoming the authorization & audit layer for AI agents

> **Strategic frame.** Lynx does not win by becoming another batteries-included
> agent platform (that's DeerFlow / Omnigent — complete, demoable apps with a
> head start). A *policy kernel* has no viral demo. Lynx wins by becoming the
> **governance/audit standard every harness depends on** — the OPA / Stripe of
> agent *actions*. Success is measured in the dependency graph, not raw stars:
> *"every serious agent harness — including DeerFlow and Omnigent — uses Lynx
> for governance."*
>
> The moat nobody can copy without a rewrite: **stateless + pure + provable.**
> Lynx stores nothing, so there is no data-residency problem, every decision is
> unit-testable, and you can *prove* to an auditor what an agent cannot do.

## Positioning (Phase 0 — no code)

- Claim an unclaimed, searchable category: **"the authorization & audit layer
  for AI agents"** / "OPA for agent tool calls" / "the agent action firewall".
  (NeMo / Guardrails own *prompt* filtering; the open lane is runtime *action*
  governance.)
- README hero = the `rm -rf` / prod-`kubectl` save + the audit trail it
  produces. Security saves go viral; capability demos don't.
- Lead with stateless/provable as the compliance superpower, not a footnote.

## Where Lynx is today (module → capability)

| Module | Provides | Area |
|---|---|---|
| `core/scheduler.py` | `run_agent` loop | Engine (minimal) |
| `policy.py` / `core/policy.py` | `evaluate`, 5 verdicts, YAML + Python rules, predicates | **Governance (core)** |
| `core/mediator.py` | Verdict dispatch | Governance |
| `decorators.py` + `shadows/` | `@tool`, `@shadow`, fs/shell/http/sql shadows | Tools / dry-run |
| `approvals.py` | cli / callback / auto — **sync only** | HITL (partial) |
| `sinks.py` | stdout, jsonl, noop, multi, callback | Observability (basic) |
| `durability.py` | `RunStore` **protocol**, journal, replay, idempotency | Durability (seam only) |
| `executors.py` | inline, subprocess, route | Isolation (seam, no cloud) |
| `compressors.py` | token compression suite | Cost / context |
| `cancel.py` | `CancelToken` | Ops |
| `graph.py` | handoff graph — **sequential** | Multi-agent (partial) |
| `cli/main.py` | `init`, `run`, `trace`, `policy lint`, `policy bundle-id` | Tooling |
| `adapters/` | anthropic, openai, langgraph, crewai, **mcp (consumer)** | Integration |

Foundations are in place. Almost everything below is **batteries on existing
seams** — additive, low-risk. The only true core change is layered policy scopes.

## Phase 1 — The Trojan Horse (growth engine)

Goal: *attention*. Meet people where they already are (MCP) and give them
something demoable.

1. **`lynx-mcp-proxy`** — drop Lynx in front of any MCP server: policy + audit +
   approvals + dry-run on tools the user already has, zero code change. The
   demoable wow. *(Prototype landed: `src/lynx/proxy/mcp_proxy.py`.)*
2. **Broad model reach + 3-line adapters.**
   - ✅ **OpenAI-compatible provider registry** (`lynx.adapters.openai_compat`):
     Grok (xAI), Mistral, DeepSeek, Groq, OpenRouter, Together, Fireworks,
     Perplexity, Ollama through one `OpenAIAgent` + `openai_compatible_agent()`;
     one policy governs every provider. *(Landed: example 35, tests.)*
   - ⬜ **Claude Agent SDK + OpenAI Agents SDK** adapters; keep all adapters
     genuinely 3-line.
3. ✅ **OTel sink** (`otel_sink`) — **shipped.** Each `AuditEvent` → one
   OpenTelemetry span (`lynx.*` attributes), stateless, nests under the ambient
   trace; optional `[otel]` extra. *(Example 38, tests.)*

## Phase 2 — Enterprise-real (closes deals)

Goal: *deployable in a regulated company*.

4. **Layered policy scopes** — `compile_policy([org, team, user], …)` with
   strict-overrides-loose merge + layer provenance in `matched_rules`. Highest-
   value core change.
5. **Shipped seam batteries** — `docker_executor` / `e2b_executor`; reference
   `PostgresRunStore` + `RedisRunStore`.
6. **Durable / async approvals** — approval queue + resume tied to `RunStore`
   (Slack / web callback that can outlive the process).
7. **Policy pack** — `lynx init --pack baseline` ships rules for rm-rf,
   prod-kubectl, PII egress, spend caps. Protected in 5 minutes.

## Phase 3 — The moat (unique to Lynx)

8. **Tamper-evident audit (the hash-chain sink).** ✅ **Shipped (2.9.0,
   hash-chain tier).** `hash_chained_sink` + `verify_chain` + `lynx verify`
   land the tamper-*evident* tier; Ed25519 signing (tamper-*proof*) remains a
   deferred follow-up. Today audit events stream to
   a sink (`jsonl_sink` → a file) with no integrity guarantee: anyone who can
   edit the file can change a line, drop a denial, or reorder events undetected.
   Every action-layer competitor advertises "signed / immutable / hash-chained"
   audit (APort, asqav, AgentMint, MCP gateways) — and `AuditEvent` currently
   says *"no hash chain."* Close it, and keep the kernel pure: **it's just a
   sink, not a new subsystem.**

   The chain — each line carries a fingerprint of (previous fingerprint + this
   event), so any edit/delete/reorder breaks every fingerprint after it:

   ```text
   line 1: {event…, hash: H1 = sha256(GENESIS + event1)}
   line 2: {event…, hash: H2 = sha256(H1     + event2)}
   line 3: {event…, hash: H3 = sha256(H2     + event3)}
   ```

   What we add — two small pieces, **zero kernel change**:
   - **`hash_chained_sink(handle)`** — like `jsonl_sink`, but keeps one
     `prev_hash` variable and writes `event + hash` per line. The little bit of
     state lives in the sink, exactly as `jsonl_sink` already holds a file
     handle — purity preserved. Hash over the existing `canonical_json` so it's
     stable. (Composes with `multi_sink`.)
   - **`verify_chain(path)` + `lynx verify <audit.jsonl>`** — re-walks the file
     and reports `intact` or `broken at line N`.

   Optional, one extra arg for tamper-*proof* (not just tamper-*evident*):
   - **`hash_chained_sink(handle, sign_key=…)`** — also Ed25519-signs each hash,
     so forging a valid chain from scratch needs your key; `verify_chain` checks
     signatures when given the public key.

   Tiers: hash-chain = "nobody altered the log" (do this) · + signing = "nobody
   can forge the log" (optional). Highest value-to-effort item on the roadmap —
   the field treats it as table stakes while Lynx currently disclaims it.
9. **Policy testing harness** — `lynx.testing` fixtures + pytest plugin:
   assert allow/deny/blast-radius. "Unit-test your agent's permissions."
10. **Visual audit viewer** — small read-only web view over the event stream
    (the screenshot people share).
11. **Compliance export** — SOC2 / EU-AI-Act-flavored report from journal +
    events (consumes the verified hash-chain from item 8).

## Phase 4 — Ubiquity

12. **Parallel fan-out / aggregate** in `graph.py` (each branch its own
    policy/budget).
13. **Upstream PRs** adding Lynx governance to DeerFlow & Omnigent — their
    weakness, your distribution.
14. **Threat-model / "agent action risk" framework** doc — own the vocabulary.

## Sequencing logic

Phase 1 buys attention (MCP is where the eyes are, and it's demoable).
Phase 2 makes it deployable in a real company. Phase 3 builds the defensible
moat nobody else has. Phase 4 makes Lynx load-bearing across the ecosystem.
