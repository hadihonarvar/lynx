# Integration Cookbook

Wiring patterns for plugging your storage / observability / approval systems into Lynx. Each recipe is small on purpose — Lynx's job stops at the protocol; the connection, schema, retention, and failure modes are yours.

**Lynx imports nothing from these systems.** The patterns below assume you've already installed the relevant Python client (`pip install psycopg-binary`, `pip install redis`, etc.). Lynx itself stays at three runtime dependencies (`click`, `pyyaml`, `rich`).

---

## Sinks — where AuditEvents go

A `Sink` is `async def __call__(event: AuditEvent) -> None`. Five to ten lines of glue is usually enough.

### SQLite

For dev, single-node services, or small audit archives. SQLite's driver is sync, so wrap it with `asyncio.to_thread` to keep the event loop responsive.

```python
import asyncio
import sqlite3
from lynx import callback_sink, run_agent
from lynx.core.types import canonical_json

conn = sqlite3.connect("audit.db")
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS events (
        correlation_id TEXT,
        bundle_id      TEXT,
        seq            INTEGER,
        kind           TEXT,
        timestamp      TEXT,
        body           TEXT,
        PRIMARY KEY (correlation_id, seq)
    )
    """
)
conn.commit()

def _write(event):
    conn.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
        (
            event.correlation_id,
            event.bundle_id,
            event.seq,
            event.kind,
            event.timestamp.isoformat(),
            canonical_json(dict(event.body)),
        ),
    )
    conn.commit()

async def sqlite_sink(event):
    await asyncio.to_thread(_write, event)

await run_agent(..., sinks=(callback_sink(sqlite_sink),))
# When the program shuts down: conn.close()
```

### PostgreSQL (asyncpg)

True async driver — no `to_thread` needed. Pool connections at startup; one acquire per event is fine for typical throughput.

```python
import asyncpg
from lynx import callback_sink, run_agent
from lynx.core.types import canonical_json

pool = await asyncpg.create_pool(dsn="postgres://...", min_size=2, max_size=10)
await pool.execute(
    """
    CREATE TABLE IF NOT EXISTS lynx_events (
        correlation_id TEXT,
        bundle_id      TEXT,
        seq            INTEGER,
        kind           TEXT,
        ts             TIMESTAMPTZ,
        body           JSONB,
        PRIMARY KEY (correlation_id, seq)
    )
    """
)

async def pg_sink(event):
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO lynx_events VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
            event.correlation_id, event.bundle_id, event.seq,
            event.kind, event.timestamp, canonical_json(dict(event.body)),
        )

await run_agent(..., sinks=(callback_sink(pg_sink),))
# When shutting down: await pool.close()
```

### JSONL files (built-in)

Already shipped — `jsonl_sink(handle)`. One JSON object per line; easy to grep, easy to ship to S3 nightly.

```python
from lynx import jsonl_sink, run_agent

with open("audit.jsonl", "a") as f:
    await run_agent(..., sinks=(jsonl_sink(f),))
```

The handle is yours — Lynx never opens or closes it. `jsonl_sink` flushes after every record so a crash mid-run does not drop buffered events.

### OpenTelemetry logs

Map Lynx events onto OTel log records. (You can do spans instead — see the note below.)

```python
from opentelemetry import _logs
from opentelemetry.sdk._logs import LoggerProvider, LogRecord
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from lynx import callback_sink, run_agent

provider = LoggerProvider()
provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
_logs.set_logger_provider(provider)
otel_logger = _logs.get_logger("lynx")

async def otel_sink(event):
    otel_logger.emit(
        LogRecord(
            timestamp=int(event.timestamp.timestamp() * 1e9),
            severity_text="INFO",
            body=event.kind,
            attributes={
                "lynx.correlation_id": event.correlation_id,
                "lynx.bundle_id":      event.bundle_id,
                "lynx.seq":            event.seq,
                **{f"lynx.body.{k}": str(v) for k, v in event.body.items()},
            },
        )
    )

await run_agent(..., sinks=(callback_sink(otel_sink),))
```

