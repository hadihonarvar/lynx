"""Framework-native integrations — the ToolGuard primitive + OpenAI Agents shim.

These cover the framework-agnostic core (all five verdicts enforced through the
real kernel, unknown-tool fail-closed, audit emission, provenance) and the pure
mapping helpers of the SDK shim. The SDK shim's wiring to the real
``openai-agents`` package is exercised only when that optional extra is present.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lynx import ToolSet, shadow, tool
from lynx.approvals import ApprovalDecision
from lynx.core.types import AuditEvent, Principal
from lynx.integrations import GovernedCall, ToolGuard
from lynx.integrations.openai_agents import render_result
from lynx.policy import compile_policy

# --- tools under test -------------------------------------------------------


@tool(reversible=True, scope=("compute:read",))
async def read_doc(path: str) -> str:
    return f"contents of {path}"


@tool(reversible=False, scope=("payments:write",))
async def charge(amount: int) -> str:
    return f"charged {amount}"


@shadow(charge)
async def _charge_shadow(amount: int) -> str:
    return f"WOULD charge {amount}"


TOOLS = ToolSet.from_functions(read_doc, charge)


def _guard(policy_yaml: str, *, sinks: Any = (), on_approval: Any = None) -> ToolGuard:
    return ToolGuard(
        tools=TOOLS,
        policy=compile_policy(policy_yaml),
        principal=Principal(kind="user", id="u1"),
        sinks=sinks,
        on_approval=on_approval,
    )


ALLOW_READS = """
version: 1
defaults: {on_no_match: deny}
rules:
  - {id: allow-read, match: {tool: read_doc}, decision: allow}
"""


# --- ToolGuard: the five verdicts through the real kernel -------------------


async def test_allow_executes_the_real_tool() -> None:
    g = _guard(ALLOW_READS)
    call = await g.check("read_doc", {"path": "/a"})
    assert isinstance(call, GovernedCall)
    assert call.allowed
    assert call.result.value == "contents of /a"
    assert call.decision.verdict.value == "allow"


async def test_no_match_denies_fail_closed() -> None:
    g = _guard(ALLOW_READS)
    call = await g.check("charge", {"amount": 5})
    assert not call.allowed
    assert call.decision.verdict.value == "deny"
    assert "denied" in (call.result.error or "")


async def test_unknown_tool_is_denied_never_executed() -> None:
    g = _guard(ALLOW_READS)
    call = await g.check("rm_rf", {"path": "/"})
    assert not call.allowed
    assert call.decision.verdict.value == "deny"
    assert call.decision.matched_rules == ("<unknown_tool>",)


async def test_dry_run_returns_shadow_preview_no_side_effect() -> None:
    policy = """
version: 1
defaults: {on_no_match: deny}
rules:
  - {id: preview-charge, match: {tool: charge}, decision: dry_run}
"""
    g = _guard(policy)
    call = await g.check("charge", {"amount": 9})
    assert call.allowed
    assert call.result.value == {"dry_run": True, "preview": "WOULD charge 9"}


async def test_transform_rewrites_args_before_execution() -> None:
    policy = """
version: 1
defaults: {on_no_match: deny}
rules:
  - id: cap-charge
    match: {tool: charge}
    decision: transform
    transform: {jsonpath: "$.args.amount", set: 1}
"""
    g = _guard(policy)
    call = await g.check("charge", {"amount": 9999})
    assert call.allowed
    assert call.result.value == "charged 1"  # transformed amount, not 9999


async def test_approve_required_uses_the_guards_handler() -> None:
    policy = """
version: 1
defaults: {on_no_match: deny}
rules:
  - {id: gate-charge, match: {tool: charge}, decision: approve_required}
"""

    async def grant(_req: Any) -> ApprovalDecision:
        return ApprovalDecision(granted=True, approver="ops")

    g = _guard(policy, on_approval=grant)
    call = await g.check("charge", {"amount": 50})
    assert call.allowed
    assert call.result.value == "charged 50"


async def test_approve_required_default_handler_denies() -> None:
    policy = """
version: 1
defaults: {on_no_match: deny}
rules:
  - {id: gate-charge, match: {tool: charge}, decision: approve_required}
