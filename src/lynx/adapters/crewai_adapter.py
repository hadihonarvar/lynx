"""CrewAI adapter.

Wraps a CrewAI ``Crew`` as a Lynx ``Agent``. CrewAI orchestrates internally
via ``kickoff()``; this adapter runs it once and surfaces the result.

For most production deployments you'll prefer wrapping individual CrewAI
tools as Lynx ``@tool``s and bundling them into a ``ToolSet`` directly —
that gives the kernel per-call mediation. Use this adapter only when you
need the full Crew orchestration.

Requires ``pip install lynx-agent[crewai]``.

Usage::

    from crewai import Crew
    from lynx import ToolSet, run_agent, compile_policy
    from lynx.adapters.crewai_adapter import CrewAIAgent

    crew = Crew(agents=[...], tasks=[...])
    agent = CrewAIAgent(crew=crew)
    await run_agent(agent, "...", tools=ToolSet(), policy=compile_policy(...))
"""

from __future__ import annotations

import asyncio
from typing import Any

from lynx.core.types import FinalAnswer, Message, ToolCall

__all__ = ["CrewAIAgent"]


class CrewAIAgent:
    """Adapter for CrewAI Crews.

    Single-shot: ``step()`` returns the crew's final answer the first time
    it's called and stays in the terminal state thereafter.
    """

    def __init__(self, crew: Any) -> None:
        try:
            import crewai  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "CrewAIAgent requires the 'crewai' package. Install with: pip install crewai"
            ) from exc
        self._crew = crew
        self._done = False
        self._result = ""

    async def step(self, conversation: tuple[Message, ...]) -> ToolCall | FinalAnswer:
        if self._done:
            return FinalAnswer(text=self._result)
        result = await asyncio.to_thread(self._crew.kickoff)
        self._done = True
        self._result = str(result)
        return FinalAnswer(text=self._result)
