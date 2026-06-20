"""
================================================================
EXAMPLE 34 — "Govern any MCP server with the Lynx proxy" (INTEGRATIONS)
================================================================

SCENARIO:
    The MCP *adapter* (example 20) pulls a server's tools INTO a Lynx
    `run_agent` loop. The MCP *proxy* (`lynx.proxy.mcp_proxy`) is the
    reverse and far more viral: it sits IN FRONT OF an existing MCP
    server so any MCP client — Claude Desktop, Claude Code, Cursor —
    points at Lynx instead of the real server and gets policy + audit +
    approvals + dry-run for FREE. Zero code change on either side.

        MCP client ──▶  Lynx proxy  ──▶  upstream MCP server
                          │ evaluate → mediate
                          └─▶ audit sinks

WHAT THIS EXAMPLE SHOWS:
    - The transport-free governance core (`GovernedProxy` / `govern_call`)
      that every proxied `call_tool` flows through — identical verdicts to
      `run_agent`: allow / deny / dry_run / approve_required / transform.
    - Wiring it to a real stdio MCP server with `serve_mcp_proxy(...)`
      (shown at the bottom; commented out — it needs the `mcp` package
      plus a server to talk to).

    To stay runnable with no external server, the demo drives the core
    against a FAKE upstream caller that just records what it was asked to
    run — so you can see deny/dry_run blocking the side effect.

REQUIRES:
    Nothing for the core demo. The live proxy needs:
      pip install lynx-agent[mcp]

RUN WITH:
    python examples/34_mcp_proxy.py
"""

from __future__ import annotations

import asyncio

from lynx import AuditEvent, compile_policy
from lynx.proxy.mcp_proxy import GovernedProxy, build_toolset

# A policy an operator drops in front of an MCP filesystem server: reads are
# free, writes are previewed (dry_run), deletes are hard-blocked. Everything
# else falls through to `on_no_match: deny`.
POLICY = """
version: 1
defaults:
  on_no_match: deny
  on_missing_shadow: approve_required
rules:
  - id: allow-reads
    priority: 10
    match: { tool: read_file }
    decision: allow
  - id: preview-writes
    priority: 10
    match: { tool: write_file }
    decision: dry_run
  - id: block-deletes
    priority: 10
    match: { tool: delete_file }
    decision: deny
    reason: "deletes are blocked by the proxy policy"
"""


async def main() -> None:
    # What the real MCP server *would* have executed. The proxy only reaches
    # upstream through this; if it stays empty for a call, the side effect was
    # blocked before it ever happened.
    executed: list[str] = []

    async def fake_upstream(name: str, args: dict) -> str:
        executed.append(name)
        return f"upstream ran {name}({args})"

    # A console audit sink so you can watch every decision stream by. The proxy
    # emits the SAME event vocabulary as run_agent — note action.denied (a policy
    # refusal) is distinct from action.failed (a crash), and dry-runs complete as
    # action.dry_run_completed.
    outcomes = {
        "policy.evaluated",
        "action.completed",
        "action.dry_run_completed",
        "action.denied",
        "action.failed",
    }

    async def print_sink(e: AuditEvent) -> None:
        if e.kind in outcomes:
            verdict = e.body.get("verdict", "")
            extra = e.body.get("reason") or e.body.get("duration_ms", "")
            print(f"  [audit] {e.kind:24} {e.body.get('tool',''):12} {verdict} {extra}")

    tools = build_toolset(["read_file", "write_file", "delete_file"], fake_upstream)
    proxy = GovernedProxy(
        policy=compile_policy(POLICY), tools=tools, sinks=(print_sink,)
    )

    for tool, args in [
        ("read_file", {"path": "/etc/hosts"}),
        ("write_file", {"path": "/tmp/notes", "content": "hi"}),
        ("delete_file", {"path": "/important"}),
        ("chmod", {"path": "/etc", "mode": "777"}),  # unknown → default deny
    ]:
        print(f"\n→ client calls {tool}({args})")
        governed = await proxy.call(tool, args)
        ok = "OK" if governed.result.ok else "BLOCKED"
        print(f"  verdict={governed.verdict}  result={ok}")

    print(f"\nUpstream actually executed: {executed or '(nothing — all gated)'}")
    print("read_file ran; write was dry-run; delete + unknown never reached upstream.")


# ---------------------------------------------------------------------------
# The real thing — wire the proxy to a live stdio MCP server. Uncomment and
# run with the `mcp` extra installed. Point your MCP client at THIS process.
# ---------------------------------------------------------------------------
#
# from lynx.proxy.mcp_proxy import serve_mcp_proxy
# from lynx.sinks import jsonl_sink
#
# async def serve() -> None:
#     with open("mcp-audit.jsonl", "a") as f:
#         await serve_mcp_proxy(
#             "npx -y @modelcontextprotocol/server-filesystem .",
#             policy=compile_policy(POLICY),
#             sinks=(jsonl_sink(f),),
#             environment="prod",
#         )


if __name__ == "__main__":
    asyncio.run(main())
