"""LangGraph adapter.

Wraps a compiled LangGraph state graph as an Agent. The graph's ToolNode
calls are intercepted; each tool invocation becomes a gzl ActionRequest
that flows through the policy + audit chain.

Requires `pip install gazelle[langgraph]`.

Usage::

    from langgraph.graph import StateGraph
    from gazelle.adapters.langgraph_adapter import LangGraphAgent
    from gazelle import runtime

    graph = StateGraph(...)
    # ... compile graph ...
    agent = LangGraphAgent(compiled_graph=graph.compile())
    await runtime.run(agent, task="...", policy="policy.yaml")
"""

from __future__ import annotations

from typing import Any

from gazelle.sdk import FinalAnswer, Message, ToolCall


class LangGraphAgent:
    """Adapter for compiled LangGraph state graphs.

    Operates by stepping the graph one node at a time. When the graph hits
    a ToolNode, we extract the ToolCall and surrender control to Gazelle's
    mediator; the result is fed back into the graph state on resume.
    """

    def __init__(self, compiled_graph: Any) -> None:
        try:
            import langgraph  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "LangGraphAgent requires the 'langgraph' package. "
                "Install with: pip install langgraph"
            ) from exc
        self.graph = compiled_graph
        self._state: dict[str, Any] = {"messages": []}

    async def step(self, conversation: list[Message]):
        # Translate the gzl conversation into LangGraph's message dict shape.
        self._state["messages"] = _to_langchain_messages(conversation)

        # Step the graph until it either proposes a tool call or finishes.
        async for event in self.graph.astream(self._state, stream_mode="updates"):
            for _node, update in event.items():
                if isinstance(update, dict) and "messages" in update:
                    self._state["messages"].extend(update["messages"])
                    for msg in update["messages"]:
                        tool_calls = getattr(msg, "tool_calls", None)
                        if tool_calls:
                            tc = tool_calls[0]
                            return ToolCall(
                                tool=tc.get("name", tc.get("function", {}).get("name", "")),
                                args=tc.get("args", {})
                                or tc.get("function", {}).get("arguments", {})
                                or {},
                                call_id=tc.get("id", ""),
                            )
        # No tool call found → emit whatever the last assistant message contains.
        for msg in reversed(self._state["messages"]):
            content = getattr(msg, "content", "")
            if content:
                return FinalAnswer(text=str(content))
        return FinalAnswer(text="(no response)")


def _to_langchain_messages(conv: list[Message]) -> list[Any]:
    """Best-effort translation. LangChain has many message classes; we use dicts."""
    return [{"role": m.role, "content": m.content} for m in conv]


__all__ = ["LangGraphAgent"]