"""
    g = _guard(policy)  # no on_approval -> fail-closed auto-deny
    call = await g.check("charge", {"amount": 50})
    assert not call.allowed


# --- audit emission ---------------------------------------------------------


async def test_sinks_receive_proposed_and_outcome_events() -> None:
    events: list[AuditEvent] = []

    async def sink(ev: AuditEvent) -> None:
        events.append(ev)

    g = _guard(ALLOW_READS, sinks=(sink,))
    await g.check("read_doc", {"path": "/a"})
    kinds = [e.kind for e in events]
    assert "step.proposed" in kinds
    assert "policy.evaluated" in kinds
    assert "action.completed" in kinds
    assert all(e.correlation_id == g.correlation_id for e in events)
    # every event shares one (non-empty) bundle id — the policy that decided them
    bundle_ids = {e.bundle_id for e in events}
    assert len(bundle_ids) == 1 and "" not in bundle_ids


async def test_correlation_id_is_stable_across_checks() -> None:
    g = _guard(ALLOW_READS)
    await g.check("read_doc", {"path": "/a"})
    cid1 = g.correlation_id
    await g.check("read_doc", {"path": "/b"})
    assert g.correlation_id == cid1


async def test_unknown_tool_still_emits_policy_evaluated() -> None:
    # The docstring promises every governed call emits policy.evaluated; the
    # fail-closed unknown-tool path must not skip it.
    events: list[AuditEvent] = []

    async def sink(ev: AuditEvent) -> None:
        events.append(ev)

    g = _guard(ALLOW_READS, sinks=(sink,))
    await g.check("rm_rf", {"path": "/"})
    kinds = [e.kind for e in events]
    assert kinds == ["step.proposed", "policy.evaluated", "action.failed"]


async def test_concurrent_checks_label_step_seq_distinctly() -> None:
    # Regression: _build_request must use the seq captured at the top of check(),
    # not the live counter. A sink that yields control opens the interleaving
    # window between _next_seq() and request-building; with the bug both requests
    # picked up the latest counter value and collided on step_seq.
    async def yielding_sink(_ev: AuditEvent) -> None:
        await asyncio.sleep(0)

    g = _guard(ALLOW_READS, sinks=(yielding_sink,))
    calls = await asyncio.gather(
        g.check("read_doc", {"path": "/a"}),
        g.check("read_doc", {"path": "/b"}),
    )
    seqs = sorted(c.request.context.step_seq for c in calls)
    assert seqs == [0, 1]  # distinct, each labeled with its own captured seq


# --- render_result: SDK-facing mapping (no SDK needed) ----------------------


async def test_render_result_passes_through_string_value() -> None:
    g = _guard(ALLOW_READS)
    call = await g.check("read_doc", {"path": "/a"})
    assert render_result(call) == "contents of /a"


async def test_render_result_marks_denials_for_the_model() -> None:
    g = _guard(ALLOW_READS)
    call = await g.check("charge", {"amount": 1})
    rendered = render_result(call)
    assert rendered.startswith("[denied]")


async def test_render_result_empty_string_for_successful_none() -> None:
    @tool(reversible=True, scope=("compute:write",))
    async def ping(x: int) -> None:  # a void tool: succeeds, returns None
        return None

    g = ToolGuard(
        tools=ToolSet.from_functions(ping),
        policy=compile_policy("version: 1\ndefaults: {on_no_match: allow}\nrules: []\n"),
    )
    call = await g.check("ping", {"x": 1})
    assert call.allowed and call.result.value is None
    assert render_result(call) == ""  # not the literal "null"


async def test_render_result_json_encodes_non_string_values() -> None:
    policy = """
version: 1
defaults: {on_no_match: deny}
rules:
  - {id: preview-charge, match: {tool: charge}, decision: dry_run}
"""
    g = _guard(policy)
    call = await g.check("charge", {"amount": 9})
    rendered = render_result(call)
    assert '"dry_run": true' in rendered and '"preview"' in rendered


# --- optional extra: only runs if openai-agents is installed ----------------


async def test_governed_function_tools_requires_the_sdk_or_builds() -> None:
    pytest.importorskip("agents", reason="openai-agents extra not installed")
    from lynx.integrations.openai_agents import governed_function_tools

    built = governed_function_tools(TOOLS, policy=compile_policy(ALLOW_READS))
    assert len(built) == len(TOOLS.names())
