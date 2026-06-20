"""Policy engine (PDP) tests — pure function behavior."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from lynx import (
    ActionRequest,
    Decision,
    ExecutionContext,
    Principal,
    ToolMetadata,
    Verdict,
    allow,
    compile_policy,
    deny,
)
from lynx.policy import PolicyCompileError, evaluate


def _ctx(env: str = "dev") -> ExecutionContext:
    return ExecutionContext(
        principal=Principal(kind="user", id="t"),
        environment=env,
        workspace="/tmp",
        correlation_id="c-test",
        step_seq=0,
        timestamp=datetime.now(UTC),
    )


def _req(
    tool: str = "shell",
    args: Mapping[str, object] | None = None,
    *,
    reversible: bool = True,
    has_shadow: bool = False,
    env: str = "dev",
) -> ActionRequest:
    return ActionRequest(
        tool=tool,
        args=args or {},
        declared=ToolMetadata(
            cost="low",
            reversible=reversible,
            scope=("compute:exec",),
            has_shadow=has_shadow,
        ),
        context=_ctx(env=env),
    )


def test_simple_allow_deny() -> None:
    bundle = compile_policy(
        """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: block
    match:
      tool: shell
      args.cmd.matches: '^rm -rf /$'
    decision: deny
    reason: forbidden
        """
    )
    d1 = evaluate(bundle, _req(args={"cmd": "rm -rf /"}), _ctx())
    assert d1.verdict == Verdict.DENY
    assert "forbidden" in d1.reason

    d2 = evaluate(bundle, _req(args={"cmd": "ls"}), _ctx())
    assert d2.verdict == Verdict.ALLOW


def test_first_match_wins() -> None:
    bundle = compile_policy(
        """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: specific-allow
    priority: 10
    match: { tool: shell, args.cmd.matches: '^curl http://localhost' }
    decision: allow
  - id: general-deny
    priority: 5
    match: { tool: shell, args.cmd.matches: '^curl ' }
    decision: deny
        """
    )
    d = evaluate(bundle, _req(args={"cmd": "curl http://localhost/health"}), _ctx())
    assert d.verdict == Verdict.ALLOW


def test_default_on_missing_shadow() -> None:
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


def test_python_rules_explicit_not_global() -> None:
    """Python rules are passed in at compile time, not via a global registry."""

    def block_in_prod(req: ActionRequest, ctx: ExecutionContext) -> Decision | None:
        if ctx.environment == "prod":
            return deny(reason="prod is locked")
        return None

    bundle = compile_policy(
        "version: 1\ndefaults: { on_no_match: allow }\nrules: []",
        python_rules=(block_in_prod,),
        python_rule_priorities=(("block_in_prod", 100),),
    )

    d_prod = evaluate(bundle, _req(env="prod"), _ctx(env="prod"))
    assert d_prod.verdict == Verdict.DENY

    d_dev = evaluate(bundle, _req(env="dev"), _ctx(env="dev"))
    assert d_dev.verdict == Verdict.ALLOW


def test_python_rule_returning_none_falls_through() -> None:
    def maybe_deny(req: ActionRequest, ctx: ExecutionContext) -> Decision | None:
        return None  # never matches

    bundle = compile_policy(
        "version: 1\ndefaults: { on_no_match: allow }\nrules: []",
        python_rules=(maybe_deny,),
    )
    d = evaluate(bundle, _req(), _ctx())
    assert d.verdict == Verdict.ALLOW


def test_pdp_is_deterministic() -> None:
    """Same inputs → same Decision, always."""
    bundle = compile_policy(
        """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: r
    match: { tool: shell }
    decision: deny
    reason: no shells
        """
    )
    req = _req()
    ctx = _ctx()
    d1 = evaluate(bundle, req, ctx)
    d2 = evaluate(bundle, req, ctx)
    d3 = evaluate(bundle, req, ctx)
    assert d1 == d2 == d3


def test_redos_guard_rejects_dangerous_regex() -> None:
    with pytest.raises(PolicyCompileError):
        compile_policy(
            """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: redos
    match:
      tool: shell
      args.cmd.matches: '(a+)+b'
    decision: deny
            """
        )


def test_overlong_regex_rejected() -> None:
    long_pat = "a" * 1500
    with pytest.raises(PolicyCompileError):
        compile_policy(
            f"""
