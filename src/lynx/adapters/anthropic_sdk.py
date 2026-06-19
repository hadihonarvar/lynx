"""Anthropic Claude adapter.

Wraps the Anthropic Messages API into the Lynx ``Agent`` protocol. The
adapter takes a ``ToolSet`` at construction (no global registry); the
function signatures inside that ToolSet are reflected into tool-use
definitions sent to Claude.

Example::

    from lynx import ToolSet, tool, run_agent, compile_policy
    from lynx.adapters.anthropic_sdk import ClaudeAgent

    @tool(reversible=False, scope=("filesystem:write",))
    async def shell(cmd: str) -> str: ...

    tools = ToolSet.from_functions(shell)
    agent = ClaudeAgent(
        tools=tools,
        model="claude-opus-4-7",
        system="You are a careful sysadmin.",
    )
    await run_agent(agent, "clean up /tmp", tools=tools, policy=compile_policy(...))

Requires ``pip install lynx-agent[anthropic]`` (or ``pip install anthropic``).
"""

from __future__ import annotations

from typing import Any

from lynx.adapters._schema import toolset_to_anthropic_tools
from lynx.core.types import FinalAnswer, Message, ToolCall, ToolSet, Usage

# An ``ephemeral`` cache breakpoint: Anthropic caches the whole prompt prefix
# up to (and including) the block it's attached to, then reads it back on the
# next call within the cache lifetime instead of re-billing it at full rate.
_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}


def _usage_from_response(response: Any, model: str) -> Usage | None:
    """Map an Anthropic Messages API usage block to a Lynx Usage record."""
    raw = getattr(response, "usage", None)
    if raw is None:
        return None
    return Usage(
        input_tokens=getattr(raw, "input_tokens", None),
        output_tokens=getattr(raw, "output_tokens", None),
        cache_read_tokens=getattr(raw, "cache_read_input_tokens", None),
        cache_write_tokens=getattr(raw, "cache_creation_input_tokens", None),
        model=model,
    )


__all__ = ["ClaudeAgent"]


class ClaudeAgent:
    """An ``Agent`` that delegates ``step()`` to Anthropic's Claude.

    Stateless across calls: each ``step()`` rebuilds the request from the
    immutable conversation it receives. The adapter holds only its client
    and configuration.

    **Client lifetime.** ``AsyncAnthropic`` keeps an internal HTTP/2 connection
    pool. If you let ``ClaudeAgent`` auto-construct one (``client=None``),
    the agent owns it: use the agent as an async context manager or call
    ``aclose()`` when done, otherwise the pool leaks until garbage
    collection. If you pass your own client in, you own it::

        async with ClaudeAgent(tools=tools) as agent:
            await run_agent(agent, ..., tools=tools, policy=policy)

    For high-throughput services, share one client across all agents::

        client = AsyncAnthropic()
        try:
            for req in incoming:
                agent = ClaudeAgent(tools=tools, client=client)
                await run_agent(agent, ...)
        finally:
            await client.aclose()
    """

    def __init__(
        self,
        *,
        tools: ToolSet,
        model: str = "claude-opus-4-7",
        system: str = "",
        max_tokens: int = 4096,
        cache_prompt: bool = True,
        client: Any | None = None,
    ) -> None:
        """``cache_prompt`` (default ``True``) turns on Anthropic prompt
        caching: the static prefix — the system prompt and the tool schemas —
        is marked with a cache breakpoint, and a second breakpoint trails the
        growing conversation, so each step re-reads the prior turns from cache
        instead of re-billing them at full input rate. The adapter already
        reports ``cache_read_tokens`` / ``cache_write_tokens`` on ``Usage``;
        this is what makes those numbers non-zero. Caching is silently ignored
        by the API below its minimum prefix size, so it is safe to leave on;
        set ``cache_prompt=False`` to send plain, unmarked prompts.
        """
        if client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:
                raise ImportError(
                    "ClaudeAgent requires the 'anthropic' package. "
                    "Install with: pip install anthropic"
                ) from exc
            client = AsyncAnthropic()
            self._owns_client = True
        else:
            self._owns_client = False
        self._client = client
        self._tools = tools
        self._cache_prompt = cache_prompt
        tool_defs = toolset_to_anthropic_tools(tools)
        if cache_prompt and tool_defs:
            # Breakpoint on the last (static) tool schema caches the system
            # prompt + every tool definition. Shallow-copy so the shared
            # schema list returned by the builder is never mutated.
            tool_defs = [
                *tool_defs[:-1],
                {**tool_defs[-1], "cache_control": dict(_CACHE_CONTROL)},
            ]
        self._tool_defs = tool_defs
        self._model = model
        self._system = system
        if cache_prompt and system:
            self._system_param: Any = [
                {"type": "text", "text": system, "cache_control": dict(_CACHE_CONTROL)}
            ]
        else:
            self._system_param = system
        self._max_tokens = max_tokens

    async def aclose(self) -> None:
        """Release the underlying Anthropic client's HTTP connection pool.

        Only closes the client when ClaudeAgent instantiated it (``client=None``
        at construction). If the caller passed a client in, the caller owns it
        and we leave it alone.
        """
        if self._owns_client:
            close = getattr(self._client, "aclose", None)
            if close is not None:
                await close()

    async def __aenter__(self) -> ClaudeAgent:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def step(self, conversation: tuple[Message, ...]) -> ToolCall | FinalAnswer:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": _to_anthropic_messages(conversation, cache_prompt=self._cache_prompt),
        }
        if self._system:
            kwargs["system"] = self._system_param
        if self._tool_defs:
            kwargs["tools"] = self._tool_defs

        response = await self._client.messages.create(**kwargs)
        usage = _usage_from_response(response, self._model)

        text_parts: list[str] = []
        tool_use_blocks: list[Any] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "tool_use":
                tool_use_blocks.append(block)
            elif btype == "text":
                text_parts.append(block.text)
        if tool_use_blocks:
            # Anthropic can emit multiple tool_use blocks in one response.
            # Lynx mediates one call per step; surface the first and rely on
            # the next step to pick up the rest (the conversation will include
            # the assistant message and the first tool_result).
            chosen = tool_use_blocks[0]
            input_data = chosen.input if isinstance(chosen.input, dict) else {}
            return ToolCall(
                tool=chosen.name,
                args=dict(input_data),
                call_id=chosen.id,
                usage=usage,
            )
        return FinalAnswer(text="\n".join(text_parts).strip() or "(no response)", usage=usage)


