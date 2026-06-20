"""Tests for the Lynx MCP proxy governance core.

These exercise the policy path (`govern_call` / `GovernedProxy`) with a fake
upstream caller — no real MCP server or the `mcp` package required. The MCP
transport (`serve_mcp_proxy`) is a thin wiring layer over this core.
"""

from __future__ import annotations

from lynx.core.types import AuditEvent
from lynx.policy import compile_policy
from lynx.proxy.mcp_proxy import (
    GovernedProxy,
    ToolClassifier,
    build_toolset,
    default_classify,
)

POLICY = """
version: 1
defaults:
  on_no_match: deny
  on_missing_shadow: approve_required
rules:
  - id: allow-read
    priority: 10
    match: { tool: read_file }
    decision: allow
  - id: dryrun-write
    priority: 10
    match: { tool: write_file }
    decision: dry_run
  - id: deny-delete
    priority: 10
    match: { tool: delete_file }
    decision: deny
    reason: deletes are blocked via the proxy
"""


def _make_proxy(calls: list[tuple[str, dict]], sinks=()):
    async def upstream(name: str, args):
        calls.append((name, dict(args)))
        return f"ran {name}"

    tools = build_toolset(
        ["read_file", "write_file", "delete_file"], upstream, default_classify
    )
    return GovernedProxy(
        policy=compile_policy(POLICY), tools=tools, sinks=sinks
    )


async def test_allow_forwards_to_upstream():
    calls: list[tuple[str, dict]] = []
    proxy = _make_proxy(calls)

    res = await proxy.call("read_file", {"path": "/etc/hosts"})

    assert res.verdict == "allow"
    assert res.result.ok is True
    assert res.result.value == "ran read_file"
    assert calls == [("read_file", {"path": "/etc/hosts"})]


async def test_deny_never_reaches_upstream():
    calls: list[tuple[str, dict]] = []
    proxy = _make_proxy(calls)

    res = await proxy.call("delete_file", {"path": "/important"})

    assert res.verdict == "deny"
    assert res.result.ok is False
    assert "blocked" in (res.result.error or "")
    assert calls == []  # the side effect never happened


async def test_dry_run_uses_shadow_not_upstream():
    calls: list[tuple[str, dict]] = []
    proxy = _make_proxy(calls)

    res = await proxy.call("write_file", {"path": "/tmp/x", "content": "hi"})

    assert res.verdict == "dry_run"
    assert res.result.ok is True
    assert res.result.value["dry_run"] is True
    assert res.result.value["preview"]["would_call"] == "write_file"
    assert calls == []  # dry_run never touches upstream


async def test_unknown_tool_is_denied():
    calls: list[tuple[str, dict]] = []
    proxy = _make_proxy(calls)

    res = await proxy.call("rm_rf", {})

    assert res.verdict == "deny"
    assert res.result.ok is False
    assert "unknown tool" in (res.result.error or "")
    assert calls == []


async def test_no_match_defaults_to_deny():
    # A tool the policy says nothing about falls through to on_no_match: deny.
    calls: list[tuple[str, dict]] = []

    async def upstream(name, args):
        calls.append((name, dict(args)))
        return "x"

    tools = build_toolset(["mystery"], upstream)
    proxy = GovernedProxy(policy=compile_policy(POLICY), tools=tools)

    res = await proxy.call("mystery", {})

    assert res.verdict == "deny"
    assert calls == []


async def test_audit_events_are_emitted():
    events: list[AuditEvent] = []

    async def sink(e: AuditEvent) -> None:
        events.append(e)

    calls: list[tuple[str, dict]] = []
    proxy = _make_proxy(calls, sinks=(sink,))

    await proxy.call("read_file", {"path": "/x"})

    kinds = [e.kind for e in events]
    assert "policy.evaluated" in kinds
    assert "action.started" in kinds
    assert "action.completed" in kinds
    # seq is monotonic and the bundle id is stamped on every event
    assert [e.seq for e in events] == sorted(e.seq for e in events)
    assert all(e.bundle_id == proxy.policy.id for e in events)


async def test_outcome_event_kinds_match_the_kernel_vocabulary():
    # The proxy must emit the SAME outcome kinds as core/scheduler.py: a denied
    # action is action.denied (not action.failed), a dry_run completes as
    # action.dry_run_completed (not action.completed).
    events: list[AuditEvent] = []

    async def sink(e: AuditEvent) -> None:
        events.append(e)

    calls: list[tuple[str, dict]] = []
    proxy = _make_proxy(calls, sinks=(sink,))

    await proxy.call("delete_file", {"path": "/x"})  # deny
    await proxy.call("write_file", {"path": "/y", "content": "z"})  # dry_run

    kinds = {e.kind for e in events}
    assert "action.denied" in kinds
    assert "action.dry_run_completed" in kinds
    assert "action.failed" not in kinds  # a policy refusal is not a crash


async def test_classifier_overrides_and_per_tool_scope():
    clf = ToolClassifier(reversible=True, scope=("mcp:tool", "fs"))
    meta = clf("read_file")
    assert meta.reversible is True
    assert "mcp:tool" in meta.scope
    assert "mcp:read_file" in meta.scope  # per-tool scope tag always added
