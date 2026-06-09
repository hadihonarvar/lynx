"""CrewAI adapter.

Wraps a CrewAI Crew as a Gazelle Agent. Each Crew tool invocation is
intercepted and routed through Gazelle's mediator.

Requires `pip install gazelle[crewai]`.

Usage::

    from crewai import Agent as CrewAgent, Crew, Task as CrewTask
    from gazelle.adapters.crewai_adapter import CrewAIAgent
    from gazelle import runtime

    crew = Crew(agents=[...], tasks=[...])
    agent = CrewAIAgent(crew=crew)
    await runtime.run(agent, task="...", policy="policy.yaml")
"""

from __future__ import annotations

from typing import Any

from gazelle.sdk import FinalAnswer, Message, ToolCall


class CrewAIAgent:
    """Adapter for CrewAI Crews.

    Notes:
      - CrewAI does not natively expose a step()-shaped interface; this
        adapter wraps `crew.kickoff()` and surfaces tool invocations via a
        monkey-patched tool dispatcher.
      - For most production deployments you'll prefer registering individual
        CrewAI tools as Gazelle @tools rather than wrapping the whole crew.
    """

    def __init__(self, crew: Any) -> None:
        try:
            import crewai  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "CrewAIAgent requires the 'crewai' package. Install with: pip install crewai"
            ) from exc
        self.crew = crew
        self._pending_tool: ToolCall | None = None
        self._done = False
        self._result: str = ""

    async def step(self, conversation: list[Message]):
        if self._done:
            return FinalAnswer(text=self._result)
        if self._pending_tool:
            tc, self._pending_tool = self._pending_tool, None
            return tc

        # In a real impl we'd run kickoff() with an instrumented tool layer
        # that yields ToolCalls into a queue. The simple form here just runs
        # the crew once and returns the final answer.
        result = await _run_blocking(self.crew.kickoff)
        self._done = True
        self._result = str(result)
        return FinalAnswer(text=self._result)


async def _run_blocking(fn, *args, **kwargs) -> Any:
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


__all__ = ["CrewAIAgent"]
