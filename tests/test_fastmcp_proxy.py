"""Live test: Lynx governs a FastMCP-built MCP server.

Reuses `examples/36_fastmcp_governed.py` in `--serve` mode as a real FastMCP
stdio server, connects as an MCP client, and drives every call through the Lynx
proxy core. The decisive assertion is physical: a policy-denied `delete_file`
leaves the real file on disk. Skipped if `mcp` isn't installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from lynx.policy import compile_policy
from lynx.proxy.mcp_proxy import GovernedProxy, build_toolset

_EXAMPLE = Path(__file__).parent.parent / "examples" / "36_fastmcp_governed.py"

POLICY = """
version: 1
defaults: { on_no_match: deny, on_missing_shadow: approve_required }
rules:
  - { id: allow-reads, match: { tool: read_file }, decision: allow }
  - { id: block-deletes, match: { tool: delete_file }, decision: deny, reason: blocked }
"""


async def test_lynx_governs_a_fastmcp_server(tmp_path: Path) -> None:
    secret = tmp_path / "keep.txt"
    secret.write_text("hello-secret")

    params = StdioServerParameters(
        command=sys.executable, args=[str(_EXAMPLE), "--serve", str(tmp_path)]
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            assert {"read_file", "write_file", "delete_file"} <= set(names)

            async def upstream(name: str, args: dict) -> object:
                res = await session.call_tool(name, arguments=dict(args))
                return res.content

            proxy = GovernedProxy(
                policy=compile_policy(POLICY), tools=build_toolset(names, upstream)
            )

            read_res = await proxy.call("read_file", {"name": "keep.txt"})
            assert read_res.verdict == "allow"
            assert read_res.result.ok
            assert "hello-secret" in repr(read_res.result.value)

            del_res = await proxy.call("delete_file", {"name": "keep.txt"})
            assert del_res.verdict == "deny"
            assert not del_res.result.ok

    # Physical proof the denied delete never reached the FastMCP server:
    assert secret.exists()
    assert secret.read_text() == "hello-secret"
