"""Agent protocol + message shapes.

The runtime accepts any object implementing the ``Agent`` protocol below.
``Message``, ``ToolCall``, ``FinalAnswer`` are re-exported from
``lynx.core.types`` for ergonomic ``from lynx import Message, ToolCall, ...``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from lynx.core.types import FinalAnswer, Message, ToolCall

__all__ = ["Agent", "AgentAction", "FinalAnswer", "Message", "ToolCall"]


AgentAction = ToolCall | FinalAnswer


@runtime_checkable
class Agent(Protocol):
    """Minimal contract every agent must satisfy.

    One async method. Receives the conversation (immutable tuple) and returns
    either a tool call or a final answer.
    """

    async def step(self, conversation: tuple[Message, ...]) -> AgentAction: ...
