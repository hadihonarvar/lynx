"""OpenAI Agents SDK integration — govern tool calls at the SDK's own hook.

The OpenAI Agents SDK (``pip install openai-agents``) owns the agent loop and
calls plain Python functions decorated with ``@function_tool``. This shim turns
a Lynx :class:`~lynx.core.types.ToolSet` into a list of governed
``function_tool``s: when the SDK invokes one, the call is routed through a
:class:`~lynx.integrations.core.ToolGuard` (``evaluate`` → ``mediate``) and the
Lynx verdict is mapped onto what the SDK expects back from a tool —

    ALLOW / TRANSFORM / approved  → the real tool runs; its value is returned
    DENY / refused / timed out    → a ``[denied] …`` string the model can read
    DRY_RUN                       → the shadow preview (no side effect)
    APPROVE_REQUIRED              → resolved by the guard's ``on_approval`` handler

So the SDK keeps driving the loop; Lynx governs every tool call inside it with
no proxy and no change to the agent's own code. Each governed tool's docstring
and name are preserved so the model sees the same tools it always did.

Requires ``pip install lynx-agent[openai-agents]`` (or ``pip install openai-agents``).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from lynx.integrations.core import GovernedCall, ToolGuard

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lynx.approvals import ApprovalHandler
    from lynx.core.policy import LayeredPolicyBundle, PolicyBundle
    from lynx.core.types import Principal, ToolSet
    from lynx.executors import Executor
    from lynx.sinks import Sink

__all__ = ["governed_function_tools", "render_result"]


def render_result(call: GovernedCall) -> str:
    """Map a :class:`GovernedCall` to the string the SDK tool should return.

    The model reads this text, so denials and previews are made explicit rather
    than swallowed. Successful values are returned as-is when already a string,
    else JSON-encoded (falling back to ``repr`` for non-serializable values).
    """
    result = call.result
    if not result.ok:
        return f"[denied] {result.error or 'policy denied this action'}"
    value = result.value
    if isinstance(value, str):
        return value
    if value is None:
        # A successful void tool — return empty, not the literal JSON "null"
        # (which the model would read as a meaningful string).
        return ""
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)


def governed_function_tools(
    tools: ToolSet,
    *,
    policy: PolicyBundle | LayeredPolicyBundle,
    principal: Principal | None = None,
    environment: str = "dev",
    workspace: str = ".",
    on_approval: ApprovalHandler | None = None,
    executor: Executor | None = None,
    sinks: Sequence[Sink] = (),
    guard: ToolGuard | None = None,
) -> list[Any]:
    """Build governed ``function_tool``s for the OpenAI Agents SDK from a ToolSet.

    Pass the returned list as ``Agent(tools=...)``. Every invocation the SDK
    makes is governed by one shared :class:`ToolGuard` (so the audit stream and
    correlation id are consistent across the agent's tools). Supply your own
    ``guard`` to share it across several agents; otherwise one is built from the
    remaining arguments.

    The SDK is imported lazily, so importing this module never requires the
    optional dependency — only calling this function does.
    """
    try:
        import agents
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "governed_function_tools requires the OpenAI Agents SDK. "
            "Install with: pip install openai-agents"
        ) from exc
    function_tool = agents.function_tool

    if guard is None:
        from lynx.core.types import Principal as _Principal

        guard = ToolGuard(
            tools=tools,
            policy=policy,
            principal=principal or _Principal(kind="user", id="anonymous"),
            environment=environment,
            workspace=workspace,
            on_approval=on_approval,
            executor=executor,
            sinks=sinks,
        )

    built: list[Any] = []
    for name in tools.names():
        built.append(_make_function_tool(function_tool, guard, tools.get(name)))
    return built


def _make_function_tool(function_tool: Any, guard: ToolGuard, tool_def: Any) -> Any:
    """Wrap one Lynx ToolDef as a governed SDK ``function_tool``.

    The wrapper accepts the SDK's tool-call context plus a JSON arguments string
    (the ``function_tool`` "manual"/raw form), so it works uniformly for any tool
    signature without reflecting over parameters. Governance — not the real
    function — is what the SDK sees; ``ToolGuard`` executes the real function
    only when the verdict permits.
    """

    async def _invoke(_ctx: Any, args_json: str) -> str:
        try:
            args = json.loads(args_json) if args_json else {}
            if not isinstance(args, dict):
                args = {"_raw_arguments": args_json}
        except json.JSONDecodeError:
            args = {"_raw_arguments": args_json}
        call = await guard.check(tool_def.name, args)
        return render_result(call)

    return function_tool(
        _invoke,
        name_override=tool_def.name,
        description_override=tool_def.description or "",
    )