version: 1
defaults: {{ on_no_match: deny }}
rules:
  - id: too-long
    match:
      tool: shell
      args.cmd.matches: "{long_pat}"
    decision: deny
            """
        )


def test_decision_constructors() -> None:
    assert allow().verdict == Verdict.ALLOW
    assert deny("no").verdict == Verdict.DENY
    assert deny("no").reason == "no"


# ---------------------------------------------------------------------------
# Error model / compile-time validation
# ---------------------------------------------------------------------------


def test_malformed_yaml_wraps_in_policy_compile_error() -> None:
    with pytest.raises(PolicyCompileError, match="YAML parse error"):
        compile_policy("this is: not valid: yaml: <<<")


def test_typo_operator_rejected_with_suggestion() -> None:
    with pytest.raises(PolicyCompileError, match="matches"):
        compile_policy(
            """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: typo
    match: { tool: shell, args.cmd.matchess: 'x' }
    decision: deny
            """
        )


def test_unknown_predicate_name_rejected() -> None:
    with pytest.raises(PolicyCompileError, match="Unknown predicate"):
        compile_policy(
            """
version: 1
defaults: { on_no_match: deny }
predicates:
  is_shell: { tool: shell }
rules:
  - id: r
    match: is_shel
    decision: allow
            """
        )


def test_in_operator_rejects_non_list_rhs() -> None:
    with pytest.raises(PolicyCompileError, match="`in` operator"):
        compile_policy(
            """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: r
    match: { tool.in: shell }
    decision: allow
            """
        )


def test_between_operator_validates_shape() -> None:
    with pytest.raises(PolicyCompileError, match="between"):
        compile_policy(
            """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: r
    match: { args.x.between: [10, 5] }
    decision: allow
            """
        )


def test_verdict_accepts_mixed_case() -> None:
    bundle = compile_policy(
        """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: r
    match: { tool: shell }
    decision: Deny
    reason: nope
        """
    )
    d = evaluate(bundle, _req(), _ctx())
    assert d.verdict == Verdict.DENY


def test_transform_without_block_rejected() -> None:
    with pytest.raises(PolicyCompileError, match="transform"):
        compile_policy(
            """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: r
    match: { tool: shell }
    decision: transform
            """
        )


def test_transform_block_without_op_rejected() -> None:
    with pytest.raises(PolicyCompileError, match="at least one of"):
        compile_policy(
            """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: r
    match: { tool: shell }
    decision: transform
    transform:
      jsonpath: "$.args.cmd"
            """
        )


def test_transform_block_on_non_transform_verdict_rejected() -> None:
    with pytest.raises(PolicyCompileError, match="transform"):
        compile_policy(
            """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: r
    match: { tool: shell }
    decision: allow
    transform:
      jsonpath: "$.args.cmd"
      set: "echo"
            """
        )


# ---------------------------------------------------------------------------
# Bundle ID — content-addressed
# ---------------------------------------------------------------------------


def test_bundle_id_changes_when_rule_body_changes() -> None:
    a = compile_policy(
        "version: 1\ndefaults: { on_no_match: deny }\n"
        "rules:\n"
        "  - id: r\n"
        "    match: { tool: shell }\n"
        "    decision: allow\n"
    )
    b = compile_policy(
        "version: 1\ndefaults: { on_no_match: deny }\n"
        "rules:\n"
        "  - id: r\n"  # same id
        "    match: { tool: shell }\n"
        "    decision: deny\n"  # different verdict
        "    reason: no\n"
    )
    assert a.id != b.id  # a naive hash collided here


def test_bundle_id_stable_across_compiles() -> None:
    src = "version: 1\ndefaults: { on_no_match: deny }\nrules: []\n"
    assert compile_policy(src).id == compile_policy(src).id


# ---------------------------------------------------------------------------
# Priority interleave + tie-break
# ---------------------------------------------------------------------------


def test_python_rule_loses_to_higher_priority_yaml_rule() -> None:
    """Python rules interleave with YAML by priority, not walked to exhaustion first."""

    def py_allow(req: ActionRequest, ctx: ExecutionContext) -> Decision | None:
        return allow(reason="python says yes")

    bundle = compile_policy(
        """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: high-yaml-deny
    priority: 100
    match: { tool: shell }
    decision: deny
    reason: yaml wins
        """,
        python_rules=(py_allow,),
        python_rule_priorities=(("py_allow", 50),),
    )
    d = evaluate(bundle, _req(), _ctx())
    assert d.verdict == Verdict.DENY


def test_python_rule_wins_when_higher_priority() -> None:
    def py_deny(req: ActionRequest, ctx: ExecutionContext) -> Decision | None:
        return deny(reason="python wins")

    bundle = compile_policy(
        """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: low-yaml-allow
    priority: 10
    match: { tool: shell }
    decision: allow
        """,
        python_rules=(py_deny,),
        python_rule_priorities=(("py_deny", 100),),
    )
    d = evaluate(bundle, _req(), _ctx())
    assert d.verdict == Verdict.DENY


def test_priority_tiebreak_uses_file_order_above_ten_rules() -> None:
    """Above 10 same-priority rules, lexicographic source_location would put
    rule[10] before rule[2]. The fix uses integer file order."""
    rules_src = "\n".join(
        f"  - id: r{idx}\n    priority: 5\n    match: {{ args.idx.eq: {idx} }}\n    decision: deny\n    reason: idx-{idx}"
        for idx in range(15)
    )
    bundle = compile_policy(
        f"version: 1\ndefaults: {{ on_no_match: allow }}\nrules:\n{rules_src}\n"
    )
    # Rule indices are 0..14; verify the bundle's compiled-rule order has r2
    # before r10 (file order), not lexicographic.
    ordered_ids = [r.id for r in bundle.rules]
    assert ordered_ids.index("r2") < ordered_ids.index("r10")


# ---------------------------------------------------------------------------
# Defaults firing
# ---------------------------------------------------------------------------


def test_default_on_no_match_fires() -> None:
    bundle = compile_policy("version: 1\nrules: []\n")  # default on_no_match=deny
    d = evaluate(bundle, _req(reversible=True, has_shadow=False), _ctx())
    assert d.verdict == Verdict.DENY
    assert "<default:on_no_match>" in d.matched_rules


def test_default_on_missing_shadow_only_fires_for_irreversible_without_shadow() -> None:
    bundle = compile_policy(
        "version: 1\ndefaults: { on_no_match: allow, on_missing_shadow: approve_required }\nrules: []\n"
    )
    # Irreversible + no shadow → on_missing_shadow
    d1 = evaluate(bundle, _req(reversible=False, has_shadow=False), _ctx())
    assert d1.verdict == Verdict.APPROVE_REQUIRED
    # Irreversible + has shadow → falls through to on_no_match
    d2 = evaluate(bundle, _req(reversible=False, has_shadow=True), _ctx())
    assert d2.verdict == Verdict.ALLOW
    # Reversible + no shadow → falls through to on_no_match
    d3 = evaluate(bundle, _req(reversible=True, has_shadow=False), _ctx())
    assert d3.verdict == Verdict.ALLOW


# ---------------------------------------------------------------------------
# Rule errors surface in matched_rules, not silently fail-open
# ---------------------------------------------------------------------------


def test_matcher_exception_is_recorded_not_swallowed() -> None:
    """A matcher that raises must record an error marker and continue."""

    def buggy(req: ActionRequest, ctx: ExecutionContext) -> Decision | None:
        raise RuntimeError("boom")

    bundle = compile_policy(
        "version: 1\ndefaults: { on_no_match: allow }\nrules: []\n",
        python_rules=(buggy,),
        python_rule_priorities=(("buggy", 100),),
    )
    d = evaluate(bundle, _req(), _ctx())
    assert d.verdict == Verdict.ALLOW
    assert any("rule_error" in m for m in d.matched_rules)
    assert any("RuntimeError" in m for m in d.matched_rules)


def test_yaml_matcher_type_error_is_recorded() -> None:
    """A YAML matcher that raises (e.g. comparison type error) must surface
    as a diagnostic, not silently fail-open."""
    bundle = compile_policy(
        """
