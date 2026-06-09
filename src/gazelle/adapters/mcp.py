"""MCP (Model Context Protocol) universal adapter.

Auto-discovers tools from an MCP server and registers them as Gazelle
@tool functions. Once registered, any agent can call them through the
normal Gazelle mediator + policy stack.

Requires `pip install gazelle[mcp]` (or `pip install mcp`).

Usage::

    from gazelle.adapters.mcp import register_mcp_server
    from gazelle import runtime

    # Connect to an MCP server (stdio, SSE, or HTTP) and register its tools.
    await register_mcp_server("python -m my_mcp_server")

    # Now `my_tool_from_mcp` is callable like any other @tool.
"""

from __future__ import annotations

from gazelle.core.mediator import RegisteredTool, get_registry
from gazelle.core.types import ToolMetadata


async def register_mcp_server(
    command: str | list[str],
    *,
    default_cost: str = "medium",
    default_reversible: bool = False,
    default_scope: tuple[str, ...] = ("mcp:tool",),
) -> list[str]:
    """Discover tools from an MCP server and register them with Gazelle.

    Returns the list of tool names registered. Each MCP tool's input schema
    is reflected into the JSON schema returned to upstream agents.

    The default safety posture is conservative: tools default to
    `reversible=False` and a `mcp:tool` scope, so policies must explicitly
    allow them before they can run.
    """
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError as exc:
        raise ImportError(
            "register_mcp_server requires the 'mcp' package. Install with: pip install mcp"
        ) from exc

    params = (
        StdioServerParameters(command=command[0], args=command[1:])
        if isinstance(command, list)
        else StdioServerParameters(command=command)
    )

    registered_names: list[str] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            for tool in tools.tools:
                name = tool.name
                description = tool.description or f"MCP tool: {name}"
                # tool.inputSchema is forwarded to upstream agents via
                # _signature_to_json_schema fallback; not needed inline here.

                async def _invoke(_session=session, _name=name, **kwargs):
                    result = await _session.call_tool(_name, arguments=kwargs)
                    return result.content

                def _meta_factory(_args, _scope=default_scope):
                    return ToolMetadata(
                        cost=default_cost,
                        reversible=default_reversible,
                        scope=_scope,
                        has_shadow=False,
                    )

                get_registry().register(
                    RegisteredTool(
                        name=name,
                        description=description,
                        fn=_invoke,
                        shadow_fn=None,
                        metadata_factory=_meta_factory,
                    )
                )
                registered_names.append(name)

    return registered_names


__all__ = ["register_mcp_server"]
