# Policy Language Spec

The contract between the operator (who writes policy) and the kernel (which enforces it). Two tiers of expressiveness — YAML rules + optional Python rules. Both compile to the same internal representation: an immutable `PolicyBundle`.

---

## Goals

1. **Reviewable in a pull request.** A non-Python reader can understand what a policy does from the YAML alone.
2. **Lintable.** `lynx policy lint` catches mistakes before deployment — see [Error model](#error-model) for what gets caught.
3. **Content-addressed.** Every compiled `PolicyBundle` has a deterministic `id` (first 16 hex chars of sha256 over the canonical compiled form). Pass it to attestation / compliance tooling.
4. **Deterministic.** No network, no clocks, no randomness inside the PDP. Same input → same Decision, always.
5. **Fast.** Sub-millisecond evaluation per request, even with hundreds of rules.
6. **Pure.** The PDP is a function: `evaluate(bundle, request, context) -> Decision`. No globals. No I/O.

---

## Tier 1 — Declarative YAML (covers ~80% of cases)

```yaml
# policy.yaml
version: 1
defaults:
  on_missing_shadow: approve_required   # irreversible tool without a shadow
  on_no_match: deny                     # default-deny if no rule matches

rules:
  - id: allow-read-only
    description: "Read-only tools are always fine"
    match:
      declared.scope.contains_any: ["filesystem:read", "net:read", "compute:read"]
    decision: allow

  - id: shell-rm-rf-root
    description: "Never delete from root"
    priority: 100
    match:
      tool: shell
      args.cmd.matches: '^\s*rm\s+(-[rRf]+\s+)+/(\s|$)'
    decision: deny
    reason: "rm -rf / is never allowed"

  - id: prod-mutations-need-approval
    match:
      context.environment: prod
      declared.scope.contains_any: ["filesystem:write", "db:write", "cloud:write"]
    decision: approve_required
    approvers: ["sre-oncall"]
    timeout_seconds: 1800

  - id: irreversible-dry-run-first
    match:
      declared.reversible: false
    decision: dry_run

  - id: tenant-scope-injection
    description: "Append a tenant filter to writes"
    match:
      tool: sql_exec
      args.sql.matches: '(?i)\b(UPDATE|DELETE)\b'
    decision: transform
    transform:
      jsonpath: "$.args.sql"
      append: " AND tenant_id = 'TENANT-ALICE'"
```

### Rule shape — full field reference

Each entry under `rules:` is a mapping with these fields:

| Field | Required | Type | Default | Notes |
|---|---|---|---|---|
| `id` | no | string | auto (`rule_<idx>`) | Used in audit events and `bundle_id`. Pick something stable. |
| `description` | no | string | `""` | Shown by `lynx policy lint`. Free text. |
| `priority` | no | int | `0` | Higher beats lower. Ties broken by file order. Convention: 100 = hard blocks, 80 = approval, 60 = dry-run, 50 = reads, 10 = catch-all. |
| `match` | yes | predicate | — | See [Predicates](#predicates). |
| `decision` (or `verdict`) | yes | string | — | One of `allow / deny / dry_run / approve_required / transform`. Mixed case is accepted (`Allow` works). |
| `reason` | no | string | `""` | Surfaced to the agent as the tool result on deny / approval-deny. Allowed on every verdict. |
| `approvers` | no | list[string] | `[]` | Opaque to the kernel; passed verbatim to your `on_approval` handler. Use any format that makes sense (`"user:hadi"`, `"sre-oncall@acme.com"`). |
| `timeout_seconds` | no | int | `1800` | Used only for `approve_required`. **Enforced by the mediator**: if the handler does not return within this many seconds, the action is denied. |
| `transform` | only for `decision: transform` | mapping | — | See [Transform decision](#transform-decision). Required for `transform`, rejected for any other verdict. |

If you write `verdict:` instead of `decision:`, the loader accepts it — they are synonyms.

### Predicates

A `match` block is a *predicate*: a boolean expression over the request and context. Predicates compose:

```yaml
# Equality on a single field
match: { tool: shell }

# Multiple fields — implicit AND
match:
  tool: shell
  context.environment: prod

# Explicit composition
match:
  all_of: [<predicate>, ...]          # AND
match:
  any_of: [<predicate>, ...]          # OR
match:
  not: <predicate>                    # negation

# Bare-string reference to a named predicate (see Tier 2)
match: is_destructive_sql
```

Inside `all_of` / `any_of`, each element can be either a **named-predicate string** or an **inline mapping** — even mixed:

```yaml
match:
  all_of:
    - is_kubectl                                       # named predicate
    - { args.command.matches: '^(apply|delete)\b' }    # inline mapping
```

### Operators — leaf predicates

A `match` field with no operator (`tool: shell`) is exact equality. To use an operator, append it to the field path with a dot:

| Operator | Example | Semantics |
|---|---|---|
| `eq` | `args.x.eq: 5` | Same as the no-operator form; explicit. |
| `matches` | `args.cmd.matches: '^rm '` | `re.search(pattern, value)`. False if value is not a string. ReDoS-guarded. |
| `in` | `tool.in: [shell, bash]` | RHS must be a list/tuple/set. True iff the field's value is a member. |
| `contains` | `args.body.contains: "secret"` | True iff `<value> in <field>` (substring on strings, element on lists, key on dicts). |
| `contains_any` | `declared.scope.contains_any: ["fs:write"]` | True iff at least one RHS element is `in` the field. |
| `contains_all` | `declared.scope.contains_all: ["a","b"]` | True iff all RHS elements are `in` the field. |
| `gt` / `ge` / `lt` / `le` | `args.amount_usd.gt: 500` | Python comparisons. False if field is `None`. |
| `between` | `args.x.between: [0, 100]` | Inclusive on both ends. False if field is `None`. RHS must be `[lo, hi]` with `lo <= hi`. |
| `not_between` | `context.extra.hour.not_between: [9, 17]` | Inverse of `between`. |

**`None`-handling**: every operator above returns `False` if the field resolves to `None`. So `match: { args.foo: null }` exact-equality DOES match a missing key; `match: { args.foo.gt: 0 }` does NOT match a missing key.

**Type discipline**: there is no implicit type coercion. `args.amount.gt: "50"` against a numeric `args.amount` raises `TypeError` at evaluation time. That error is recorded as a diagnostic marker in `matched_rules` (see [Rule errors](#rule-errors)) and evaluation continues.

**Typos are caught at compile time.** Writing `args.cmd.matchess` (notice the double `s`) raises `PolicyCompileError` because the trailing segment is close to a known operator. Without the guard, the policy would silently never match.

### Available match fields

Everything in the `ActionRequest` and `ExecutionContext` is addressable:

| Path | Type | Notes |
|---|---|---|
| `tool` | string | The tool name (cannot have child paths — `tool.foo` is invalid). |
| `args.<key>` | any | Free-form. Nested paths supported when the value is itself a `Mapping`: `args.body.subject`. |
| `declared.cost` | `"low" / "medium" / "high"` | From `@tool(cost=...)`. |
| `declared.reversible` | bool | From `@tool(reversible=...)`. |
| `declared.scope` | `tuple[str, ...]` | From `@tool(scope=...)`. Use `contains` / `contains_any` / `contains_all`. |
| `declared.has_shadow` | bool | True if `@tool.shadow` was attached. |
| `declared.blast_radius_hint` | `int | None` | From `@tool(blast_radius_hint=...)`. Opaque to the kernel; for your rules. |
| `context.environment` | string | The `environment=` you passed to `run_agent`. |
| `context.workspace` | string | The `workspace=` you passed to `run_agent`. |
| `context.principal.kind` | `"user" / "service" / "agent"` | From `principal=`. |
| `context.principal.id` | string | |
| `context.principal.name` | `str | None` | |
| `context.correlation_id` | string | The current run's UUID4. |
| `context.step_seq` | int | 0-based step counter within the run. |
| `context.timestamp` | datetime | UTC. Matching against this breaks determinism — avoid. |
| `context.extra.<key>` | any | Free-form. Populate via the `extra=` keyword on `ExecutionContext` when wiring your own scheduler — `run_agent` always passes an empty `extra` today. Don't rely on `context.extra` from `run_agent` today. |

### Decision shapes

```yaml
decision: allow
reason: "(optional)"

# ---
decision: deny
reason: "<why; shown to the agent as a tool result>"

# ---
decision: dry_run
reason: "(optional)"
# Routes to the tool's `.shadow` function.
# If the tool has no shadow, the mediator returns ok=False with an explanatory
# error — distinct from the `defaults.on_missing_shadow` default, which only
# fires when *no rule matches* at all.

# ---
decision: approve_required
approvers: ["sre-oncall@acme.com", "user:hadi"]
timeout_seconds: 1800
reason: "(optional)"
# The kernel calls your `on_approval` handler with the rule's metadata.
# `timeout_seconds` is enforced by the mediator: handler over budget → deny.
# Handler exceptions also → deny (with the exception class in the message).

# ---
decision: transform
transform:
  jsonpath: "$.args.<key>"   # which arg to rewrite
  set: <literal>             # — OR —
  append: <string>           # — OR —
  delete: true
```

### Transform decision

The `transform:` block rewrites one entry under the tool call's `args` mapping. **The `jsonpath` field is NOT a real JSONPath**: only the `$.args.<single_key>` form is supported. The prefix is stripped and the remainder is used as a flat key into the `args` mapping. Writing `$.args.body.subject` would target a top-level key literally named `"body.subject"`, not the nested `subject` inside `body`.

| Field | Notes |
|---|---|
| `jsonpath` | Must start with `$.args`. Compile error if not. |
| `set` | Replace the target key's value with the literal RHS. |
| `append` | Coerce existing value with `str()`, then concatenate the (string) RHS. Useful for `WHERE` injection. |
| `delete: true` | Remove the target key (no-op if it was absent). |

Exactly one of `set` / `append` / `delete` is required per transform; declaring more than one is a compile error.

There is no `${...}` template interpolation at transform time. If you need values from `context.principal.id` (or similar), use a Python rule.

The rewritten args are passed to the tool as `**transform_args`. If your transform produces a key that isn't a parameter of the tool function, the mediator returns `ok=False` with a clean `TypeError` error rather than silently running with the original args.

### Obligations — "allow, *and also* do X"

An `obligations:` block attaches mandatory side-actions to **any** verdict (the XACML/Cedar model). It is *not* a verdict itself — it rides on `allow` / `deny` / `transform` / etc. Each obligation names a handler `id` that you resolve against an `ObligationRegistry` passed to `run_agent(..., obligations={...})`; the kernel ships **no** handlers (mechanism, not policy).

```yaml
- id: large-refund
  match: { tool: refund_customer, args.amount_usd.gt: 1000 }
  decision: allow
  obligations:
    - id: issue-ttl-credential   # mapping form
      phase: pre                 # "pre" | "post"  (default "post")
      params: { seconds: 300 }
    - notify-finance             # bare-string shorthand → phase "post", no params
```

| Field | Notes |
|---|---|
| `id` | Required. The key looked up in your `ObligationRegistry`. |
| `phase` | `pre` (default `post`). See below. |
| `params` | Optional mapping passed verbatim to the handler. |

**`phase` decides when the handler runs and what failure means — this is the fail-closed crux:**

- **`pre`** runs *before* the action and **gates** it. A handler that raises (or an `id` with no registered handler) **denies the action — the tool never runs.** Use for "you may refund, *but only if* a scoped credential was successfully issued."
- **`post`** runs *after* the action. A failure is recorded and audited but cannot un-execute the side effect (best-effort by physics). Use for "refund, *and then* notify finance."

A decision that never executes (`deny`, refused/timed-out approval) runs all its obligations best-effort — e.g. `decision: deny` + `obligations: [alert-security]` for notify-on-deny.

Each obligation emits `obligation.required`, then `obligation.fulfilled` / `obligation.failed` on the audit stream. **An obligation with no registry configured at all fails closed** — if your policy emits obligations, wire the registry.

In **layered** policies, obligations from the *winning* (same-verdict) layers are **unioned**; an overridden layer's obligations are dropped with its verdict.

### Defaults

```yaml
defaults:
  on_missing_shadow: approve_required  # default for irreversible tools w/o a shadow
  on_no_match: deny                    # what happens when no rule matched
```

Default values when the `defaults:` block is omitted:
- `on_missing_shadow: approve_required`
- `on_no_match: deny`

`on_missing_shadow` only fires when **no rule matched AND** `declared.reversible == False AND declared.has_shadow == False`. A tool declared `reversible=True` falls back to `on_no_match` (no special treatment) even if it lacks a shadow.

---

## Tier 2 — Reusable Predicates

Compose patterns into named predicates and reference them by name.

```yaml
predicates:
  destructive_db:
    tool: sql_exec
    args.body.matches: '(?i)\b(drop|truncate|delete)\b'

  high_blast:
    declared.blast_radius_hint.gt: 100

  in_prod:
    context.environment: prod

rules:
  - id: prod-destructive-needs-approval
    match:
      all_of: [destructive_db, in_prod]      # reference by name
    decision: approve_required
    approvers: ["dba-oncall"]
```

Predicates are pure inlinable booleans. The compiler expands references at load time; there is no recursion or runtime indirection.

**Resolution surface**: a predicate name is resolved as a predicate only when it appears in a *compositional position* — at the top of `match:`, or inside `all_of` / `any_of` / `not`. A predicate name used as the RHS of a leaf (e.g. `match: { args.x: my_predicate }`) is treated as a literal string and almost certainly never matches. Unknown predicate names in compositional positions raise `PolicyCompileError` with a typo suggestion.

---

## Tier 3 — Programmatic Rules (Python escape hatch)

For predicates YAML can't easily express — path extraction, structural pattern matching, decimal math.

**Note:** Python rules are passed explicitly to `compile_policy()` — no module-level `@policy.rule` registration. This keeps the kernel stateless.

```python
# policy_rules.py
from lynx import deny, ActionRequest, ExecutionContext, Decision

def block_paths_outside_workspace(
    req: ActionRequest, ctx: ExecutionContext
) -> Decision | None:
    if req.tool != "shell":
        return None  # this rule does not apply
    for path in extract_paths_from_cmd(req.args.get("cmd", "")):
        absolute = resolve(path, base=ctx.workspace)
        if not absolute.startswith(ctx.workspace):
            return deny(reason=f"Path {absolute} escapes workspace {ctx.workspace}")
    return None
```

```python
# wire it up
from lynx import compile_policy, load_policy_file
from .policy_rules import block_paths_outside_workspace

bundle = load_policy_file(
    "policy.yaml",
    python_rules=(block_paths_outside_workspace,),
)
# or with explicit priorities (compile_policy only):
bundle = compile_policy(
    yaml_source,
    python_rules=(block_paths_outside_workspace,),
    python_rule_priorities=(("block_paths_outside_workspace", 100),),
)
```

**Constraints on Python rules:**

- Must be a pure function of `(ActionRequest, ExecutionContext) -> Decision | None`.
- Returning `None` means "this rule does not apply; continue evaluation."
- Default priority is `0`. Override via `python_rule_priorities=` on `compile_policy`. `load_policy_file` does NOT accept priorities — use `compile_policy` if you need them.
- The priority key matches `fn.__name__`. Lambdas / wrapped functools that don't carry the expected name will silently take the default.
- The kernel does NOT sandbox Python rules — operators are trusted.

**Interleaved evaluation order**: Python and YAML rules are walked in a *single* priority-sorted list. A YAML rule at priority 100 beats a Python rule at priority 80, and vice versa. Within an equal priority, YAML rules sort by file order; Python rules sort by the order they appeared in the `python_rules=` tuple, after YAML.

---

## Evaluation Semantics

For each `ActionRequest`, the PDP runs:

```
1. Walk bundle.eval_order in (priority desc, file order) order.
   (Python rules and YAML rules are interleaved by priority.)
2. For a YAML rule: if rule.match(request, context) is True,
   return rule.decision.
3. For a Python rule: call fn(request, context). If non-None Decision returned,
   that's the verdict.
4. If a rule raises during evaluation:
     - Record `"<rule_error:{rule_id}:{ExceptionName}>"` in matched_rules.
     - Continue evaluation with the next rule.
5. If no rule matched:
     - If declared.reversible == False and declared.has_shadow == False:
         return defaults.on_missing_shadow.
     - Else:
         return defaults.on_no_match.
6. Any accumulated rule-error markers are prepended to the final Decision's
   matched_rules tuple.
```

**First match wins.** No accumulation, no scoring. This makes policies predictable and debuggable.

**Equal-priority tie-break**: integer file index. Rule at index 2 beats rule at index 10 if both share a priority.

To "review then enforce", layer rules from specific to general:

```yaml
rules:
  - id: allow-curl-localhost
    priority: 100
    match: { tool: shell, args.cmd.matches: "^curl http://localhost" }
    decision: allow

  - id: deny-curl
    priority: 50
    match: { tool: shell, args.cmd.matches: "^curl " }
    decision: deny
```

### Rule errors

When a matcher (or Python rule) raises during evaluation, the rule is *skipped* (treated as "did not match") and the error is recorded. The final Decision's `matched_rules` tuple includes one entry per error:

```
matched_rules = ("<rule_error:prod-aws-mutations:TypeError>", "<default:on_no_match>")
```

Sinks see this via the `policy.evaluated` audit event:

```json
{"kind": "policy.evaluated",
 "body": {"verdict": "deny",
          "matched_rules": ["<rule_error:foo:TypeError>", "<default:on_no_match>"]}}
```

This means a buggy rule **never silently fails-open**: the operator can see that something went wrong while the action is being denied (or allowed) by the next valid rule (or the default).

### Decision dataclass

The `Decision` you can return from a Python rule has this shape:

```python
@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    reason: str = ""
    matched_rules: tuple[str, ...] = ()
    approvers: tuple[str, ...] = ()
    transform_args: Mapping[str, Any] | None = None
    timeout_seconds: int | None = None
    obligations: tuple[Obligation, ...] = ()   # mandatory side-actions (see above)
```

Use the helpers in `lynx.policy` (`allow`, `deny`, `dry_run`, `approve_required`, `transform`) for ergonomics — they set sensible defaults, and each takes an optional `obligations=` argument.

---

## PolicyBundle (compiled form)

A YAML file (and optional Python rules) compile into a frozen `PolicyBundle`:

```python
@dataclass(frozen=True, slots=True)
class PolicyBundle:
    id: str                            # first 16 hex chars of sha256
    version: int
    rules: tuple[CompiledRule, ...]
    python_rules: tuple[tuple[str, int, PythonRule], ...]
    defaults: PolicyDefaults
    source_files: tuple[str, ...]
    eval_order: tuple[_EvalStep, ...]  # interleaved python+yaml steps
```

`bundle.id` is a content hash over: the policy `version`, the `defaults`, the canonical form of each rule's body (match + decision + transform + reason + approvers + timeout_seconds), and the `(name, priority)` of each Python rule. **Two policies that differ only in rule bodies — even if the IDs and order are identical — produce different bundle IDs.** Pin `bundle_id` in attestation tooling and CI to detect quiet policy drift.

Every `AuditEvent` carries the `bundle_id` so downstream attestation can prove which policy was in effect.

---

## Hot-swap

The bundle is an immutable value. Pass a different `PolicyBundle` on the next `run_agent` call and the next run uses it — no restart, no invalidation, no cache. Mid-run swap is not supported (and would not be safe: `evaluate` is called inside the run loop, so the bundle must be stable for the run's duration). Recompile on every change; compilation is sub-millisecond for typical policies.

---

## Approval flow contract

When `evaluate` returns `Verdict.APPROVE_REQUIRED`, the mediator calls your `on_approval` handler:

```python
class ApprovalHandler(Protocol):
    async def __call__(self, req: ApprovalRequest) -> ApprovalDecision: ...
```

`lynx.approvals` ships four implementations:

| Helper | Behavior |
|---|---|
| `auto_approve("name")` | Always grant. Useful in tests / lower envs. |
| `auto_deny("reason")` | Always deny. Safe default — used by `run_agent` when no handler is passed. |
| `cli_prompt_approval()` | Prompt on stdin. Runs the blocking read in a worker thread so the event loop keeps spinning. |
| `callback_approval(fn)` | Wrap any async callable. |

Mediator semantics:

- The handler runs **inside** the run loop. The kernel awaits it with `asyncio.wait_for(handler, decision.timeout_seconds)`.
- If the handler exceeds the timeout → the action is denied with `error="denied: approval handler timed out after Ns"`.
- If the handler raises → denied with `error="denied: approval handler raised <Class>: ..."`.
- If the handler returns `ApprovalDecision(granted=False, ...)` → denied.
- If granted → the action runs as if the verdict had been `ALLOW`.

`approvers` is a tuple of opaque strings. The kernel does not interpret them — your handler can use any format (`"user:hadi"`, `"sre-oncall@acme.com"`).

---

## CLI

```bash
lynx init [--dir <path>] [--force]    # write a starter policy.yaml
lynx policy lint [policy.yaml]        # compile-check + rule summary
lynx policy bundle-id [policy.yaml]   # print the content-addressed id
lynx run <script>                     # run an async main() coroutine
```

`lint` compile-checks the file and prints rule summaries. It does NOT yet perform semantic checks (unreachable-rule detection, shadowed-rule warnings).

---

## ReDoS guard

`args.cmd.matches: '(a+)+b'` is **rejected** at compile time — patterns matching `(x+)+`, `(\w+)+`, `(.+)+`, `(a|a)+` shapes are catastrophic-backtracking risks. Patterns longer than 1000 characters are also rejected.

This is a first-pass guard, not a full ReDoS analyzer. If you write something subtler, `lynx policy lint` catches the worst forms but not all of them.

---

## Error model

| Where | What raises | Class |
|---|---|---|
| YAML parse | Malformed YAML | `PolicyCompileError` (wraps `yaml.YAMLError`) |
| Compile | Unknown operator suffix (typo) | `PolicyCompileError` with suggestion |
| Compile | Unknown predicate name in `all_of` / `any_of` / `not` / top-level `match:` | `PolicyCompileError` with suggestion |
| Compile | Unknown verdict | `PolicyCompileError` with the valid list |
| Compile | `decision: transform` without a `transform:` block (or vice versa) | `PolicyCompileError` |
| Compile | `between` / `not_between` / `in` with wrong RHS shape | `PolicyCompileError` |
| Compile | ReDoS-guard rejection / regex too long | `PolicyCompileError` |
| Evaluate | Matcher raises (e.g. type mismatch) | Caught; rule skipped; recorded in `matched_rules` |
| Evaluate | Python rule raises | Caught; rule skipped; recorded in `matched_rules` |

`PolicyCompileError` is a `ValueError` subclass, so existing `except ValueError:` blocks still work. Catch the specific class to give operators a friendlier error.

---

## Determinism

Because the PDP is a pure function over immutable inputs:

- Same `(bundle, request, context)` → same `Decision`, always.
- No clock reads. No network calls. No randomness.
- Tests can use property-based testing (Hypothesis) on the PDP directly.

Matching against `context.timestamp` will technically work but breaks determinism — avoid in production policies. If you need time-of-day rules, populate `context.extra` from the runtime layer above the kernel and match against that.

---

## Layered policy scopes

Real authorization is rarely one flat file. A platform team sets an org-wide floor; a squad layers its own rules on top; an individual may have a personal sandbox config. Lynx composes these without hard-coding *who overrides whom* — that precedence is a business decision, so the kernel ships the **mechanism** and you supply the **policy**.

Pass a list of `PolicyLayer` to `compile_policy` instead of a single source. Each layer is compiled and evaluated **independently**; the per-layer decisions are handed to a developer-chosen `Combiner`, which returns the final `Decision`.

```python
from lynx import PolicyLayer, compile_policy, last_layer_wins

bundle = compile_policy(
    [
        PolicyLayer("org", org_yaml),
        PolicyLayer("team", team_yaml),
        PolicyLayer("user", user_yaml, python_rules=(my_rule,)),
    ],
    merge=last_layer_wins,   # optional; defaults to strict_overrides_loose
)
```

### Shipped combiners (none privileged)

| Combiner | Rule | Use when |
|----------|------|----------|
| `strict_overrides_loose` *(default)* | most-restrictive verdict wins (`deny > approve_required > dry_run > transform > allow`) | broader layers set a floor narrower layers can only tighten — **fail-closed** |
| `last_layer_wins` | the last non-abstaining layer decides outright (CSS cascade) | the most-specific layer is authoritative and may re-grant |
| `first_layer_wins` | the first non-abstaining layer decides | the broadest layer is authoritative |

A `Combiner` is just `Callable[[tuple[tuple[str, Decision | None], ...]], Decision]` — write your own to encode any trust model (soft floors, role-weighted votes, quorum). It is only ever called with at least one non-abstaining layer.

### Abstention and defaults

- A layer that **matches no rule abstains** (contributes `None`) — it does not vote. An empty or non-matching layer can never force a verdict.
- **Defaults apply only when every layer abstains.** The combined default is the **strictest** `on_no_match` / `on_missing_shadow` across all layers, so a forgotten layer can never loosen the floor.

### Provenance

Each layer's `matched_rules` are prefixed with the layer name, and rule-error markers are layer-tagged too:

```
matched_rules = ("team:block-http",)
matched_rules = ("user:<rule_error:my_rule:TypeError>", "org:allow-http")
```

The bundle `id` folds in each layer's id, the combiner's name, and the combined defaults, so it is deterministic across processes and changes when the merge strategy changes.

> Pass `python_rules` per layer via `PolicyLayer(..., python_rules=...)`, not to the top-level `compile_policy` call (which would be ambiguous about ownership and is rejected). See `examples/39_layered_policy.py`.

---

## What this language deliberately does NOT do

- **No turing-completeness in YAML.** No loops, no variables, no arbitrary computation.
- **No global state.** Each rule is a pure function of its match.
- **No cross-rule data passing.** First-match-wins is enforced.
- **No mutation of the request.** Only `transform` produces new args, returned from the PDP; the mediator applies them.
- **No template interpolation** (`${...}`) in `transform: append`. Use a Python rule if you need values from the context.
- **No runtime hot reload within a single `run_agent` call.** Build a new bundle and use it on the next run.

These are the "boring" constraints that make policy reasoning tractable.

---

## Common patterns

### Allow-list reads, deny everything else
```yaml
defaults: { on_no_match: deny }
rules:
  - id: reads
    match: { declared.scope.contains_any: ["customer:read", "fs:read"] }
    decision: allow
```

### Hard deny + approval + dry-run + allow ladder
See `examples/policies/devops.yaml`.

### Append-style transform with regex match
See `examples/policies/sql-transform.yaml`.

---

## Mistakes to avoid

- **Typo'd operator becoming a literal field path**: compile-time check rejects close-miss spellings, but exotic typos may slip through. Prefer the explicit `.eq:` form when in doubt.
- **Bare predicate name in leaf position**: `match: { args.x: my_predicate }` is literal-string equality on `args.x`, not a predicate reference. Predicate names only resolve inside `all_of` / `any_of` / `not` or as a top-level bare-string `match:`.
- **Transform `jsonpath` as real JSONPath**: only `$.args.<single_key>` is supported. Nested paths become literal flat keys.
- **Relying on `context.extra` from `run_agent`**: `run_agent` always passes an empty `extra` today. Use a Python rule if you need to inject context.
- **Assuming `defaults.on_missing_shadow` fires for any irreversible-no-shadow tool**: it only fires when *no rule matched*. A rule explicitly matching the tool overrides the default.
