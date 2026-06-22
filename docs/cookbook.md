# Cookbook — policy patterns

Copy-pasteable recipes for the YAML policy.

## Block `rm -rf /`

```yaml
- id: block-rm-rf-root
  priority: 100
  match:
    tool: shell
    args.cmd.matches: '^\s*rm\s+(-[rRf]+\s+)+/(\s|$)'
  decision: deny
  reason: "rm -rf / is hard-blocked"
```

## Allow reads, require approval for writes

```yaml
rules:
  - id: read-only-allow
    match:
      declared.scope.contains_any: [filesystem:read, db:read]
    decision: allow

  - id: writes-need-approval
    match:
      declared.scope.contains_any: [filesystem:write, db:write]
    decision: approve_required
    approvers: ["sre-oncall"]
```

## Production stricter than dev

```yaml
predicates:
  in_prod: { context.environment: prod }
  destructive:
    args.cmd.matches: '(?i)\b(delete|drop|truncate|rm)\b'

rules:
  - id: prod-destructive-deny
    priority: 100
    match:
      all_of: [in_prod, destructive]
    decision: deny
    reason: "destructive ops in prod via the bot are forbidden"

  - id: dev-destructive-allow
    match: destructive
    decision: allow
```

## Dry-run every irreversible action

```yaml
- id: irreversible-dry-run
  match: { declared.reversible: false }
  decision: dry_run
```

## Spend cap on refunds — three tiers

```yaml
predicates:
  is_refund: { tool: refund_customer }

rules:
  - id: over-500-blocked
    priority: 100
    match:
      all_of:
        - is_refund
        - { args.amount_usd.gt: 500 }
    decision: deny
    reason: "amounts over $500 require Finance"

  - id: medium-refund-approval
    priority: 50
    match:
      all_of:
        - is_refund
        - { args.amount_usd.gt: 50 }
    decision: approve_required
    approvers: ["supervisor"]

  - id: small-refund-allow
    match:
      all_of:
        - is_refund
        - { args.amount_usd.le: 50 }
    decision: allow
```

## Fraud watchlist

```yaml
predicates:
  watchlist_customer:
    tool: refund_customer
    args.customer_id.in: ["C-789", "C-1023"]

rules:
  - id: watchlist-block
    priority: 100
    match: watchlist_customer
    decision: deny
    reason: "fraud watchlist — finance handles manually"
```

## Auto-inject tenant_id into SQL

The YAML `transform` block can only insert literal strings — there is no
`${...}` interpolation. For a fixed tenant per environment, just hard-code
the literal in YAML. For a dynamic tenant from `context.principal.id`,
write a Python rule (see the next recipe).

```yaml
predicates:
  destructive_sql:
    tool: sql_exec
    args.sql.matches: '(?i)\b(UPDATE|DELETE)\b'

rules:
  - id: bulk-mutation-deny
    priority: 100
    match:
      tool: sql_exec
      args.sql.matches: '(?i)^\s*(UPDATE|DELETE)\b(?!.*\bWHERE\b)'
    decision: deny
    reason: "UPDATE/DELETE without WHERE would touch every tenant"

  - id: tenant-scope-injection
    match: destructive_sql
    decision: transform
    transform:
      jsonpath: "$.args.sql"
      append: " AND tenant_id = 'TENANT-ALICE'"   # hard-coded literal
```

### Dynamic tenant from the principal (Python rule)

```python
from lynx import transform, ActionRequest, ExecutionContext

def inject_tenant(req: ActionRequest, ctx: ExecutionContext):
    if req.tool != "sql_exec":
        return None
    sql = req.args.get("sql", "")
    if not sql or "tenant_id" in sql:
        return None
    new_args = dict(req.args)
    new_args["sql"] = f"{sql} AND tenant_id = '{ctx.principal.id}'"
    return transform(transform_args=new_args, reason="scoped to principal")

bundle = compile_policy(yaml_source, python_rules=(inject_tenant,))
```

## Path containment (Python escape hatch)

```python
# policy_rules.py
import os
from lynx import deny, ActionRequest, ExecutionContext

def block_paths_outside_workspace(req: ActionRequest, ctx: ExecutionContext):
    if req.tool not in ("shell", "write_file", "delete_file"):
        return None
    workspace = os.path.abspath(ctx.workspace)
    for path in extract_paths(req.args):
        if not os.path.abspath(path).startswith(workspace):
            return deny(f"path {path} escapes workspace {workspace}")
    return None
```

```python
# Wire it up
from lynx.policy import load_policy_file
from .policy_rules import block_paths_outside_workspace

bundle = load_policy_file(
    "policy.yaml",
    python_rules=(block_paths_outside_workspace,),
)
```

## Default-deny with explicit allow-list

```yaml
defaults:
  on_no_match: deny

rules:
  - id: allow-ls
    match: { tool: shell, args.cmd.matches: '^ls\s' }
    decision: allow

  - id: allow-cat
    match: { tool: shell, args.cmd.matches: '^cat\s' }
    decision: allow
```

## Net-egress tools require subprocess sandbox

(Sandbox + scope are tool-author concerns; the policy can require them.)

```yaml
- id: net-egress-must-sandbox
  match:
    declared.scope.contains: net:egress
    declared.has_shadow: false
  decision: deny
  reason: "net-egress tools must have a shadow"
```

## Gate uncertain retries (durable runs)

When a durable run crashes between executing an action and journaling its
result, the resume re-proposes that action with
`context.extra.uncertain_retry: true` — it *may* have already executed.
Decide per risk tier: idempotent tools re-run, money movement goes to a
human.

```yaml
rules:
  - id: never-rerun-uncertain-irreversibles
    match:
      context.extra.uncertain_retry: true
      declared.reversible: false
    decision: approve_required
    reason: "action may have already executed in a crashed attempt"

  - id: idempotent-retries-are-fine
    match:
      context.extra.uncertain_retry: true
      declared.reversible: true
    decision: allow
```

## Layer org / team / user policies

Compose independent policies instead of merging them into one file. Each layer
is evaluated on its own; a developer-chosen `Combiner` resolves disagreements.
The default `strict_overrides_loose` is fail-closed — a broad layer sets a floor
narrower layers can only tighten.

```python
from lynx import compile_policy, PolicyLayer

org = """
rules:
  - id: org-no-prod-deletes
    priority: 100
    match: { context.environment: prod, args.cmd.matches: '(?i)\\b(rm|drop|delete)\\b' }
    decision: deny
    reason: "org policy: no destructive ops in prod"
"""

team = """
rules:
  - id: team-allow-shell-reads
    match: { tool: shell, args.cmd.matches: '^(ls|cat|grep)\\s' }
    decision: allow
"""

# Default Combiner = strict_overrides_loose (fail-closed). The org DENY wins
# over any team ALLOW for the same action; a layer that matches no rule abstains.
bundle = compile_policy([
    PolicyLayer("org", org),
    PolicyLayer("team", team),
])
# Matched rules are layer-tagged in the audit, e.g. "org:org-no-prod-deletes".
```

Swap the merge strategy when a more-specific layer should be authoritative:

```python
from lynx import last_layer_wins   # most-specific layer may re-grant

bundle = compile_policy(
    [PolicyLayer("org", org), PolicyLayer("user", user)],
    merge=last_layer_wins,
)
```
