"""LangGraph adapter.

Wraps a compiled LangGraph state graph as an Lynx ``Agent``. The graph's
``ToolNode`` calls surface to Lynx as ``ToolCall``s; the kernel mediates
them through policy, executes the tool, and feeds the result back into
the conversation the graph sees on its next step.

Requires ``pip install lynx-agent[langgraph]``.

Usage::

    from langgraph.graph import StateGraph
    from lynx import ToolSet, run_agent, compile_policy
    from lynx.adapters.langgraph_adapter import LangGraphAgent

    graph = StateGraph(...)
    agent = LangGraphAgent(compiled_graph=graph.compile())
    await run_agent(
        agent, "...", tools=ToolSet.from_functions(...), policy=compile_policy(...)
    )
"""

from __future__ import annotations

from typing import Any

from lynx.core.types import FinalAnswer, Message, ToolCall

__all__ = ["LangGraphAgent"]


class LangGraphAgent:
    """Adapter for compiled LangGraph state graphs.

    Stateless across ``step()`` calls: each step rebuilds the graph input
    from the immutable conversation it receives. When the graph emits a
    message with ``tool_calls``, the first call is surfaced to Lynx and the
    kernel mediates it; subsequent calls are picked up by the next step
    after the tool_result is appended to the conversation.

    If the graph emits multiple parallel ``tool_calls`` in one update, only
    the first is forwarded — the others are intentionally dropped because
    the Lynx kernel mediates one call per step. The next ``step()`` will see
    only the first call's result; the graph is responsible for re-deciding.
    """

    def __init__(self, compiled_graph: Any) -> None:
        try:
            import langgraph  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "LangGraphAgent requires the 'langgraph' package. "
                "Install with: pip install langgraph"
            ) from exc
        self._graph = compiled_graph

    async def step(self, conversation: tuple[Message, ...]) -> ToolCall | FinalAnswer:
        state: dict[str, Any] = {"messages": _to_langchain_messages(conversation)}
        seen_messages: list[Any] = list(state["messages"])

        async for event in self._graph.astream(state, stream_mode="updates"):
            for _node, update in event.items():
                if isinstance(update, dict) and "messages" in update:
                    seen_messages.extend(update["messages"])
                    for msg in update["messages"]:
                        tool_calls = getattr(msg, "tool_calls", None)
                        if tool_calls:
                            tc = tool_calls[0]
                            name = tc.get("name") or tc.get("function", {}).get("name", "")
                            raw_args = (
                                tc.get("args") or tc.get("function", {}).get("arguments", {}) or {}
                            )
                            args = (
                                raw_args
                                if isinstance(raw_args, dict)
                                else {"_raw_arguments": str(raw_args)}
                            )
                            return ToolCall(
                                tool=name,
                                args=args,
                                call_id=tc.get("id", ""),
                            )
        for msg in reversed(seen_messages):
            content = getattr(msg, "content", "")
            if content:
                return FinalAnswer(text=str(content))
        return FinalAnswer(text="(no response)")


def _to_langchain_messages(conv: tuple[Message, ...]) -> list[dict[str, Any]]:
    """Best-effort: LangChain has many message classes; plain dicts work."""
    return [{"role": m.role, "content": m.content} for m in conv]
