"""OpenAI GPT adapter.

Wraps the OpenAI Chat Completions API into the Gazelle Agent protocol.

Example::

    from gazelle import tool, runtime
    from gazelle.adapters.openai_sdk import OpenAIAgent

    @tool(reversible=False, scope=["filesystem:write"])
    async def shell(cmd: str) -> str: ...

    agent = OpenAIAgent(model="gpt-5", system="You are a careful sysadmin.")
    await runtime.run(agent, task="clean up /tmp", policy="policy.yaml")

Requires `pip install gazelle[openai]` (or `pip install openai`).
"""

from __future__ import annotations

import json
from typing import Any

from gazelle.adapters.anthropic_sdk import _signature_to_json_schema
from gazelle.core.mediator import get_registry
from gazelle.sdk import FinalAnswer, Message, ToolCall


class OpenAIAgent:
    """An Agent that delegates step() to OpenAI's GPT."""

    def __init__(
        self,
        model: str = "gpt-5",
        system: str = "",
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise ImportError(
                    "OpenAIAgent requires the 'openai' package. Install with: pip install openai"
                ) from exc
            client = AsyncOpenAI()
        self.client = client
        self.model = model
        self.system = system

    async def step(self, conversation: list[Message]):
        tools = _tools_for_openai()
        messages = _to_openai_messages(conversation, self.system)
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools

        response = await self.client.chat.completions.create(**kwargs)
        choice = response.choices[0].message

        if choice.tool_calls:
            call = choice.tool_calls[0]
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            return ToolCall(tool=call.function.name, args=args, call_id=call.id)
        return FinalAnswer(text=choice.content or "(no response)")


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def _tools_for_openai() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, registered in get_registry().all().items():
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": registered.description or f"Tool {name}",
                    "parameters": _signature_to_json_schema(registered),
                },
            }
        )
    return out


def _to_openai_messages(conversation: list[Message], system: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in conversation:
        if m.role == "system":
            out.append({"role": "system", "content": m.content})
        elif m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            out.append({"role": "assistant", "content": m.content})
        elif m.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id or "unknown",
                    "content": m.content,
                }
            )
    return out


__all__ = ["OpenAIAgent"]
