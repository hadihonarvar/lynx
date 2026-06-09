# examples/

A learning path of 10 examples. Each one is **self-contained** and starts with a **grandma-level explanation** of the problem it solves and where it would fit in a real system.

Read them in order — each one builds on the last.

```
SIMPLE       01 → 02 → 03           "see the system working"
MORE COMPLEX 04 → 05 → 06           "real workflows: approvals, real LLMs, compliance"
ADVANCED     07 → 08 → 09           "production patterns: rules, transforms, web service"
COMPLETE     10                     "the full thing — one realistic DevOps scenario"
```

## The 10 examples

| # | File | Verdict shown | Problem in one line |
|---|------|--------------|---------------------|
| 01 | [`01_hello_allow.py`](01_hello_allow.py) | `allow` | "Just confirm my install works." |
| 02 | [`02_block_dangerous.py`](02_block_dangerous.py) | `deny` | "Block `rm -rf /` before it can run." |
| 03 | [`03_preview_writes.py`](03_preview_writes.py) | `dry_run` | "Show me the file BEFORE saving it." |
| 04 | [`04_human_approval.py`](04_human_approval.py) | `approve_required` | "Pause for my OK before wiring money." |
| 05 | [`05_real_llm_blocked.py`](05_real_llm_blocked.py) | `allow` + `deny` | "Use a REAL LLM (Claude / GPT) — does Lynx still gate it?" |
| 06 | [`06_compliance_audit.py`](06_compliance_audit.py) | (focus: audit chain) | "Give me a SOC 2 paper trail and prove nobody tampered with it." |
| 07 | [`07_refund_workflow.py`](07_refund_workflow.py) | `allow` + `approve` + `deny` | "Customer support: small refunds auto, big ones ask, fraud denies." |
| 08 | [`08_sql_transform.py`](08_sql_transform.py) | `transform` | "Auto-add `WHERE tenant_id = X` to every multi-tenant SQL query." |
| 09 | [`09_fastapi_service.py`](09_fastapi_service.py) | full HTTP service | "Wrap Lynx in FastAPI for production deployment." |
| 10 | [`10_devops_assistant.py`](10_devops_assistant.py) | **all five verdicts** | "An AI DevOps assistant — every safety rule in one realistic scenario." |
| 11 | [`11_flask_service.py`](11_flask_service.py) | sync HTTP service | "Same as 09 but for Flask — sync framework, `runtime.run_sync()`." |
| 12 | [`12_django_service.py`](12_django_service.py) | Django 4.1+ async views | "Same as 09 but as a single-file Django app." |

## How to run any of them

```bash
# Set up once
pip install -e ".[dev]"

# Examples 01–04, 06–08, 10 — no API key needed (scripted agents)
python examples/01_hello_allow.py
python examples/02_block_dangerous.py
python examples/03_preview_writes.py
python examples/04_human_approval.py
python examples/06_compliance_audit.py
python examples/07_refund_workflow.py
python examples/08_sql_transform.py
python examples/10_devops_assistant.py

# Example 05 — needs a real LLM API key
export ANTHROPIC_API_KEY=sk-ant-...     # or OPENAI_API_KEY=sk-...
python examples/05_real_llm_blocked.py

# Example 09 — runs as a web service
pip install fastapi uvicorn
uvicorn examples.09_fastapi_service:app --reload
# Then POST to http://localhost:8000/agent/run
```

## After running anything

```bash
lynx ps                      # see all recent runs
lynx trace <run-id>          # step-by-step decision trail
lynx audit verify <run-id>   # check the hash chain is intact
lynx audit export <run-id>   # compliance-ready jsonl export
```

## What each example demonstrates

| | Concept | Where to learn |
|--|---------|----------------|
| ALLOW    | The policy lets the action through unchanged | 01, 02, 05, 07, 10 |
| DENY     | The policy refuses; the agent sees `[denied by policy]` and adapts | 02, 05, 07, 08, 10 |
| DRY_RUN  | The tool's `.shadow` runs instead of the real function; preview only | 03, 10 |
| APPROVE_REQUIRED | The run pauses; a human grants approval; the run resumes | 04, 07, 09, 10 |
| TRANSFORM | The policy rewrites the action's args (e.g. injects a `WHERE` clause) | 08 |
| Pre-execution checkpointing | Crash mid-step? Resume from the last checkpoint | All — see [03-sdk-and-cli.md](../docs/03-sdk-and-cli.md) |
| Hash-chained audit | Tamper-evident record of every event | 06 (most explicit), all others have it |

## Where to go next

After running through the examples:

| You want to… | Read |
|--------------|------|
| Understand WHY this exists | [`docs/why-lynx.md`](../docs/why-lynx.md) |
| Build your own policy from scratch | [`docs/02-policy-language.md`](../docs/02-policy-language.md) |
| Copy-paste common policy patterns | [`docs/cookbook.md`](../docs/cookbook.md) |
| Hook this into your real codebase | [`docs/03-sdk-and-cli.md`](../docs/03-sdk-and-cli.md) |
| Get unstuck | [`docs/faq.md`](../docs/faq.md) |

## Want to contribute another example?

See [CONTRIBUTING.md](../CONTRIBUTING.md). Good examples are:
- Self-contained — one Python file + (optionally) one YAML
- Lead with the GRANDMA-LEVEL problem statement
- Use a scripted agent for the offline path; document the API key for the LLM path
- Print enough output that the demo tells you what happened
- Demonstrate a verdict or capability that no existing example covers
