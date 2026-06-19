# What Lynx is — and what it deliberately isn't

Lynx is a **control layer for AI agent tool calls**: every action an agent
proposes flows through one chokepoint where policy decides, your executor
runs it, your sinks audit it, and your store makes it durable. It is a
**library** — `pip install lynx-agent`, three dependencies, zero
infrastructure. It runs no server, opens no database, and stores nothing on
your machine.

A complete production agent stack has many parts. Lynx owns a specific,
high-value spine of it and **composes with** specialists for the rest. This
page draws that line on purpose, so the gaps read as boundaries, not holes.

## What Lynx owns (its defensible core)

These are the parts that are hard to add later, dangerous to get wrong, and
where being *in the loop on every action* is the whole point:

- **Enforcement on every action** — a policy gate with five verdicts (allow /
  deny / dry-run / approve / transform). Not observation after the fact:
  the action cannot run unless policy lets it.
- **Human-in-the-loop** — approvals with enforced timeouts; previews before
  irreversible actions.
- **Durable execution** — crash-resume, idempotency, no double side effects;
  you bring the store (Redis/Postgres/anything via two methods).
- **Cost control** — token metering plus hard budget caps that *stop* a run,
  not just alert.
- **Containment** — a pluggable executor seam (inline / subprocess / your
  Docker/gVisor/E2B), routed per tool, failing closed.
- **Bounded multi-agent workflows** — handoff graphs where each node's
  permissions are enforced and routing can key on policy outcomes (denial
  counts); bounded by construction.
- **Operability** — kill-switch (cooperative cancellation), repetition gate,
  per-step and per-tool timeouts.
- **Compliance-grade audit** — every decision and outcome streamed to your
  sinks; the evidence trail SOC 2 / EU AI Act auditors ask for.

## What Lynx does NOT do — and what to use instead

These are real needs in a complete stack. They are platform- or
substrate-shaped, and a zero-infra library is the wrong place for them.
Lynx **integrates with** them rather than reimplementing them.

| Need | Why it's not Lynx's job | Compose with |
|---|---|---|
| **Long-term memory** (vector/graph store, recall, forgetting) | A storage substrate with its own quality engineering; bundling it is the lock-in users reject. Lynx *governs* memory ops as policy-gated tools — it doesn't store. | mem0, Zep, Letta, Cloudflare Agent Memory — gated through Lynx policy |
| **Observability dashboards** (trace UI, search, span trees) | Lynx *emits* the data; the viewer is a hosted product. | Langfuse, Phoenix, Datadog — fed by an OTel sink (see cookbook) |
| **Eval platform** (datasets, LLM-judge, scoring UI) | Datasets + judges + dashboards are a product. Lynx's audit + `replay()` are the *substrate* evals consume. | DeepEval, Confident AI, Arize, Braintrust |
| **Retrieval / RAG** | A knowledge-base concern; expose it as a tool and gate it. | LlamaIndex, your vector DB, as a `@tool` |
| **Agent identity / credential brokering** (scoped tokens, rotation) | Network-resident infra, not a pip install. Lynx *gates on* identity; it doesn't issue it. | MCP gateways, SPIFFE/SVID, your IdP |
| **LLM gateway** (routing, failover, semantic caching) | A network *proxy* — a separate hop. Lynx meters, caps, and (opt-in) compresses tool results in-process; it isn't a proxy. In-process *prompt* caching lives in the adapter (`ClaudeAgent(cache_prompt=True)`); a cross-request *semantic* cache is the gateway's job. | LiteLLM, Portkey, Bifrost |
| **Token optimization** (deciding *what* to drop from a result) | A model- and domain-specific strategy. Lynx owns the `compressor=` seam and ships truncate/dedup conveniences; *which* bytes are worth keeping is yours — the same stance as "you bring the sandbox." | your own `Compressor`, `external_filter_compressor`, RTK at the tool level |
| **Deployment / serving / fleet** (REST, containers, scaling, cron) | The host's job; Lynx is a library you embed in your service. | FastAPI/your framework, k8s, Temporal/Inngest |
| **Prompt registry / versioning** | Caller-owned config management. | MLflow, LangWatch, your VCS |
| **Memory/context summarization quality** | A strategy that's model- and domain-specific. Lynx gives the trigger hook (token metering, memory-as-policy); you supply the strategy. | your own, or a memory layer above |

## The strategic shape

Two patterns explain every line above:

1. **Lynx ships seams and data, you ship infrastructure.** Storage, sandboxes,
   dashboards, gateways — Lynx defines the protocol (often ≤2 methods) and
   you bring the backend. This is why it's three dependencies.
2. **Lynx is the data spine the rest of the stack consumes.** Its audit +
   durable journal + `replay()` are the trustworthy record that evals,
   observability, and compliance tools all want as their *input*. Being the
   neutral, in-process source of truth is more defensible than being a 38th
   dashboard.

If you need a hosted platform that bundles all of the above, Lynx is the
enforcement/durability/audit core you'd put *inside* it — not a replacement
for it. If you're a team that already runs your own storage, sandboxes, and
dashboards and just needs your agents to be safe, recoverable, metered, and
contained, Lynx is the whole answer.
