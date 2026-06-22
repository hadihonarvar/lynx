"""Layered policy scopes — independent layers + a developer-supplied combiner.

Lynx provides the mechanism (evaluate each layer, hand results to a Combiner);
the developer owns the trust model. These tests cover the shipped combiners,
abstention, defaults, provenance, determinism, and single-bundle regression.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lynx import (
    LayeredPolicyBundle,
    PolicyLayer,
    compile_policy,
    first_layer_wins,
    last_layer_wins,
    strict_overrides_loose,
)
from lynx.core.types import ActionRequest, ExecutionContext, Principal, ToolMetadata
from lynx.policy import PolicyBundle, PolicyCompileError, evaluate

ORG = """
version: 1
defaults: {on_no_match: allow}
rules:
  - {id: allow-http, match: {tool: http}, decision: allow}
"""
TEAM_DENY = """
version: 1
defaults: {on_no_match: allow}
rules:
  - {id: block-http, match: {tool: http}, decision: deny, reason: team blocks http}
"""
EMPTY = """
version: 1
defaults: {on_no_match: allow}
rules: []
"""


def _layers(*specs: tuple[str, str]) -> list[PolicyLayer]:
    return [PolicyLayer(name, src) for name, src in specs]


def _req(tool: str = "http", *, reversible: bool = True, has_shadow: bool = True) -> ActionRequest:
    return ActionRequest(
        tool=tool,
        args={},
        declared=ToolMetadata(
            cost="low", reversible=reversible, scope=(), has_shadow=has_shadow
        ),
        context=ExecutionContext(
            principal=Principal(kind="user", id="t"),
            environment="dev",
            workspace=".",
            correlation_id="c",
            step_seq=0,
            timestamp=datetime(2026, 6, 21, tzinfo=UTC),
        ),
    )


def _ctx() -> ExecutionContext:
    return _req().context


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def test_list_of_layers_compiles_to_layered_bundle() -> None:
    b = compile_policy(_layers(("org", ORG), ("team", TEAM_DENY)))
    assert isinstance(b, LayeredPolicyBundle)
    assert [name for name, _ in b.layers] == ["org", "team"]


def test_single_source_still_compiles_to_plain_bundle() -> None:
    b = compile_policy(ORG)
    assert isinstance(b, PolicyBundle)


def test_empty_layer_list_is_rejected() -> None:
    with pytest.raises(PolicyCompileError, match="at least one PolicyLayer"):
        compile_policy([])


def test_duplicate_layer_names_rejected() -> None:
    with pytest.raises(PolicyCompileError, match="duplicate layer name"):
        compile_policy(_layers(("org", ORG), ("org", EMPTY)))


def test_merge_only_valid_for_layers() -> None:
    with pytest.raises(PolicyCompileError, match="merge= is only valid"):
        compile_policy(ORG, merge=strict_overrides_loose)


def test_bundle_id_is_deterministic() -> None:
    a = compile_policy(_layers(("org", ORG), ("team", TEAM_DENY)))
    b = compile_policy(_layers(("org", ORG), ("team", TEAM_DENY)))
    assert a.id == b.id


def test_bundle_id_changes_with_merge_strategy() -> None:
    a = compile_policy(_layers(("org", ORG)), merge=strict_overrides_loose)
    b = compile_policy(_layers(("org", ORG)), merge=last_layer_wins)
    assert a.id != b.id


# ---------------------------------------------------------------------------
# strict_overrides_loose (default) — most restrictive wins
# ---------------------------------------------------------------------------


def test_strict_team_deny_overrides_org_allow() -> None:
    b = compile_policy(_layers(("org", ORG), ("team", TEAM_DENY), ("user", EMPTY)))
    d = evaluate(b, _req("http"), _ctx())
    assert d.verdict.value == "deny"
    assert d.matched_rules == ("team:block-http",)
    assert d.reason == "team blocks http"


def test_strict_user_cannot_regrant_team_deny() -> None:
    USER_ALLOW = """
version: 1
defaults: {on_no_match: allow}
rules:
  - {id: allow-http, match: {tool: http}, decision: allow}
