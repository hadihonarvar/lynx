"""Live end-to-end test of the MCP proxy transport.

Three real processes over stdio:

    this test (MCP client)  ──▶  mcp_live_proxy.py (Lynx proxy)  ──▶  mcp_live_upstream.py

The upstream server does REAL filesystem ops, so the decisive assertion is
physical: after a policy-denied `delete_file`, the file is still on disk —
the side effect never happened. Skipped if `mcp` isn't installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

_PROXY = Path(__file__).with_name("mcp_live_proxy.py")


def _texts(result: object) -> str:
    """Concatenate the .text of every content block in a CallToolResult."""
    blocks = getattr(result, "content", []) or []
    return " ".join(getattr(b, "text", "") for b in blocks)


async def test_live_proxy_governs_real_mcp_server(tmp_path: Path) -> None:
    secret = tmp_path / "keep.txt"
    secret.write_text("hello-secret")

    params = StdioServerParameters(command=sys.executable, args=[str(_PROXY), str(tmp_path)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            # 1. Tools are re-exported verbatim through the proxy.
            listed = await client.list_tools()
            names = {t.name for t in listed.tools}
            assert {"read_file", "write_file", "delete_file"} <= names

            # 2. read_file is allowed → upstream content flows back.
            read_res = await client.call_tool("read_file", {"name": "keep.txt"})
            assert "hello-secret" in _texts(read_res)

            # 3. write_file is dry_run → preview, no real write.
            await client.call_tool("write_file", {"name": "keep.txt", "content": "OVERWRITTEN"})

            # 4. delete_file is denied → proxy returns a denial, upstream untouched.
            del_res = await client.call_tool("delete_file", {"name": "keep.txt"})
            assert "lynx denied" in _texts(del_res).lower()

    # Physical proof the gated side effects never reached the filesystem:
    assert secret.exists(), "denied delete must not have removed the file"
    assert secret.read_text() == "hello-secret", "dry-run write must not have changed it"