version: 1
defaults: { on_no_match: allow }
rules:
  - id: cmp
    priority: 100
    match: { args.amount.gt: 5 }
    decision: deny
    reason: too big
        """
    )
    # Compare int>str at runtime — should record an error and fall through.
    d = evaluate(bundle, _req(args={"amount": "not a number"}), _ctx())
    assert d.verdict == Verdict.ALLOW
    assert any("rule_error" in m for m in d.matched_rules)


# ---------------------------------------------------------------------------
# Hot-swap proves no leaked state
# ---------------------------------------------------------------------------


def test_two_bundles_with_same_request_decide_independently() -> None:
    allow_bundle = compile_policy("version: 1\ndefaults: { on_no_match: allow }\nrules: []\n")
    deny_bundle = compile_policy("version: 1\ndefaults: { on_no_match: deny }\nrules: []\n")
    req = _req()
    ctx = _ctx()
    assert evaluate(allow_bundle, req, ctx).verdict == Verdict.ALLOW
    assert evaluate(deny_bundle, req, ctx).verdict == Verdict.DENY
    # And again the other direction — no order-dependent caching.
    assert evaluate(deny_bundle, req, ctx).verdict == Verdict.DENY
    assert evaluate(allow_bundle, req, ctx).verdict == Verdict.ALLOW