> **Spans instead of logs?** Wrap each `run_agent` call in `tracer.start_as_current_span("lynx.run")` and emit `span.add_event(event.kind, attributes=...)` from the sink. The correlation_id maps naturally to a trace id; the bundle_id becomes a span attribute.

### Splunk (HTTP Event Collector)

Splunk HEC takes JSON over HTTPS with a token header. Send the event verbatim; Splunk indexes it automatically.

```python
import httpx
from lynx import callback_sink, run_agent

HEC_URL   = "https://splunk.example.com:8088/services/collector/event"
HEC_TOKEN = "00000000-0000-0000-0000-000000000000"   # from Splunk Settings → Data Inputs → HEC

# Reuse one client across the whole process — the HTTP/2 pool matters.
http = httpx.AsyncClient(
    timeout=5.0,
    headers={"Authorization": f"Splunk {HEC_TOKEN}"},
)

async def splunk_sink(event):
    payload = {
        # Splunk envelope
        "time":       event.timestamp.timestamp(),
        "host":       "lynx",
        "source":     "lynx-kernel",
        "sourcetype": "lynx:audit",
        "index":      "main",            # or whichever index you configured
        # The actual event — Splunk indexes the JSON keys
        "event": {
            "correlation_id": event.correlation_id,
            "bundle_id":      event.bundle_id,
            "seq":            event.seq,
            "kind":           event.kind,
            "body":           dict(event.body),
        },
    }
    await http.post(HEC_URL, json=payload)

await run_agent(..., sinks=(callback_sink(splunk_sink),))
# When shutting down: await http.aclose()
```

A few production details that matter:

- **Self-signed certs?** Pass `verify=False` to `httpx.AsyncClient(...)` for dev only. In prod, point `verify="/path/to/ca.pem"`.
- **Batching for high volume.** One HTTP POST per event is fine up to a few hundred events/sec. Above that, buffer events and send them in batches to `/services/collector/event/1.0` (Splunk accepts newline-delimited JSON in one POST).
- **HEC ack mode.** If you can't tolerate dropped events, enable HEC indexer acknowledgment in Splunk and add the `X-Splunk-Request-Channel` header + an `acks` follow-up call. Otherwise the default best-effort behavior (failures logged to stderr, run continues) is usually right.
- **Retry policy.** Wrap with `tenacity` if you want exponential backoff for transient 5xx errors. Don't retry forever — that would block the next step.

### Generic HTTP POST

Send events to any endpoint that accepts JSON — SaaS audit services, internal microservices, custom webhooks.

```python
import httpx
from lynx import callback_sink, run_agent
from lynx.core.types import canonical_json

http = httpx.AsyncClient(timeout=5.0)

async def http_sink(event):
    record = {
        "correlation_id": event.correlation_id,
        "bundle_id":      event.bundle_id,
        "seq":            event.seq,
        "kind":           event.kind,
        "timestamp":      event.timestamp.isoformat(),
        "body":           dict(event.body),
    }
    await http.post("https://audit.example.com/events", json=record)

await run_agent(..., sinks=(callback_sink(http_sink),))
# When done: await http.aclose()
```

If the endpoint is critical, wrap with retry/backoff (`tenacity`, `httpx-retries`). If best-effort is fine, the default is correct — a failed POST logs to stderr; the run continues.

---

## Approval handlers — cross-process humans-in-the-loop

An `ApprovalHandler` is `async def __call__(req: ApprovalRequest) -> ApprovalDecision`. It runs inside the run loop; the kernel `await`s it. The mediator wraps the call in `asyncio.wait_for(handler, decision.timeout_seconds)`, so a hanging handler can never hang the run — it just becomes an auto-deny after the timeout.

### Slack interactive message

```python
from lynx import ApprovalDecision, callback_approval, run_agent
import myapp.slack as slack   # your Slack-bot client; your code

async def slack_approval(req):
    msg = await slack.post_message(
        channel="#approvals",
        text=f"Approve `{req.request.tool}` "
             f"with args `{dict(req.request.args)}`?\n"
             f"Rule: {req.decision.reason}",
        buttons=["approve", "deny"],
    )
    click = await slack.wait_for_click(msg, timeout=3600)
    return ApprovalDecision(
        granted=(click.value == "approve"),
        approver=click.user_id,
        reason=click.comment or "",
    )

await run_agent(..., on_approval=callback_approval(slack_approval))
```

