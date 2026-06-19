"""Adapter smoke tests.

The goal is not to exercise each framework SDK — those are externally
maintained — but to assert that:

  1. Every adapter imports cleanly under v2 (no references to v1 internals).
  2. ClaudeAgent / OpenAIAgent satisfy the ``Agent`` protocol when given a
     ToolSet and a stubbed client.
  3. ToolSet introspection produces the right tool-definition shapes.

We never reach the network. The stubbed clients return whatever the test
needs to drive a single ``step()`` to completion.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from lynx import FinalAnswer, Message, ToolCall, ToolSet, tool
from lynx.adapters._schema import (
    tooldef_to_json_schema,
    toolset_to_anthropic_tools,
    toolset_to_openai_tools,
)
from lynx.adapters.anthropic_sdk import ClaudeAgent
from lynx.adapters.openai_sdk import OpenAIAgent
from lynx.sdk import Agent


@tool(reversible=False, scope=("filesystem:write",))
async def shell(cmd: str, timeout: int = 30) -> str:
    """Run a shell command."""
    return cmd


TOOLS = ToolSet.from_functions(shell)


# ---------------------------------------------------------------------------
# Schema reflection
# ---------------------------------------------------------------------------


def test_tooldef_to_json_schema_picks_required_vs_optional():
    schema = tooldef_to_json_schema(TOOLS.get("shell"))
    assert schema["type"] == "object"
    assert schema["properties"]["cmd"] == {"type": "string"}
    assert schema["properties"]["timeout"] == {"type": "integer"}
    assert schema["required"] == ["cmd"]  # timeout has a default


def test_toolset_to_anthropic_tools_shape():
    defs = toolset_to_anthropic_tools(TOOLS)
    assert len(defs) == 1
    assert defs[0]["name"] == "shell"
    assert defs[0]["description"] == "Run a shell command."
    assert defs[0]["input_schema"]["properties"]["cmd"] == {"type": "string"}


def test_toolset_to_openai_tools_shape():
    defs = toolset_to_openai_tools(TOOLS)
    assert len(defs) == 1
    assert defs[0]["type"] == "function"
    assert defs[0]["function"]["name"] == "shell"
    assert defs[0]["function"]["parameters"]["properties"]["cmd"] == {"type": "string"}


# ---------------------------------------------------------------------------
# ClaudeAgent
# ---------------------------------------------------------------------------


class _FakeAnthropicClient:
    def __init__(self, response: Any) -> None:
        self.messages = SimpleNamespace(create=self._create)
        self._response = response
        self.last_kwargs: dict[str, Any] | None = None

    async def _create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        return self._response


def _anthropic_tool_use_response(tool_name: str, args: dict[str, Any], call_id: str) -> Any:
    block = SimpleNamespace(type="tool_use", name=tool_name, input=args, id=call_id)
    return SimpleNamespace(content=[block])


def _anthropic_text_response(text: str) -> Any:
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block])


def test_claude_agent_satisfies_agent_protocol():
    client = _FakeAnthropicClient(_anthropic_text_response("hi"))
    agent = ClaudeAgent(tools=TOOLS, client=client, system="be careful")
    assert isinstance(agent, Agent)


@pytest.mark.asyncio
async def test_claude_agent_extracts_tool_call():
    client = _FakeAnthropicClient(_anthropic_tool_use_response("shell", {"cmd": "ls"}, "call-1"))
    # cache_prompt=False keeps the plain (unmarked) prompt shape this test asserts;
    # prompt caching is exercised by the dedicated tests below.
    agent = ClaudeAgent(tools=TOOLS, client=client, system="be careful", cache_prompt=False)
    out = await agent.step((Message(role="user", content="list files"),))
    assert isinstance(out, ToolCall)
    assert out.tool == "shell"
    assert out.args == {"cmd": "ls"}
    assert out.call_id == "call-1"
    # ToolSet definitions reached the SDK call
    assert client.last_kwargs is not None
    assert client.last_kwargs["tools"][0]["name"] == "shell"
    assert client.last_kwargs["system"] == "be careful"
    assert "cache_control" not in client.last_kwargs["tools"][0]


@pytest.mark.asyncio
async def test_claude_agent_returns_final_answer_on_text_only():
    client = _FakeAnthropicClient(_anthropic_text_response("all done"))
    agent = ClaudeAgent(tools=TOOLS, client=client)
    out = await agent.step((Message(role="user", content="status?"),))
    assert isinstance(out, FinalAnswer)
    assert out.text == "all done"


@pytest.mark.asyncio
async def test_claude_agent_translates_tool_role_messages():
    client = _FakeAnthropicClient(_anthropic_text_response("ok"))
    agent = ClaudeAgent(tools=TOOLS, client=client)
    conv = (
        Message(role="user", content="run ls"),
        Message(role="assistant", content="calling shell"),
        Message(role="tool", content="file1\nfile2", tool_call_id="call-1"),
    )
    await agent.step(conv)
    msgs = client.last_kwargs["messages"]
    # tool role becomes a user message with a tool_result block
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"][0]["type"] == "tool_result"
    assert msgs[-1]["content"][0]["tool_use_id"] == "call-1"


# ---------------------------------------------------------------------------
# ClaudeAgent — prompt caching (cache_prompt, default on)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_agent_marks_system_and_tools_for_caching():
    client = _FakeAnthropicClient(_anthropic_text_response("ok"))
    agent = ClaudeAgent(tools=TOOLS, client=client, system="be careful")  # cache_prompt default
    await agent.step((Message(role="user", content="hi"),))
    kwargs = client.last_kwargs
    # System is promoted to a block list carrying a cache breakpoint.
    assert isinstance(kwargs["system"], list)
    assert kwargs["system"][0]["text"] == "be careful"
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    # The last (static) tool schema carries a cache breakpoint.
    assert kwargs["tools"][-1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_claude_agent_caches_conversation_prefix():
    client = _FakeAnthropicClient(_anthropic_text_response("ok"))
    agent = ClaudeAgent(tools=TOOLS, client=client)
    conv = (
        Message(role="user", content="run ls"),
        Message(role="assistant", content="calling shell"),
        Message(role="tool", content="file1\nfile2", tool_call_id="call-1"),
    )
    await agent.step(conv)
    msgs = client.last_kwargs["messages"]
    # The trailing message's last block carries the conversation cache breakpoint.
    assert msgs[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert msgs[-1]["content"][-1]["type"] == "tool_result"


@pytest.mark.asyncio
async def test_claude_agent_cache_disabled_sends_plain_prompt():
    client = _FakeAnthropicClient(_anthropic_text_response("ok"))
    agent = ClaudeAgent(tools=TOOLS, client=client, system="hi", cache_prompt=False)
    await agent.step((Message(role="user", content="hi"),))
    kwargs = client.last_kwargs
    assert kwargs["system"] == "hi"
    assert "cache_control" not in kwargs["tools"][-1]


# ---------------------------------------------------------------------------
# OpenAIAgent
# ---------------------------------------------------------------------------


class _FakeOpenAIClient:
    def __init__(self, response: Any) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self._response = response
        self.last_kwargs: dict[str, Any] | None = None

    async def _create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        return self._response


def _openai_tool_call_response(name: str, args_json: str, call_id: str) -> Any:
    fn = SimpleNamespace(name=name, arguments=args_json)
    tc = SimpleNamespace(function=fn, id=call_id)
    message = SimpleNamespace(tool_calls=[tc], content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _openai_text_response(text: str) -> Any:
    message = SimpleNamespace(tool_calls=None, content=text)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_openai_agent_satisfies_agent_protocol():
    client = _FakeOpenAIClient(_openai_text_response("hi"))
    agent = OpenAIAgent(tools=TOOLS, client=client)
    assert isinstance(agent, Agent)


@pytest.mark.asyncio
async def test_openai_agent_extracts_tool_call():
    client = _FakeOpenAIClient(_openai_tool_call_response("shell", '{"cmd": "ls"}', "call-9"))
    agent = OpenAIAgent(tools=TOOLS, client=client, system="be careful")
    out = await agent.step((Message(role="user", content="list files"),))
    assert isinstance(out, ToolCall)
    assert out.tool == "shell"
    assert out.args == {"cmd": "ls"}
    assert out.call_id == "call-9"
    assert client.last_kwargs is not None
    assert client.last_kwargs["tools"][0]["function"]["name"] == "shell"
    # system prepended
    assert client.last_kwargs["messages"][0] == {"role": "system", "content": "be careful"}


@pytest.mark.asyncio
async def test_openai_agent_handles_malformed_json_args():
    """Malformed JSON args are captured as _raw_arguments so the audit trail
    shows what the model actually sent rather than dropping it silently."""
    client = _FakeOpenAIClient(_openai_tool_call_response("shell", "{not valid", "call-x"))
    agent = OpenAIAgent(tools=TOOLS, client=client)
    out = await agent.step((Message(role="user", content="?"),))
    assert isinstance(out, ToolCall)
    assert out.args == {"_raw_arguments": "{not valid"}


@pytest.mark.asyncio
async def test_anthropic_assistant_tool_use_round_trip() -> None:
    """When the conversation includes an assistant message with
    tool_call_args (recorded by the scheduler), the adapter must emit a
    proper assistant→tool_use→user→tool_result alternation."""
    client = _FakeAnthropicClient(_anthropic_text_response("ok"))
    agent = ClaudeAgent(tools=TOOLS, client=client)
    conv = (
        Message(role="user", content="run ls"),
        Message(
            role="assistant",
            content="",
            name="shell",
            tool_call_id="call-1",
            tool_call_args={"cmd": "ls"},
        ),
        Message(role="tool", content="file1", tool_call_id="call-1", name="shell"),
    )
    await agent.step(conv)
    msgs = client.last_kwargs["messages"]
    # user → assistant(tool_use) → user(tool_result)
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    use_block = next(b for b in msgs[1]["content"] if b.get("type") == "tool_use")
    assert use_block["id"] == "call-1"
    assert use_block["name"] == "shell"
    assert use_block["input"] == {"cmd": "ls"}
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"][0]["type"] == "tool_result"
    assert msgs[2]["content"][0]["tool_use_id"] == "call-1"


@pytest.mark.asyncio
async def test_openai_assistant_tool_call_round_trip() -> None:
    """Same as the Anthropic test, for OpenAI's assistant.tool_calls shape."""
    client = _FakeOpenAIClient(_openai_text_response("ok"))
    agent = OpenAIAgent(tools=TOOLS, client=client)
    conv = (
        Message(role="user", content="run ls"),
        Message(
            role="assistant",
            content="",
            name="shell",
            tool_call_id="call-1",
            tool_call_args={"cmd": "ls"},
        ),
        Message(role="tool", content="file1", tool_call_id="call-1", name="shell"),
    )
    await agent.step(conv)
    msgs = client.last_kwargs["messages"]
    # Position 1 is the assistant tool_calls message.
    assistant = msgs[1]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["id"] == "call-1"
    assert assistant["tool_calls"][0]["function"]["name"] == "shell"
    # The args round-trip as a JSON string.
    import json as _json

    assert _json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"cmd": "ls"}
    # Position 2 is the tool result.
    assert msgs[2]["role"] == "tool"
    assert msgs[2]["tool_call_id"] == "call-1"


