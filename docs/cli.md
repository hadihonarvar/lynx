# CLI reference

The `lynx` command ships with the core package (no extras needed) — `pip install
lynx-agent` puts it on your `PATH` via the `lynx` entry point. It is deliberately
small: Lynx is a library, so the CLI only covers the things you do *around* a run
(scaffold a policy, lint it, run a script, and inspect the two kinds of file Lynx
helps you produce — a hash-chained **audit log** and a durable **run journal**).

Everything the CLI does is also available as a Python function; the CLI is a thin
convenience wrapper, never the only way in.

```text
lynx
├── --version                 print the installed version and exit
├── --help                    show help (works on every command)
├── init                      write a starter policy.yaml
├── run     <script>          run a Python script's async main()
├── verify  <audit.jsonl>     check a hash-chained audit log
├── trace   <records.jsonl>   reconstruct a durable run journal
└── policy
    ├── lint        [path]    compile-check a policy + print a rule summary
    └── bundle-id   [path]    print a policy's content-addressed id
```

**Contents**

- [Conventions](#conventions) — exit codes, stdout vs stderr, help
- [`lynx --version`](#lynx---version)
- [`lynx init`](#lynx-init)
- [`lynx run`](#lynx-run)
- [`lynx verify`](#lynx-verify)
- [`lynx trace`](#lynx-trace)
- [`lynx policy lint`](#lynx-policy-lint)
- [`lynx policy bundle-id`](#lynx-policy-bundle-id)
- [Which command do I use?](#which-command-do-i-use)
- [Two file formats, side by side](#two-file-formats-side-by-side)

---

## Conventions

These hold for every command.

| Aspect | Behavior |
|---|---|
| **Exit code** | `0` on success; `1` on any handled error (file missing, parse error, broken chain, validation failure). Click itself returns `2` for usage errors (unknown command/flag, missing required argument). |
| **stdout vs stderr** | Successful, parseable output goes to **stdout**. Errors and diagnostics go to **stderr**. This makes the success output safe to pipe. |
| **`--help`** | Available on the group and every command: `lynx --help`, `lynx init --help`, `lynx policy lint --help`, … |
| **File arguments** | Paths are checked for existence by the CLI; a missing path is a usage error (exit `2`) with a Click message, not a traceback. |

Scripting tip: because errors go to stderr and exit non-zero, you can gate a
pipeline on a command — e.g. `lynx verify audit.jsonl && ship_to_s3 audit.jsonl`.

---

## `lynx --version`

Print the installed Lynx version and exit.

```console
$ lynx --version
lynx, version 2.11.0
```

- **Arguments / options:** none.
- **Exit codes:** `0`.

---

## `lynx init`

Write a starter `policy.yaml` so you have something valid to edit on day one.

### Synopsis

```text
lynx init [--dir <path>] [--force]
```

### Options

| Option | Default | Description |
|---|---|---|
| `--dir <path>` | `.` (current directory) | Directory to write `policy.yaml` into. **Created if it doesn't exist** (`mkdir -p` semantics). |
| `--force` | off | Overwrite an existing `policy.yaml`. Without it, init refuses to clobber a file you already have. |

### Behavior & output

Writes a single file — `<dir>/policy.yaml` — and nothing else (no config, no
state dir). On success:

```console
$ lynx init
wrote /abs/path/policy.yaml
```

The starter policy is intentionally small and safe-by-default — read-only tools
allowed, `rm -rf /` hard-denied, irreversible tools sent to approval:

```yaml
version: 1
defaults:
  on_missing_shadow: approve_required
  on_no_match: deny

rules:
  - id: read-only-allow
    description: Read-only tools are always fine
    match:
      declared.scope.contains_any: ["filesystem:read", "net:read"]
    decision: allow

  - id: shell-rm-rf-root
    description: Never delete from filesystem root
    match:
      tool: shell
      args.cmd.matches: '^\s*rm\s+(-[rRf]+\s+)+/(\s|$)'
    decision: deny
    reason: "rm -rf / is never allowed"

  - id: irreversible-needs-approval
    description: Irreversible actions require explicit approval
    match:
      declared.reversible: false
    decision: approve_required
```

### Exit codes

| Code | When |
|---|---|
| `0` | File written. |
| `1` | `policy.yaml` already exists and `--force` was not passed; or the directory/file could not be created/written (permissions, read-only FS). The reason is printed to stderr. |

### Examples

```console
$ lynx init                          # ./policy.yaml
$ lynx init --dir services/billing   # creates the dir if needed
$ lynx init --force                  # overwrite an existing policy.yaml
```

---

## `lynx run`

Run a Python script that defines an `async def main()` coroutine. The script
owns the `await run_agent(...)` call — `lynx run` just imports the file and
`asyncio.run`s its `main()`. It is a convenience for examples and one-offs, not
a process supervisor.

### Synopsis

```text
lynx run <script>
```

### Arguments

| Argument | Required | Description |
|---|---|---|
| `<script>` | yes | Path to a `.py` file. Must exist. |

### Behavior

1. The script's **directory is temporarily prepended to `sys.path`** so sibling
   imports work, then removed when the command finishes (so `lynx run` is safe to
   call from inside another Python process without leaking `sys.path`).
2. The file is executed; its module namespace must contain a coroutine function
   named **`main`**.
3. `main()` is run to completion with `asyncio.run`.

```python
# my_run.py
from lynx import ToolSet, tool, compile_policy, run_agent, stdout_sink, auto_deny

async def main():
    result = await run_agent(my_agent, task="...", tools=tools,
                             policy=policy, sinks=(stdout_sink(),),
                             on_approval=auto_deny("n/a"))
    print(result.final_answer)
```
```console
$ lynx run my_run.py
```

### Exit codes

| Code | When |
|---|---|
| `0` | `main()` ran to completion. |
| `1` | The script has no `main`, or `main` is not an `async def` coroutine function. Message printed to stderr. |
| `2` | `<script>` does not exist (Click usage error). |
| (other) | An uncaught exception inside `main()` propagates and exits with a traceback — your script's error, surfaced as-is. |

---

## `lynx verify`

Verify the integrity of a **hash-chained audit log** — a file written by
[`hash_chained_sink`](../README.md#tamper-evident-audit). It re-walks the file
and confirms every line's fingerprint recomputes and links to the line before
it, so an edited body, a deleted denial, or reordered events are all caught.
Equivalent to the `verify_chain(path)` function.

### Synopsis

```text
lynx verify <audit.jsonl>
```

### Arguments

| Argument | Required | Description |
|---|---|---|
| `<audit.jsonl>` | yes | A JSONL file produced by `hash_chained_sink`. Must exist. |

### Output & exit codes

Intact — printed to **stdout**, exit `0`:

```console
$ lynx verify audit.jsonl
intact: 42 events, chain verified
```

Broken — printed to **stderr**, exit `1`. The line number is 1-based and points
at the first bad link:

```console
$ lynx verify audit.jsonl
broken at line 17: hash mismatch (line was modified)
```

| Code | When |
|---|---|
| `0` | Chain intact (`VerifyResult.intact == True`). |
| `1` | A fingerprint failed to recompute or link — the file was altered, truncated, or reordered. |
| `2` | `<audit.jsonl>` does not exist. |

> This proves **tamper-evidence** (nobody altered the log undetected), not
> tamper-*proofing*. See [`examples/37_tamper_evident_audit.py`](../examples/37_tamper_evident_audit.py).

---

## `lynx trace`

Reconstruct what happened in a **durable run journal** — a JSONL file of
`StepRecord`s in the `step_record_to_json` format that file-backed `RunStore`
implementations write (see the [integration cookbook](integration-cookbook.md)).
It prints the per-step history: tool, verdict, outcome, the resume/uncertainty
markers, and the final answer. Equivalent to the `replay(records)` function.

> A journal is **not** an audit-sink file. `jsonl_sink` output (keyed by
> `correlation_id`) is *not* a journal (keyed by `run_id`); `trace` detects this
> and tells you. Use [`verify`](#lynx-verify) for audit logs, `trace` for journals.

### Synopsis

```text
lynx trace <records.jsonl> [--run-id <id>]
```

### Arguments & options

| Name | Required | Description |
|---|---|---|
| `<records.jsonl>` | yes | A JSONL file of `StepRecord`s. Must exist. |
| `--run-id <id>` | conditionally | Show only this run. **Required when the file contains more than one run** — replay keys steps by step number, so mixing runs would produce a plausible-but-wrong reconstruction. With a single run in the file it is optional. |

### Output

```console
$ lynx trace journal.jsonl
run invoice-2026-0611: 7 records, 2 attempt(s)
  step 1: get_invoice verdict=allow ok
  step 2: charge_card verdict=approve_required ok
  step 3: send_receipt verdict=allow failed
           SMTP connection refused
  step 4: charge_card verdict=deny ok  [resolved uncertain retry — the original attempt may still have executed]
  step 5: final answer: Refund processed and receipt sent.
final: Refund processed and receipt sent.
```

Reading the output:

- **Header** — `run <id>: <N> records, <A> attempt(s)`. `attempts` is `1 + the
  number of resume markers`, so `2 attempt(s)` means the run crashed and resumed once.
- **Per step** — `step <n>: <tool> verdict=<verdict> <status>`, where status is
  `ok` / `failed` / `?` (no result journaled). A truncated tool message (≤100
  chars) is printed on the next line when present.
- **Final-answer step** — printed as `step <n>: final answer: <msg>`.
- **Markers** that explain durability edge cases:
  - `[UNCERTAIN: intent without result — action may have executed]` — the intent
    was journaled but no result was; on resume Lynx re-proposes it to policy.
  - `[resolved uncertain retry — the original attempt may still have executed]` —
    this step's result came from re-deciding such an uncertain action.
- **Footer** — `final: <answer>`, or `final: (run incomplete)` if the run never
  reached a final answer.

### Exit codes

| Code | When |
|---|---|
| `0` | Journal reconstructed and printed. |
| `1` | A line is not JSON; a line is not a `StepRecord` (no `run_id`); the file looks like an **audit-sink** file (`correlation_id` present) rather than a journal; **no records** matched; or the file contains **multiple runs** and no `--run-id` was given (the message lists the run ids to choose from). |
| `2` | `<records.jsonl>` does not exist. |

### Examples

```console
$ lynx trace journal.jsonl                       # single-run file
$ lynx trace shared.jsonl --run-id invoice-0611  # one run out of many
```

---

## `lynx policy lint`

Compile-check a policy file and print a summary of the rules it produced. Use it
in CI to catch policy mistakes before deploy. Equivalent to `load_policy_file(path)`
plus a summary.

### Synopsis

```text
lynx policy lint [path]
```

### Arguments

| Argument | Required | Default | Description |
|---|---|---|---|
| `[path]` | no | `policy.yaml` | The policy YAML to compile. |

### Output & exit codes

Valid — exit `0`, summary to stdout (one line per rule, in evaluation order):

```console
$ lynx policy lint policy.yaml
3 rules compiled cleanly
  read-only-allow (priority 0) - Read-only tools are always fine
  shell-rm-rf-root (priority 0) - Never delete from filesystem root
  irreversible-needs-approval (priority 0) - Irreversible actions require explicit approval
```

Invalid — exit `1`, the compiler error (type + message) to stderr:

```console
$ lynx policy lint broken.yaml
PolicyCompileError: Rule 'large-refund': obligations[0].phase must be 'pre' or 'post', got 'after'
```

| Code | When |
|---|---|
| `0` | Compiled cleanly. |
| `1` | The file is missing, unreadable, not valid YAML, or fails policy compilation (unknown operator, bad regex, malformed rule, invalid obligation, …). |

See the [policy language reference](02-policy-language.md) for what the compiler
validates and the full error model.

---

## `lynx policy bundle-id`

Print a policy's **content-addressed id** — a stable hash of the compiled
bundle. Two files that compile to the same rules share an id; any meaningful
change produces a new one. Useful for pinning "which policy was in force",
asserting a deploy shipped the policy you expected, or tagging audit records.

### Synopsis

```text
lynx policy bundle-id [path]
```

### Arguments

| Argument | Required | Default | Description |
|---|---|---|---|
| `[path]` | no | `policy.yaml` | The policy YAML to identify. |

### Output & exit codes

```console
$ lynx policy bundle-id policy.yaml
b3a1c9f4e2d7…           # the bundle.id
```

| Code | When |
|---|---|
| `0` | Printed the id. |
| `1` | The file is missing, unreadable, or fails to compile (same conditions as `lint`). |

---

## Which command do I use?

| I want to… | Command |
|---|---|
| Start a new project with a sane policy | `lynx init` |
| Catch a policy mistake before deploying | `lynx policy lint` |
| Pin / compare which policy is in force | `lynx policy bundle-id` |
| Run an example or a one-off script | `lynx run <script>` |
| Prove an **audit log** wasn't tampered with | `lynx verify <audit.jsonl>` |
| See what happened in a crashed/resumed **run** | `lynx trace <journal.jsonl>` |

---

## Two file formats, side by side

`verify` and `trace` operate on two different files Lynx helps you produce.
Mixing them is the most common mistake, so here's the distinction:

| | Audit log | Run journal |
|---|---|---|
| **Written by** | `hash_chained_sink` (a sink) | your `RunStore` (file-backed), via `step_record_to_json` |
| **One line is** | an `AuditEvent` (`correlation_id`) | a `StepRecord` (`run_id`) |
| **Purpose** | tamper-evident history of every event | crash-safe replay of a single run |
| **Inspect with** | `lynx verify` / `verify_chain()` | `lynx trace` / `replay()` |

`trace` will explicitly tell you if you point it at an audit-sink file by
mistake (and vice-versa there is no overlap, since `verify` only checks chained
hashes).

---

*Everything here is also a Python API: [`verify_chain`](../README.md#tamper-evident-audit),
[`replay`](../README.md#durability--crash-resume-without-double-side-effects),
[`load_policy_file` / `compile_policy`](02-policy-language.md). The CLI never does
anything the library can't.*