### Email + reply link

```python
import secrets
from lynx import ApprovalDecision, callback_approval, run_agent

async def email_approval(req):
    token = secrets.token_urlsafe(16)
    await your_email.send(
        to="oncall@example.com",
        subject=f"Approve {req.request.tool}?",
        body=f"Approve: https://your-app/approve/{token}\n"
             f"Deny:    https://your-app/deny/{token}",
    )
    granted = await your_approval_db.wait_for(token, timeout=3600)
    return ApprovalDecision(granted=granted, approver="email-link")
```

`your_approval_db.wait_for(token, ...)` is your code — a row/key that your link-receiver updates when the human clicks.

### PagerDuty / Opsgenie / generic queue

Identical shape: post to your system, block on a queue / DB / webhook receiver, return the decision.

---

## Patterns not shown — they're the same shape

| You want to plug in | Pattern is identical to | Note |
|---|---|---|
| Redis Streams (`XADD`) | SQLite | Use `redis.asyncio` — no `to_thread` needed |
| Kafka | SQLite | `aiokafka.AIOKafkaProducer.send_and_wait(...)` |
| AWS S3 / R2 / GCS | HTTP | Batch events; flush periodically. One PUT per event is expensive |
| Datadog Logs | HTTP | POST to `https://http-intake.logs.datadoghq.com/api/v2/logs` |
| Elasticsearch / OpenSearch | HTTP | POST to `/<index>/_doc` |
| Discord webhook approval | Slack | Replace Slack client with `aiohttp.post(webhook_url, json=...)` and poll a reply channel |
| MongoDB | SQLite | `motor` is async — no `to_thread` needed |

The pattern is always: a 5–10 line `async def my_sink_or_handler(...)` that talks to the service of your choice, then `sinks=(callback_sink(my_sink),)` or `on_approval=callback_approval(my_handler)` at the `run_agent` call site.

---

## OpenTelemetry — emit Lynx events as GenAI spans