def _mark_cache(content: Any) -> Any:
    """Attach a cache breakpoint to the last content block of a message.

    Strings are promoted to a single text block first. An empty string has
    nothing worth caching and is returned untouched (Anthropic rejects an
    empty text block carrying ``cache_control``).
    """
    if isinstance(content, str):
        if not content:
            return content
        return [{"type": "text", "text": content, "cache_control": dict(_CACHE_CONTROL)}]
    if not content:
        return content
    blocks = list(content)
    blocks[-1] = {**blocks[-1], "cache_control": dict(_CACHE_CONTROL)}
    return blocks


def _to_anthropic_messages(
    conversation: tuple[Message, ...], *, cache_prompt: bool = False
) -> list[dict[str, Any]]:
    """Translate lynx Messages into the Anthropic Messages API shape.

    Tool results become user-role messages with ``tool_result`` content blocks.
    Assistant messages that carry ``tool_call_args`` (recorded by the Lynx
    scheduler) are translated into ``tool_use`` content blocks so the API
    sees a well-formed assistant→user(tool_result) alternation.

    System messages are dropped — the caller passes them via the top-level
    ``system`` parameter.

    Consecutive same-role entries are merged so the API never receives two
    user (or two assistant) messages in a row.

    When ``cache_prompt`` is set, a cache breakpoint is attached to the final
    message's last content block: the prior turns become a cached prefix that
    the next step reads back instead of re-billing — the fix for the
    quadratic input-token growth of a long, fully re-sent agent loop.
    """
    out: list[dict[str, Any]] = []

    def push(role: str, content: Any) -> None:
        # Anthropic requires strict alternation; collapse runs of the same role.
        if out and out[-1]["role"] == role:
            prev = out[-1]["content"]
            new_blocks = (
                content if isinstance(content, list) else [{"type": "text", "text": content}]
            )
            if isinstance(prev, str):
                prev_blocks: list[Any] = [{"type": "text", "text": prev}] if prev else []
            else:
                prev_blocks = list(prev)
            out[-1]["content"] = prev_blocks + new_blocks
        else:
            out.append({"role": role, "content": content})

    for m in conversation:
        if m.role == "system":
            continue
        if m.role == "user":
            push("user", m.content)
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            if m.tool_call_args is not None and m.tool_call_id:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": m.tool_call_id,
                        "name": m.name or "",
                        "input": dict(m.tool_call_args),
                    }
                )
            if not blocks:
                # Skip empty assistant messages — Anthropic rejects them.
                continue
            push("assistant", blocks)
        elif m.role == "tool":
            push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id or "unknown",
                        "content": m.content,
                    }
                ],
            )
    if cache_prompt and out:
        out[-1]["content"] = _mark_cache(out[-1]["content"])
    return out