"""
    b = compile_policy(_layers(("org", ORG), ("team", TEAM_DENY), ("user", USER_ALLOW)))
    d = evaluate(b, _req("http"), _ctx())
    assert d.verdict.value == "deny"  # strict floor holds; user cannot loosen


# ---------------------------------------------------------------------------
# Abstention + defaults
# ---------------------------------------------------------------------------


def test_empty_layer_abstains_does_not_force_default_deny() -> None:
    # team is empty with on_no_match: deny — but it must abstain, not veto.
    TEAM_EMPTY_DENY = "version: 1\ndefaults: {on_no_match: deny}\nrules: []\n"
    b = compile_policy(_layers(("org", ORG), ("team", TEAM_EMPTY_DENY)))
    d = evaluate(b, _req("http"), _ctx())
    assert d.verdict.value == "allow"  # only org matched; team abstained


def test_all_layers_abstain_falls_through_to_combined_default() -> None:
    # No layer has a rule matching 'ftp'; org/team default on_no_match: allow,
    # but one layer is stricter (deny) -> combined default is the strictest (deny).
    TEAM_DENY_DEFAULT = "version: 1\ndefaults: {on_no_match: deny}\nrules: []\n"
    b = compile_policy(_layers(("org", ORG), ("team", TEAM_DENY_DEFAULT)))
    d = evaluate(b, _req("ftp"), _ctx())
    assert d.verdict.value == "deny"
    assert d.matched_rules == ("<default:on_no_match>",)


def test_combined_default_is_strictest_on_missing_shadow() -> None:
    LOOSE = "version: 1\ndefaults: {on_missing_shadow: allow, on_no_match: allow}\nrules: []\n"
    STRICT = (
        "version: 1\n"
        "defaults: {on_missing_shadow: approve_required, on_no_match: allow}\nrules: []\n"
    )
    b = compile_policy(_layers(("org", LOOSE), ("team", STRICT)))
    d = evaluate(b, _req("ftp", reversible=False, has_shadow=False), _ctx())
    assert d.verdict.value == "approve_required"
    assert d.matched_rules == ("<default:on_missing_shadow>",)


# ---------------------------------------------------------------------------
# Other shipped combiners
# ---------------------------------------------------------------------------


def test_last_layer_wins_lets_user_regrant() -> None:
    USER_ALLOW = (
        "version: 1\ndefaults: {on_no_match: allow}\n"
        "rules:\n  - {id: allow-http, match: {tool: http}, decision: allow}\n"
    )
    b = compile_policy(
        _layers(("org", ORG), ("team", TEAM_DENY), ("user", USER_ALLOW)),
        merge=last_layer_wins,
    )
    d = evaluate(b, _req("http"), _ctx())
    assert d.verdict.value == "allow"
    assert d.matched_rules == ("user:allow-http",)


def test_last_layer_wins_skips_abstaining_top_layer() -> None:
    # user abstains -> last non-abstaining layer (team) decides.
    b = compile_policy(
        _layers(("org", ORG), ("team", TEAM_DENY), ("user", EMPTY)),
        merge=last_layer_wins,
    )
    d = evaluate(b, _req("http"), _ctx())
    assert d.verdict.value == "deny"
    assert d.matched_rules == ("team:block-http",)


def test_first_layer_wins() -> None:
    b = compile_policy(
        _layers(("org", ORG), ("team", TEAM_DENY)),
        merge=first_layer_wins,
    )
    d = evaluate(b, _req("http"), _ctx())
    assert d.verdict.value == "allow"
    assert d.matched_rules == ("org:allow-http",)


# ---------------------------------------------------------------------------
# Custom combiner — the whole point: developer owns the trust model
# ---------------------------------------------------------------------------


def test_custom_combiner_is_honored() -> None:
    def always_dry_run(layers):
        from lynx.core.types import Decision, Verdict

        names = tuple(f"custom:{n}" for n, d in layers if d is not None)
        return Decision(verdict=Verdict.DRY_RUN, reason="custom", matched_rules=names)

    b = compile_policy(_layers(("org", ORG), ("team", TEAM_DENY)), merge=always_dry_run)
    d = evaluate(b, _req("http"), _ctx())
    assert d.verdict.value == "dry_run"
    assert d.matched_rules == ("custom:org", "custom:team")


# ---------------------------------------------------------------------------
# Provenance: rule errors get layer-tagged
# ---------------------------------------------------------------------------


def test_layer_rule_errors_are_tagged() -> None:
    def boom(req, ctx):
        raise RuntimeError("nope")

    org = compile_policy(EMPTY)  # ensure single still works
    assert isinstance(org, PolicyBundle)

    layer_with_bug = PolicyLayer("team", EMPTY, python_rules=(boom,))
    b = compile_policy([PolicyLayer("org", ORG), layer_with_bug])
    d = evaluate(b, _req("http"), _ctx())
    # org allows http; team's python rule raised -> tagged error in provenance.
    assert any(m.startswith("team:<rule_error:boom") for m in d.matched_rules)
    assert d.verdict.value == "allow"


def test_per_layer_python_rules_via_layer_not_call() -> None:
    def grant(req, ctx):
        return None

    with pytest.raises(PolicyCompileError, match="python_rules per layer"):
        compile_policy([PolicyLayer("org", ORG)], python_rules=(grant,))
