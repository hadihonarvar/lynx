"""Tests for Decision obligations — the XACML/Cedar "allow-and-also-do-X" channel.

Covers the compiler (YAML parse + validation), the PEP enforcement in ``mediate``
(pre-gate fail-closed / post best-effort / fail-closed on unknown id or missing
registry), the two evaluate-side footguns (python rule preservation + layered
union), and the end-to-end ``run_agent`` path with audit events.
"""

from __future__ import annotations

import pytest

from lynx import (
    ActionRequest,
    ExecutionContext,
    FinalAnswer,
    Message,
    Obligation,
    PolicyLayer,
    Principal,
    ToolCall,
    ToolSet,
    Verdict,
    auto_deny,
    callback_sink,
    compile_policy,
    run_agent,
    tool,
)
from lynx.core.mediator import mediate
from lynx.core.policy import (
    PolicyCompileError,
    allow,
    deny,
    evaluate,
    last_layer_wins,
)
from lynx.core.types import now_utc

# --- scaffolding --------------------------------------------------------

CALLS: list[int] = []
FIRED: list[str] = []


@tool(reversible=True, scope=("compute:exec",))
async def rec_tool(x: int) -> str:
    """Records each execution so a gated action can be proven NOT to have run."""
    CALLS.append(x)
    return f"ran {x}"


TOOLS = ToolSet.from_functions(rec_tool)


def _req() -> ActionRequest:
    return ActionRequest(
        tool="rec_tool",
        args={"x": 1},
        declared=TOOLS.get("rec_tool").metadata,
        context=ExecutionContext(
            principal=Principal(kind="user", id="t"),
            environment="dev",
            workspace=".",
            correlation_id="c",
            step_seq=0,
            timestamp=now_utc(),
        ),
    )


async def _ok_handler(ob, req, ctx) -> None:
    FIRED.append(ob.id)


async def _boom_handler(ob, req, ctx) -> None:
    raise RuntimeError("handler down")


def setup_function() -> None:
    CALLS.clear()
    FIRED.clear()


class _ScriptedAgent:
    def __init__(self, *actions):
        self._actions = list(actions)

    async def step(self, conversation: tuple[Message, ...]):
        return self._actions.pop(0)


# --- compiler: YAML parsing + validation --------------------------------


def test_yaml_obligations_compile_long_and_short_form() -> None:
    bundle = compile_policy(
        """
        version: 1
        rules:
          - id: r
            match: { tool: rec_tool }
            decision: allow
            obligations:
              - id: issue-cred
                phase: pre
                params: { seconds: 300 }
              - notify-finance
        """
    )
    decision = evaluate(bundle, _req(), _req().context)
    assert [o.id for o in decision.obligations] == ["issue-cred", "notify-finance"]
    assert decision.obligations[0].phase == "pre"
    assert decision.obligations[0].params == {"seconds": 300}
    # bare-string shorthand → post phase, no params
    assert decision.obligations[1].phase == "post"
    assert decision.obligations[1].params == {}


@pytest.mark.parametrize(
    "block",
    [
        "obligations: not-a-list",
        "obligations:\n      - phase: pre",  # missing id
        "obligations:\n      - { id: x, phase: sideways }",  # bad phase
        "obligations:\n      - { id: x, params: 5 }",  # params not a mapping
    ],
)
def test_yaml_obligations_validation_errors(block: str) -> None:
    with pytest.raises(PolicyCompileError):
        compile_policy(
            f"version: 1\nrules:\n  - id: r\n    match: {{ tool: rec_tool }}\n"
            f"    decision: allow\n    {block}\n"
        )


def test_obligations_change_bundle_id() -> None:
    base = "version: 1\nrules:\n  - id: r\n    match: {tool: rec_tool}\n    decision: allow\n"
    with_ob = base + "    obligations: [notify]\n"
    assert compile_policy(base).id != compile_policy(with_ob).id


# --- mediate: PEP enforcement -------------------------------------------


async def test_pre_obligation_gates_execution() -> None:
    decision = allow(obligations=(Obligation("issue-cred", "pre"),))
    result = await mediate(
        _req(), decision, TOOLS, auto_deny("x"), obligations={"issue-cred": _boom_handler}
    )
    assert result.ok is False
    assert "pre-obligation 'issue-cred' unfulfilled" in (result.error or "")
    assert CALLS == []  # tool MUST NOT have run
    assert result.obligations[0].fulfilled is False


async def test_post_obligation_is_best_effort() -> None:
    decision = allow(obligations=(Obligation("notify", "post"),))
    result = await mediate(
        _req(), decision, TOOLS, auto_deny("x"), obligations={"notify": _ok_handler}
    )
    assert result.ok is True
    assert CALLS == [1]  # tool ran
    assert FIRED == ["notify"]
    assert result.obligations[0].fulfilled is True


async def test_post_obligation_failure_does_not_undo_action() -> None:
    decision = allow(obligations=(Obligation("notify", "post"),))
    result = await mediate(
        _req(), decision, TOOLS, auto_deny("x"), obligations={"notify": _boom_handler}
    )
    assert result.ok is True  # the side effect happened; we cannot un-execute it
    assert CALLS == [1]
    assert result.obligations[0].fulfilled is False
    assert "handler down" in (result.obligations[0].error or "")


async def test_unknown_obligation_id_fails_closed() -> None:
    decision = allow(obligations=(Obligation("ghost", "pre"),))
    result = await mediate(
        _req(), decision, TOOLS, auto_deny("x"), obligations={"notify": _ok_handler}
    )
    assert result.ok is False
    assert CALLS == []