@pytest.mark.asyncio
async def test_anthropic_merges_consecutive_same_role_messages() -> None:
    """Two adjacent user messages must collapse so the API never sees
    `[{user}, {user}]` in a row."""
    client = _FakeAnthropicClient(_anthropic_text_response("ok"))
    agent = ClaudeAgent(tools=TOOLS, client=client)
    conv = (
        Message(role="user", content="first"),
        Message(role="tool", content="result-a", tool_call_id="x"),  # → user(tool_result)
    )
    await agent.step(conv)
    msgs = client.last_kwargs["messages"]
    # Both became user, must have been merged.
    roles = [m["role"] for m in msgs]
    assert roles == ["user"]


def test_openai_empty_choices_returns_final_answer():
    """Defensive: empty choices list must not crash with IndexError."""
    import asyncio

    class _EmptyClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        async def _create(self, **kwargs):
            return SimpleNamespace(choices=[])

    agent = OpenAIAgent(tools=TOOLS, client=_EmptyClient())
    out = asyncio.run(agent.step((Message(role="user", content="?"),)))
    assert isinstance(out, FinalAnswer)


# ---------------------------------------------------------------------------
# Client-lifetime ownership (memory-leak prevention)
# ---------------------------------------------------------------------------


class _ClosableClient:
    """Stub client with an aclose() the test can observe."""

    def __init__(self) -> None:
        self.closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        return SimpleNamespace(content=[], choices=[])

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_claude_agent_aclose_user_supplied_client_not_closed() -> None:
    client = _ClosableClient()
    async with ClaudeAgent(tools=TOOLS, client=client):
        pass
    # We did not auto-create the client, so we do NOT close it.
    assert client.closed is False


