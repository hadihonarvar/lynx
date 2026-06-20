"""Minimal upstream MCP server for the live proxy integration test.

Exposes read_file / write_file / delete_file scoped to a workdir given as
argv[1]. These do REAL filesystem side effects, so the test can prove that a
policy-denied delete never reached here (the file is still on disk).

Not a pytest module (name doesn't match test_*) — it's launched as a child
process by tests/test_mcp_proxy_live.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

_NAME_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}
_WRITE_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
    "required": ["name", "content"],
}


def main() -> None:
    workdir = Path(sys.argv[1])
    server = Server("upstream-fs")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return [
            types.Tool(name="read_file", description="read a file", inputSchema=_NAME_SCHEMA),
            types.Tool(name="write_file", description="write a file", inputSchema=_WRITE_SCHEMA),
            types.Tool(name="delete_file", description="delete a file", inputSchema=_NAME_SCHEMA),
        ]

    @server.call_tool()
    async def _call(name: str, arguments: dict | None) -> list[types.TextContent]:
        args = arguments or {}
        target = workdir / str(args.get("name", ""))
        if name == "read_file":
            text = target.read_text()
        elif name == "write_file":
            target.write_text(str(args.get("content", "")))
            text = f"wrote {target.name}"
        elif name == "delete_file":
            target.unlink()
            text = f"deleted {target.name}"
        else:
            text = f"unknown tool {name}"
        return [types.TextContent(type="text", text=text)]

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
