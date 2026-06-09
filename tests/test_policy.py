"""Unit tests for the policy compiler and PDP. Pure, fast, no I/O."""

from __future__ import annotations

from datetime import UTC, datetime

from gazelle.core.policy import compile_policy, evaluate
from gazelle.core.types import (
    ActionRequest,
    ExecutionContext,
    Principal,
    ToolMetadata,
    Verdict,
)


def _ctx(env: str = "dev") -> ExecutionContext:
    return ExecutionContext(
        principal=Principal(kind="user", id="tester"),
        environment=env,
        workspace="/tmp",
        run_id="R-test",
        step_seq=0,
        timestamp=datetime.now(UTC),
    )


def _req(
    tool: str = "shell",
    args: dict | None = None,
    reversible: bool = True,
    scope: tuple = ("filesystem:write",),
    has_shadow: bool = False,
    env: str = "dev",
) -> ActionRequest:
    return ActionRequest.build(
        tool=tool,
        args=args or {},
        declared=ToolMetadata(
            cost="low",
            reversible=reversible,
            scope=scope,
            has_shadow=has_shadow,
        ),
        context=_ctx(env=env),
    )


def test_simple_allow_deny() -> None:
    bundle = compile_policy(
        """
version: 1
defaults:
  on_no_match: allow
rules:
  - id: block-rm-root
    match:
      tool: shell
      args.cmd.matches: '^rm -rf /$'
    decision: deny
    reason: rm -rf / is forbidden
        """
    )
    deny_decision = evaluate(bundle, _req(args={"cmd": "rm -rf /"}), _ctx())
    assert deny_decision.verdict == Verdict.DENY
    assert "forbidden" in deny_decision.reason

    allow_decision = evaluate(bundle, _req(args={"cmd": "ls"}), _ctx())
    assert allow_decision.verdict == Verdict.ALLOW


def test_first_match_wins() -> None:
    bundle = compile_policy(
        """
version: 1
defaults:
  on_no_match: deny
rules:
  - id: specific-allow
    priority: 10
    match:
      tool: shell
      args.cmd.matches: '^curl http://localhost'
    decision: allow
  - id: general-deny
    priority: 5
    match:
      tool: shell
      args.cmd.matches: '^curl '
    decision: deny
        """
    )
    d = evaluate(bundle, _req(args={"cmd": "curl http://localhost/healthz"}), _ctx())
    assert d.verdict == Verdict.ALLOW
    assert d.matched_rules == ("specific-allow",)

    d2 = evaluate(bundle, _req(args={"cmd": "curl https://example.com"}), _ctx())
    assert d2.verdict == Verdict.DENY


def test_irreversible_without_shadow_defaults_to_approve() -> None:
    bundle = compile_policy(
        """
version: 1
defaults:
  on_no_match: allow
  on_missing_shadow: approve_required
rules: []
        """
    )
    d = evaluate(bundle, _req(reversible=False, has_shadow=False), _ctx())
    assert d.verdict == Verdict.APPROVE_REQUIRED


def test_predicates_compose() -> None:
    bundle = compile_policy(
        """
version: 1
defaults:
  on_no_match: allow
predicates:
  in_prod: { context.environment: prod }
  shelly:  { tool: shell }
rules:
  - id: prod-shell-deny
    match:
      all_of: [in_prod, shelly]
    decision: deny
    reason: no shell in prod
        """
    )
    d = evaluate(bundle, _req(env="prod", args={"cmd": "ls"}), _ctx(env="prod"))
    assert d.verdict == Verdict.DENY

    d2 = evaluate(bundle, _req(env="dev", args={"cmd": "ls"}), _ctx(env="dev"))
    assert d2.verdict == Verdict.ALLOW


def test_default_deny_no_match() -> None:
    bundle = compile_policy(
        """
version: 1
defaults:
  on_no_match: deny
rules: []
        """
    )
    d = evaluate(bundle, _req(args={"cmd": "anything"}), _ctx())
    assert d.verdict == Verdict.DENY


def test_scope_contains_any() -> None:
    bundle = compile_policy(
        """
version: 1
defaults:
  on_no_match: deny
rules:
  - id: read-only-ok
    match:
      declared.scope.contains_any: ["filesystem:read", "net:read"]
    decision: allow
        """
    )
    d = evaluate(bundle, _req(scope=("filesystem:read",), reversible=True), _ctx())
    assert d.verdict == Verdict.ALLOW

    d2 = evaluate(bundle, _req(scope=("filesystem:write",), reversible=True), _ctx())
    assert d2.verdict == Verdict.DENY
