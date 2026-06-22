"""
================================================================
EXAMPLE 39 — "Org, team, and user policies that compose" (POLICY)
================================================================

SCENARIO:
    In a real company, authorization is not one flat file. The platform team
    sets an org-wide floor ("never touch prod without approval"). A team layers
    on its own rules ("our squad may not call the billing API at all"). An
    individual developer has a personal sandbox config. These must COMPOSE — and
    the composition rule (who can override whom) is a *business* decision, not
    something the kernel should hard-code.

    `compile_policy([...PolicyLayer...])` evaluates each named layer
    independently and hands the per-layer decisions to a developer-chosen
    `Combiner`. Lynx ships three batteries — none privileged:

        strict_overrides_loose  most-restrictive verdict wins (the default,
                                fail-closed). Broader layers set a floor;
                                narrower layers can only tighten.
        last_layer_wins         most-specific layer is authoritative (CSS
                                cascade): the last matching layer can re-grant.
        first_layer_wins        broadest layer is authoritative.

    A layer that matches no rule ABSTAINS — it does not vote. Defaults apply
    only when EVERY layer abstains, and the combined default is the strictest
    across layers, so a forgotten layer can never loosen the floor.

WHAT THIS EXAMPLE SHOWS:
    - Compiling org / team / user layers into one LayeredPolicyBundle.
    - strict_overrides_loose: team's deny beats org's allow; user can't re-grant.
    - last_layer_wins: same layers, but now the user layer CAN re-grant.
    - Layer-tagged provenance in `matched_rules` (who decided, and where).
    - A custom Combiner — the whole point: you own the trust model.

REQUIRES:
    pip install lynx-agent        # stdlib only — no extra deps

RUN WITH:
    python examples/39_layered_policy.py
"""

from __future__ import annotations

from datetime import UTC, datetime

from lynx import (
    PolicyLayer,
    compile_policy,
    first_layer_wins,
    last_layer_wins,
    strict_overrides_loose,
)
from lynx.core.types import ActionRequest, ExecutionContext, Principal, ToolMetadata
from lynx.policy import evaluate

# --- The three layers, each an independent policy ---------------------------

ORG = """
version: 1
defaults: {on_no_match: allow}
rules:
  - {id: allow-http, match: {tool: http}, decision: allow}
"""

TEAM = """
version: 1
defaults: {on_no_match: allow}
rules:
  - {id: block-http, match: {tool: http}, decision: deny, reason: team blocks http}
"""

USER_REGRANT = """
version: 1
defaults: {on_no_match: allow}
rules:
  - {id: allow-http, match: {tool: http}, decision: allow, reason: my sandbox}
"""


def _req(tool: str = "http") -> ActionRequest:
    return ActionRequest(
        tool=tool,
        args={},
        declared=ToolMetadata(cost="low", reversible=True, scope=(), has_shadow=True),
        context=ExecutionContext(
            principal=Principal(kind="user", id="dev-1"),
            environment="dev",
            workspace=".",
            correlation_id="run-39",
            step_seq=0,
            timestamp=datetime(2026, 6, 21, tzinfo=UTC),
        ),
    )


def _show(title: str, decision: object) -> None:
    d = decision  # Decision
    print(f"{title:<28} -> {d.verdict.value:<8} {list(d.matched_rules)}")  # type: ignore[attr-defined]


def main() -> None:
    layers = [PolicyLayer("org", ORG), PolicyLayer("team", TEAM), PolicyLayer("user", USER_REGRANT)]

    # 1. Default combiner: strict_overrides_loose. The team's deny is a floor the
    #    user cannot lift — even though the user layer explicitly allows http.
    strict = compile_policy(layers)  # strict_overrides_loose is the default
    _show("strict_overrides_loose", evaluate(strict, _req(), _req().context))

    # 2. Same layers, last_layer_wins: the most-specific (user) layer re-grants.
    cascade = compile_policy(layers, merge=last_layer_wins)
    _show("last_layer_wins", evaluate(cascade, _req(), _req().context))

    # 3. first_layer_wins: the broadest (org) layer is authoritative.
    broadest = compile_policy(layers, merge=first_layer_wins)
    _show("first_layer_wins", evaluate(broadest, _req(), _req().context))

    # 4. A custom Combiner — you own the trust model. Here: any single deny among
    #    the layers downgrades to dry_run instead of a hard block (a "soft floor").
    def deny_becomes_dry_run(decided: tuple[tuple[str, object], ...]) -> object:
        from lynx.core.types import Decision, Verdict

        votes = [(n, d) for n, d in decided if d is not None]
        if any(d.verdict is Verdict.DENY for _, d in votes):  # type: ignore[attr-defined]
            tags = tuple(f"{n}:soft-floor" for n, _ in votes)
            return Decision(verdict=Verdict.DRY_RUN, reason="deny softened to dry_run", matched_rules=tags)
        return strict_overrides_loose(decided)  # type: ignore[arg-type]

    custom = compile_policy(layers, merge=deny_becomes_dry_run)  # type: ignore[arg-type]
    _show("custom (soft floor)", evaluate(custom, _req(), _req().context))

    print(
        "\nKey idea: Lynx evaluates each layer; YOU choose how disagreements resolve.\n"
        "The default is fail-closed (strictest wins); everything else is opt-in."
    )


if __name__ == "__main__":
    main()
