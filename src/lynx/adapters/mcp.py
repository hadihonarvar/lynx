"""MCP (Model Context Protocol) adapter.

Connects to an MCP server, discovers its tools, and returns them as a
``ToolSet`` you can pass directly to ``run_agent`` (or union with your own
tools). No global registration — the returned ToolSet is an immutable value.

The MCP server runs as a child process for the lifetime of the session;
``mcp_tools`` returns an async context manager so the session stays open while
the ToolSet is in use. Closing the context manager terminates the server::

    from lynx import run_agent
    from lynx.adapters.mcp import mcp_tools

    async with mcp_tools("python -m my_mcp_server") as mcp:
        await run_agent(agent, "...", tools=mcp, policy=...)

Pass a list to bypass shell-style splitting::

    async with mcp_tools(["python", "-m", "my_mcp_server", "--flag"]) as mcp:
        ...

Requires ``pip install lynx-agent[mcp]`` (or ``pip install mcp``).
"""

from __future__ import annotations

import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import Any

from lynx.core.types import ToolDef, ToolMetadata, ToolSet

__all__ = ["mcp_tools"]


@asynccontextmanager
async def mcp_tools(
    command: str | list[str],
    *,
    default_cost: str = "medium",
    default_reversible: bool = False,
    default_scope: tuple[str, ...] = ("mcp:tool",),
) -> AsyncIterator[ToolSet]:
    """Async context manager that yields a ``ToolSet`` of discovered MCP tools.

    The MCP session and its stdio child process stay alive for the lifetime
    of the ``async with`` block. Exiting the block tears them down.

    The default safety posture is conservative: ``reversible=False`` with a
    ``mcp:tool`` scope, so operator policies must explicitly allow MCP tools
    before they can run.
    """
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError as exc:
        raise ImportError(
            "mcp_tools requires the 'mcp' package. Install with: pip install mcp"
        ) from exc

    if isinstance(command, list):
        if not command:
            raise ValueError("mcp_tools: command list cannot be empty")
        params = StdioServerParameters(command=command[0], args=command[1:])
    else:
        parts = shlex.split(command)
        if not parts:
            raise ValueError("mcp_tools: command string cannot be empty")
        params = StdioServerParameters(command=parts[0], args=parts[1:])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()

            defs: dict[str, ToolDef] = {}
            for t in listed.tools:
                name = t.name
                description = t.description or f"MCP tool: {name}"

                def make_invoke(
                    bound_name: str,
                ) -> Any:
                    async def _invoke(**kwargs: Any) -> Any:
                        result = await session.call_tool(bound_name, arguments=kwargs)
                        return result.content

                    _invoke.__qualname__ = f"mcp_tool[{bound_name}]"
                    return _invoke

                defs[name] = ToolDef(
                    name=name,
                    description=description,
                    fn=make_invoke(name),
                    shadow_fn=None,
                    metadata=ToolMetadata(
                        cost=default_cost,  # type: ignore[arg-type]
                        reversible=default_reversible,
                        scope=default_scope,
                        has_shadow=False,
                    ),
                )

            yield ToolSet(tools=MappingProxyType(defs))