async def test_obligation_without_registry_fails_closed() -> None:
    decision = allow(obligations=(Obligation("notify", "pre"),))
    result = await mediate(_req(), decision, TOOLS, auto_deny("x"))  # no obligations=
    assert result.ok is False
    assert CALLS == []


async def test_deny_runs_obligations_best_effort() -> None:
    decision = deny("nope", obligations=(Obligation("notify", "post"),))
    result = await mediate(
        _req(), decision, TOOLS, auto_deny("x"), obligations={"notify": _ok_handler}
    )
    assert result.ok is False
    assert CALLS == []  # deny never executes
    assert FIRED == ["notify"]  # but notify-on-deny still fires
    assert result.obligations[0].fulfilled is True


async def test_no_obligations_is_unchanged() -> None:
    result = await mediate(_req(), allow(), TOOLS, auto_deny("x"))
    assert result.ok is True
    assert result.value == "ran 1"
    assert result.obligations == ()


# --- evaluate-side footguns ---------------------------------------------


def test_python_rule_preserves_obligations() -> None:
    def rule(req, ctx):
        return allow(obligations=(Obligation("notify", "post"),))

    bundle = compile_policy("version: 1\nrules: []", python_rules=(rule,))
    decision = evaluate(bundle, _req(), _req().context)
    assert [o.id for o in decision.obligations] == ["notify"]
    assert "rule" in decision.matched_rules  # rule name still tagged


def test_layered_unions_obligations_across_winning_layers() -> None:
    org = "version: 1\nrules:\n  - id: a\n    match: {tool: rec_tool}\n    decision: allow\n    obligations: [org-log]\n"
    team = "version: 1\nrules:\n  - id: b\n    match: {tool: rec_tool}\n    decision: allow\n    obligations: [team-notify]\n"
    bundle = compile_policy([PolicyLayer("org", org), PolicyLayer("team", team)])
    decision = evaluate(bundle, _req(), _req().context)
    assert decision.verdict == Verdict.ALLOW
    # both layers' allow won → both obligations survive (union, not first-wins)
    assert {o.id for o in decision.obligations} == {"org-log", "team-notify"}


def test_layered_losing_verdict_obligations_are_dropped() -> None:
    # org denies (with obligation), team allows (with obligation). strict default:
    # DENY wins; only the deny's obligations survive — the overridden allow's don't.
    org = "version: 1\nrules:\n  - id: a\n    match: {tool: rec_tool}\n    decision: deny\n    obligations: [alert-security]\n"
    team = "version: 1\nrules:\n  - id: b\n    match: {tool: rec_tool}\n    decision: allow\n    obligations: [team-notify]\n"
    bundle = compile_policy([PolicyLayer("org", org), PolicyLayer("team", team)])
    decision = evaluate(bundle, _req(), _req().context)
    assert decision.verdict == Verdict.DENY
    assert {o.id for o in decision.obligations} == {"alert-security"}


def test_last_layer_wins_passes_obligations_through() -> None:
    org = "version: 1\nrules:\n  - id: a\n    match: {tool: rec_tool}\n    decision: deny\n"
    user = "version: 1\nrules:\n  - id: b\n    match: {tool: rec_tool}\n    decision: allow\n    obligations: [user-ack]\n"
    bundle = compile_policy(
        [PolicyLayer("org", org), PolicyLayer("user", user)], merge=last_layer_wins
    )
    decision = evaluate(bundle, _req(), _req().context)
    assert decision.verdict == Verdict.ALLOW
    assert [o.id for o in decision.obligations] == ["user-ack"]


# --- end-to-end via run_agent (incl. audit events) ----------------------


async def test_run_agent_emits_obligation_events_and_fulfills() -> None:
    events = []

    async def _cap(e):
        events.append((e.kind, e.body))

    policy = compile_policy(
        "version: 1\ndefaults: {on_no_match: deny}\n"
        "rules:\n  - id: allow-rec\n    match: {tool: rec_tool}\n    decision: allow\n"
        "    obligations: [notify]\n"
    )
    agent = _ScriptedAgent(
        ToolCall(tool="rec_tool", args={"x": 7}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    result = await run_agent(
        agent,
        task="go",
        tools=TOOLS,
        policy=policy,
        sinks=(callback_sink(_cap),),
        on_approval=auto_deny("x"),
        obligations={"notify": _ok_handler},
    )
    kinds = [k for k, _ in events]
    assert result.final_answer == "done"
    assert CALLS == [7]
    assert "obligation.required" in kinds
    assert "obligation.fulfilled" in kinds
    assert FIRED == ["notify"]


async def test_run_agent_pre_obligation_failure_denies_action() -> None:
    events = []

    async def _cap(e):
        events.append(e.kind)

    policy = compile_policy(
        "version: 1\ndefaults: {on_no_match: deny}\n"
        "rules:\n  - id: allow-rec\n    match: {tool: rec_tool}\n    decision: allow\n"
        "    obligations:\n      - {id: issue-cred, phase: pre}\n"
    )
    agent = _ScriptedAgent(
        ToolCall(tool="rec_tool", args={"x": 9}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    await run_agent(
        agent,
        task="go",
        tools=TOOLS,
        policy=policy,
        sinks=(callback_sink(_cap),),
        on_approval=auto_deny("x"),
        obligations={"issue-cred": _boom_handler},
    )
    assert CALLS == []  # the gated tool never executed
    assert "obligation.failed" in events
