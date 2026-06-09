"""Public agent contract and message shapes.

The runtime accepts any object implementing the Agent protocol below.
Adapters for LangGraph, CrewAI, OpenAI Agents SDK live in gzl/adapters/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass
class Message:
    """One turn of conversation. The agent owns the buffer; runtime appends results."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    """Agent wants to call a tool. Returned from Agent.step()."""

    tool: str
    args: dict[str, Any]
    call_id: str = ""


@dataclass
class FinalAnswer:
    """Agent has finished. Returned from Agent.step()."""

    text: str


AgentAction = ToolCall | FinalAnswer


@runtime_checkable
class Agent(Protocol):
    """Minimal contract every agent must satisfy.

    Implementations can be 10 lines (see SimpleAgent below) or wrap a full
    framework (see adapters/).
    """

    async def step(self, conversation: list[Message]) -> AgentAction: ...
