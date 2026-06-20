"""Tests for the OpenAI-compatible provider registry (grok / mistral / deepseek / …).

Credential resolution is pure and needs no SDK. The factory build is guarded by
``importorskip("openai")`` and never touches the network (constructing
``AsyncOpenAI`` with a dummy key makes no request).
"""

from __future__ import annotations

import pytest

from lynx import ToolSet, tool
from lynx.adapters.openai_compat import (
    PROVIDERS,
    Provider,
    openai_compatible_agent,
    resolve_credentials,
)


@tool(reversible=False, scope=("net",))
async def ping(host: str) -> str:
    """Ping a host."""
    return host


TOOLS = ToolSet.from_functions(ping)


def test_registry_covers_the_popular_providers():
    for name in ("openai", "grok", "mistral", "deepseek", "groq", "openrouter"):
        assert name in PROVIDERS
    # grok (xAI) and groq (Groq cloud) are distinct endpoints — easy to confuse.
    assert PROVIDERS["grok"].base_url == "https://api.x.ai/v1"
    assert PROVIDERS["groq"].base_url == "https://api.groq.com/openai/v1"
    # every endpoint is an absolute http(s) URL with a named env key
    for p in PROVIDERS.values():
        assert p.base_url.startswith("http")
        assert p.env_key


def test_resolve_uses_explicit_key_first():
    base_url, key = resolve_credentials("deepseek", api_key="sk-explicit", env={})
    assert base_url == "https://api.deepseek.com/v1"
    assert key == "sk-explicit"


def test_resolve_falls_back_to_env_var():
    base_url, key = resolve_credentials("mistral", env={"MISTRAL_API_KEY": "sk-env"})
    assert base_url == "https://api.mistral.ai/v1"
    assert key == "sk-env"


def test_resolve_missing_key_is_a_clear_error():
    with pytest.raises(ValueError, match="no API key for provider 'grok'"):
        resolve_credentials("grok", env={})


def test_resolve_ollama_key_is_optional():
    # Local provider: no key needed; a placeholder is supplied so the SDK is happy.
    base_url, key = resolve_credentials("ollama", env={})
    assert base_url == "http://localhost:11434/v1"
    assert key == "not-needed"


def test_unknown_provider_lists_known_ones():
    with pytest.raises(ValueError, match="unknown provider 'mistralai'"):
        resolve_credentials("mistralai", env={})


def test_custom_provider_object_bypasses_registry():
    custom = Provider("acme", "https://llm.acme.test/v1", "ACME_KEY")
    base_url, key = resolve_credentials(custom, env={"ACME_KEY": "sk-acme"})
    assert base_url == "https://llm.acme.test/v1"
    assert key == "sk-acme"


def test_factory_builds_agent_pointed_at_provider():
    pytest.importorskip("openai")
    agent = openai_compatible_agent(
        "grok", tools=TOOLS, model="grok-4", api_key="sk-test", system="be terse"
    )
    # The agent owns the client it built and aimed it at xAI.
    assert agent._owns_client is True
    assert str(agent._client.base_url).rstrip("/") == "https://api.x.ai/v1"
    assert agent._model == "grok-4"


def test_factory_requires_a_known_or_custom_provider():
    pytest.importorskip("openai")
    with pytest.raises(ValueError, match="unknown provider"):
        openai_compatible_agent("nope", tools=TOOLS, model="x", api_key="k")


def test_agent_rejects_client_and_base_url_together():
    # Ambiguous: a prebuilt client already carries its endpoint/key.
    pytest.importorskip("openai")
    from lynx.adapters.openai_sdk import OpenAIAgent

    with pytest.raises(ValueError, match="not both"):
        OpenAIAgent(tools=TOOLS, client=object(), base_url="https://x")
