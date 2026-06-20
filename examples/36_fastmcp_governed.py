"""
================================================================
EXAMPLE 36 — "Build it with FastMCP, govern it with Lynx" (INTEGRATIONS)
================================================================

SCENARIO:
    FastMCP (the high-level decorator API bundled in the official `mcp` SDK,
    `mcp.server.fastmcp`) is the popular, ergonomic way to BUILD an MCP server:
    write a function, slap `@mcp.tool()` on it, done. Lynx is the layer that
    GOVERNS what those tools may do. Put them together and you get a tool
    server an agent can call — under policy, with an audit trail, and with
    irreversible calls blocked — without writing any wiring in the server.

        @mcp.tool() def delete_file(...)        ← built with FastMCP
                 │  (real MCP, stdio)
                 ▼
        Lynx proxy: evaluate → mediate          ← governed by Lynx
                 │  allow / dry_run / deny
                 ▼
        the actual side effect (or not)

WHAT THIS EXAMPLE SHOWS:
    - Defining an MCP server with FastMCP's `@mcp.tool()` decorators.
    - Governing it with the Lynx proxy core (`build_toolset` + `GovernedProxy`):
      reads allowed, writes previewed (dry_run), deletes denied — and the
      denied delete never reaches the real filesystem.
    - The one-liner you'd use in production (`serve_mcp_proxy`, commented).

    This single file is dual-mode:
      *  `--serve <dir>` runs as the FastMCP stdio server.
      *  no args runs the governed demo, launching the server as a child.

REQUIRES:
    pip install lynx-agent[mcp]   (FastMCP ships inside the `mcp` package)

RUN WITH:
    python examples/36_fastmcp_governed.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from lynx import AuditEvent, compile_policy
from lynx.proxy.mcp_proxy import GovernedProxy, build_toolset


def build_server(workdir: Path):  # type: ignore[no-untyped-def]
    """A tiny MCP server built the FastMCP way — three filesystem tools."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("lynx-fastmcp-demo")

    @mcp.tool()
    def read_file(name: str) -> str:
        """Read a file from the workspace."""
        return (workdir / name).read_text()

    @mcp.tool()
    def write_file(name: str, content: str) -> str:
        """Write a file in the workspace."""
        (workdir / name).write_text(content)
        return f"wrote {name}"

    @mcp.tool()
    def delete_file(name: str) -> str:
        """Delete a file from the workspace."""
        (workdir / name).unlink()
        return f"deleted {name}"

    return mcp


# Operator policy dropped in front of the FastMCP server: reads free, writes
# previewed, deletes blocked. Anything else falls through to on_no_match: deny.
POLICY = """
version: 1
defaults:
  on_no_match: deny
  on_missing_shadow: approve_required
rules:
  - id: allow-reads
    match: { tool: read_file }
    decision: allow
  - id: preview-writes
    match: { tool: write_file }
    decision: dry_run
  - id: block-deletes
    match: { tool: delete_file }
    decision: deny
    reason: "deletes are blocked by the proxy policy"
"""


async def demo() -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    workdir = Path(tempfile.mkdtemp(prefix="lynx-fastmcp-"))
    secret = workdir / "secret.txt"
    secret.write_text("hello-secret")

    async def print_sink(e: AuditEvent) -> None:
        if e.kind in {"policy.evaluated", "action.completed", "action.dry_run_completed", "action.denied"}:
            print(f"  [audit] {e.kind:24} {e.body.get('tool', ''):12} {e.body.get('verdict', '')}")

    # Launch this same file in --serve mode as the upstream FastMCP server.
    params = StdioServerParameters(
        command=sys.executable, args=[__file__, "--serve", str(workdir)]
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = [t.name for t in listed.tools]
            print(f"FastMCP server exposed: {names}\n")

            async def upstream(name: str, args: dict) -> object:
                res = await session.call_tool(name, arguments=dict(args))
                return res.content

            proxy = GovernedProxy(
                policy=compile_policy(POLICY),
                tools=build_toolset(names, upstream),
                sinks=(print_sink,),
            )

            for tool, args in [
                ("read_file", {"name": "secret.txt"}),
                ("write_file", {"name": "secret.txt", "content": "OVERWRITTEN"}),
                ("delete_file", {"name": "secret.txt"}),
            ]:
                print(f"→ {tool}({args})")
                gov = await proxy.call(tool, args)
                print(f"  verdict={gov.verdict}  ok={gov.result.ok}")

    survived = secret.exists() and secret.read_text() == "hello-secret"
    print(f"\nsecret.txt intact on disk: {survived}")
    print("read allowed; write was previewed; delete denied — none mutated the file.")


# ---------------------------------------------------------------------------
# In production you wouldn't hand-drive the core — you'd point an MCP client
# (Claude Desktop/Code, Cursor) at the Lynx proxy and let it front the server:
#
#   from lynx.proxy.mcp_proxy import serve_mcp_proxy
#   await serve_mcp_proxy(
#       [sys.executable, __file__, "--serve", "/data"],
#       policy=compile_policy(POLICY),
#       sinks=(jsonl_sink(open("mcp-audit.jsonl", "a")),),
#   )
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--serve":
        build_server(Path(sys.argv[2])).run()  # FastMCP stdio server; blocks
    else:
        asyncio.run(demo())