Lynx ships no tracer (zero dependencies), but its audit stream is the exact
data an observability backend wants. A sink can map each `AuditEvent` to an
OpenTelemetry span using the [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
— so Lynx becomes a drop-in source for Langfuse, Phoenix, Datadog, Grafana,
or any OTLP collector, without Lynx depending on any of them.

```python
# pip install opentelemetry-sdk opentelemetry-exporter-otlp  (YOUR deps)
from opentelemetry import trace
from lynx import callback_sink

tracer = trace.get_tracer("lynx")

def otel_sink():
    spans = {}  # correlation_id -> the run's root span
    def sink_fn():
        async def sink(event):
            if event.kind == "run.started":
                spans[event.correlation_id] = tracer.start_span(
                    "lynx.run", attributes={"gen_ai.operation.name": "agent"}
                )
            elif event.kind == "step.usage":
                # GenAI-convention attributes — Lynx's Usage fields already match
                span = spans.get(event.correlation_id)
                if span:
                    span.add_event("gen_ai.usage", attributes={
                        "gen_ai.usage.input_tokens": event.body.get("input_tokens") or 0,
                        "gen_ai.usage.output_tokens": event.body.get("output_tokens") or 0,
                        "gen_ai.request.model": event.body.get("model") or "",
                    })
            elif event.kind in ("action.completed", "action.denied", "action.failed"):
                span = spans.get(event.correlation_id)
                if span:
                    span.add_event(event.kind, attributes={"lynx.seq": event.seq})
            elif event.kind in ("run.succeeded", "run.failed", "run.cancelled"):
                span = spans.pop(event.correlation_id, None)
                if span:
                    span.set_attribute("lynx.outcome", event.kind)
                    span.end()
        return sink
    return callback_sink(sink_fn())

await run_agent(..., sinks=(otel_sink(),))
```

`Usage` field names (`input_tokens`, `output_tokens`, `cache_read_tokens`)
are already aligned to the GenAI convention, so the mapping is a rename, not
a transform. Lynx is the firehose; OTel is one consumer of it.

## Consume the audit stream as a live agent-event feed

The same events drive a UI ("show me what the agent is doing"): tool
proposals, approvals, completions, cancellations all flow through your sink
in real time. A queue-backed sink turns the push stream into an async
iterator a web handler can `async for` over:

```python
import asyncio
from lynx import callback_sink

def event_feed():
    q: asyncio.Queue = asyncio.Queue()
    async def sink(event):
        await q.put(event)
    async def stream():
        while True:
            event = await q.get()
            yield event
            if event.kind in ("run.succeeded", "run.failed", "run.cancelled"):
                return
    return callback_sink(sink), stream

sink, stream = event_feed()
task = asyncio.create_task(run_agent(..., sinks=(sink,)))
async for event in stream():          # push to SSE / WebSocket / your UI
    ...                               # token streaming itself stays the provider's job
await task
```

---

## Executors — bring your own isolation

The executor seam is one async callable: `(request, tool) -> ActionResult`.
Lynx ships `inline_executor` / `subprocess_executor` / `route_executor`;
real isolation is yours. Two recipes:

### Docker (~20 lines, `aiodocker` or subprocess + docker CLI)

```python
import asyncio, json
from lynx import ActionRequest, ActionResult, ToolDef

IMAGE = "python:3.12-slim"  # bake your tools module into your own image

async def docker_executor(request: ActionRequest, tool: ToolDef) -> ActionResult:
    payload = json.dumps({"tool": tool.name, "args": dict(request.args)})
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm", "--network=none", "--memory=512m",
        "--cpus=1", "--read-only", "-i", IMAGE,
        "python", "-m", "my_tools.runner",          # reads payload on stdin,
        stdin=asyncio.subprocess.PIPE,              # prints JSON result
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(payload.encode()), timeout=120)
    if proc.returncode != 0:
        return ActionResult(ok=False, error=f"container exited {proc.returncode}: {err.decode()[-500:]}")
    return ActionResult(ok=True, value=json.loads(out))
```

`--network=none` is the point: the tool cannot exfiltrate even if the model
was prompt-injected. Wire it per-tool:

```python
executor = route_executor({None: inline_executor(), "container": docker_executor})
# @tool(isolation="container") tools run in Docker; everything else inline.
```

### E2B / hosted sandboxes

Same shape — call the provider's SDK inside the executor and return an
`ActionResult`. The executor owns the sandbox lifecycle (create per call, or
hold a warm one in the closure and `aclose()` it when your app shuts down —
Lynx never closes your resources).

---

## Durability — RunStore recipes

Lynx ships **no storage**. Pass `run_agent(..., store=..., run_id=...)` a
`RunStore` you implement over whatever you already run. The whole contract:
`append` must atomically reject a duplicate `(run_id, seq)` by raising
`DuplicateRecord`; `load` returns a run's records ordered by `seq`. That
uniqueness rule is what makes concurrent re-dispatch safe — the write-ahead
intent journaled before each action *is* the claim.

### In-memory (tests)

```python
from lynx import DuplicateRecord, StepRecord

class MemoryRunStore:
    def __init__(self):
        self.records: dict[tuple[str, int], StepRecord] = {}

    async def append(self, record: StepRecord) -> None:
        key = (record.run_id, record.seq)
        if key in self.records:
            raise DuplicateRecord(f"{key} already journaled")
        self.records[key] = record

    async def load(self, run_id: str):
        return sorted((r for (rid, _), r in self.records.items() if rid == run_id),
                      key=lambda r: r.seq)
```

### Redis (`redis.asyncio`)

`HSETNX` is the atomic insert-if-absent. **Plain `RPUSH` does NOT satisfy
the contract** — a list happily accepts two records at the same position.

```python
from lynx import DuplicateRecord, StepRecord, step_record_from_json, step_record_to_json

class RedisRunStore:
    def __init__(self, redis):                      # redis.asyncio.Redis
        self.redis = redis

    async def append(self, record: StepRecord) -> None:
        created = await self.redis.hsetnx(
            f"lynx:run:{record.run_id}", str(record.seq), step_record_to_json(record)
        )
        if not created:
            raise DuplicateRecord(f"({record.run_id}, {record.seq}) already journaled")

    async def load(self, run_id: str):
        raw = await self.redis.hgetall(f"lynx:run:{run_id}")
        return sorted((step_record_from_json(v) for v in raw.values()),
                      key=lambda r: r.seq)
```

### Postgres (`asyncpg`)

The primary key does the work; catch the unique violation.

```python
import asyncpg
from lynx import DuplicateRecord, StepRecord, step_record_from_json, step_record_to_json

# CREATE TABLE lynx_runs (
#     run_id TEXT, seq BIGINT, record JSONB NOT NULL,
#     PRIMARY KEY (run_id, seq)
# );

class PostgresRunStore:
    def __init__(self, pool):                       # asyncpg.Pool
        self.pool = pool

    async def append(self, record: StepRecord) -> None:
        try:
            await self.pool.execute(
                "INSERT INTO lynx_runs (run_id, seq, record) VALUES ($1, $2, $3)",
                record.run_id, record.seq, step_record_to_json(record),
            )
        except asyncpg.UniqueViolationError as exc:
            raise DuplicateRecord(str(exc)) from exc

    async def load(self, run_id: str):
        rows = await self.pool.fetch(
            "SELECT record FROM lynx_runs WHERE run_id = $1 ORDER BY seq", run_id
        )
        return [step_record_from_json(r["record"]) for r in rows]
```

### JSONL file (single process only)

Fine for a laptop or CI; a flat file cannot enforce uniqueness across
processes, so the supersede guarantee only holds within one process. Use the
`step_record_to_json` format and `lynx trace <file>` can render it.

### Picking a backend

| Need | Backend |
|---|---|
| Tests / demos | dict (above) |
| Survive a process crash, one machine | JSONL file or your SQLite |
| Survive machine loss, multiple workers | Redis / Postgres / DynamoDB — *your* database |

Durability needs no database; **distributed** durability needs *your*
database. Lynx never restarts a dead process — your supervisor retries the
`run_agent` call; the journal makes that retry cheap (no re-burned tokens)
and safe (no double side effects).

---

## Durability — or wrap run_agent in a workflow engine

If you already run Temporal/Restate/Inngest, you can skip RunStore entirely and let the engine own retries — wrap `run_agent` as an activity. Temporal example:

```python
from datetime import timedelta
from temporalio import workflow, activity
from lynx import run_agent

@activity.defn
async def lynx_run_activity(task: str) -> dict:
    result = await run_agent(...)
    return {
        "correlation_id": result.correlation_id,
        "bundle_id":      result.bundle_id,
        "final_answer":   result.final_answer,
        "error":          result.error,
        "steps_taken":    result.steps_taken,
    }

@workflow.defn
class AgentWorkflow:
    @workflow.run
    async def run(self, task: str) -> dict:
        return await workflow.execute_activity(
            lynx_run_activity,
            task,
            start_to_close_timeout=timedelta(minutes=10),
        )
```

Temporal owns the durable state, the retry, and the resume. Lynx stays the pure-function policy kernel inside.

Restate / Inngest / Trigger.dev follow the same shape — wrap `run_agent` as a step / function / task.

For approvals across a workflow restart, write a Temporal-aware `on_approval` handler that emits a workflow signal and blocks on it. The workflow can survive arbitrary delays; the handler returns when the signal arrives.

---

## Closing notes

- **Lynx never closes your resources.** If your sink opens a DB connection or your handler holds an HTTP client, close them when your program shuts down.
- **Sink failures are best-effort.** When a sink raises, the run continues and the failure goes to stderr. If you need fail-closed (no side effects allowed without an audit record), put the audit write inside the tool body itself and let it raise.
- **Approval timeouts are enforced.** The mediator wraps `on_approval` in `asyncio.wait_for(handler, decision.timeout_seconds)`. A hanging handler becomes a deny, automatically.
- **Lynx imports nothing from your storage stack.** Every snippet above is *your* code, your dependency, your retention policy. The library's runtime dependencies stay at `click + pyyaml + rich`.