@pytest.mark.asyncio
async def test_openai_agent_aclose_user_supplied_client_not_closed() -> None:
    client = _ClosableClient()
    async with OpenAIAgent(tools=TOOLS, client=client):
        pass
    assert client.closed is False


@pytest.mark.asyncio
async def test_claude_agent_aclose_closes_owned_client() -> None:
    """When the adapter auto-creates the SDK client, exiting the context
    manager must call aclose() — otherwise the HTTP pool leaks."""
    sentinel = _ClosableClient()
    # Construct without passing client to set _owns_client=True, then swap
    # in a stub we can observe — we can't import the real anthropic package.
    agent = ClaudeAgent.__new__(ClaudeAgent)
    agent._client = sentinel
    agent._owns_client = True
    async with agent:
        pass
    assert sentinel.closed is True


@pytest.mark.asyncio
async def test_openai_agent_aclose_closes_owned_client() -> None:
    sentinel = _ClosableClient()
    agent = OpenAIAgent.__new__(OpenAIAgent)
    agent._client = sentinel
    agent._owns_client = True
    async with agent:
        pass
    assert sentinel.closed is True


@pytest.mark.asyncio
async def test_openai_agent_returns_final_answer_on_text_only():
    client = _FakeOpenAIClient(_openai_text_response("done"))
    agent = OpenAIAgent(tools=TOOLS, client=client)
    out = await agent.step((Message(role="user", content="?"),))
    assert isinstance(out, FinalAnswer)
    assert out.text == "done"
