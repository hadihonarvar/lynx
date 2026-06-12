"""OpenAI GPT adapter — v2.

Wraps the OpenAI Chat Completions API into the Lynx ``Agent`` protocol.
The adapter takes a ``ToolSet`` at construction (no global registry).

Example::

    from lynx import ToolSet, tool, run_agent, compile_policy
    from lynx.adapters.openai_sdk import OpenAIAgent

    @tool(reversible=False, scope=("filesystem:write",))
    async def shell(cmd: str) -> str: ...

    tools = ToolSet.from_functions(shell)
    agent = OpenAIAgent(tools=tools, model="gpt-5", system="...")
    await run_agent(agent, "...", tools=tools, policy=compile_policy(...))

Requires ``pip install lynx-agent[openai]`` (or ``pip install openai``).
"""

from __future__ import annotations

import json
from typing import Any

from lynx.adapters._schema import toolset_to_openai_tools
from lynx.core.types import FinalAnswer, Message, ToolCall, ToolSet, Usage

__all__ = ["OpenAIAgent"]


def _usage_from_response(response: Any, model: str) -> Usage | None:
    """Map an OpenAI Chat Completions usage block to a Lynx Usage record."""
    raw = getattr(response, "usage", None)
    if raw is None:
        return None
    details = getattr(raw, "prompt_tokens_details", None)
    return Usage(
        input_tokens=getattr(raw, "prompt_tokens", None),
        output_tokens=getattr(raw, "completion_tokens", None),
        cache_read_tokens=getattr(details, "cached_tokens", None) if details else None,
        model=model,
    )


class OpenAIAgent:
    """An ``Agent`` that delegates ``step()`` to OpenAI's GPT.

    Stateless across calls: each ``step()`` rebuilds the request from the
    immutable conversation it receives.

    **Client lifetime.** ``AsyncOpenAI`` keeps an internal HTTP/2 connection
    pool. If you let ``OpenAIAgent`` auto-construct one (``client=None``), the
    agent owns it: use the agent as an async context manager or call
    ``aclose()`` when done. For high-throughput services, share one client
    across all agents (see ``ClaudeAgent`` docstring for the pattern).
    """

    def __init__(
        self,
        *,
        tools: ToolSet,
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
            self._owns_client = True
        else:
            self._owns_client = False
        self._client = client
        self._tools = tools
        self._tool_defs = toolset_to_openai_tools(tools)
        self._model = model
        self._system = system

    async def aclose(self) -> None:
        """Release the underlying OpenAI client's HTTP connection pool.

        Only closes the client when OpenAIAgent instantiated it (``client=None``
        at construction). If the caller passed a client in, the caller owns it
        and we leave it alone.
        """
        if self._owns_client:
            close = getattr(self._client, "aclose", None)
            if close is not None:
                await close()

    async def __aenter__(self) -> OpenAIAgent:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def step(self, conversation: tuple[Message, ...]) -> ToolCall | FinalAnswer:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": _to_openai_messages(conversation, self._system),
        }
        if self._tool_defs:
            kwargs["tools"] = self._tool_defs

        response = await self._client.chat.completions.create(**kwargs)
        usage = _usage_from_response(response, self._model)
        if not response.choices:
            return FinalAnswer(text="(no choices returned)", usage=usage)
        choice = response.choices[0].message

        if getattr(choice, "tool_calls", None):
            # OpenAI can emit parallel tool_calls; Lynx mediates one per step.
            # We surface the first; subsequent steps will see the assistant
            # tool_calls message and can pick up the rest.
            call = choice.tool_calls[0]
            raw = call.function.arguments or "{}"
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    args = {"_raw_arguments": raw}
            except json.JSONDecodeError:
                # Don't silently drop the malformed string — surface it so
                # audit/debugging shows what the model actually sent.
                args = {"_raw_arguments": raw}
            return ToolCall(tool=call.function.name, args=args, call_id=call.id, usage=usage)
        return FinalAnswer(text=choice.content or "(no response)", usage=usage)


def _to_openai_messages(conversation: tuple[Message, ...], system: str) -> list[dict[str, Any]]:
    """Translate Lynx Messages into the OpenAI Chat Completions shape.

    Assistant messages that carry ``tool_call_args`` (recorded by the Lynx
    scheduler) are translated into a ``tool_calls`` array so the API sees a
    well-formed assistant→tool→assistant flow.
    """
    import json as _json

    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in conversation:
        if m.role == "system":
            out.append({"role": "system", "content": m.content})
        elif m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            if m.tool_call_args is not None and m.tool_call_id:
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": m.tool_call_id,
                            "type": "function",
                            "function": {
                                "name": m.name or "",
                                "arguments": _json.dumps(dict(m.tool_call_args)),
                            },
                        }
                    ],
                }
                out.append(entry)
            elif m.content:
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
