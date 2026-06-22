"""OpenAI-compatible provider registry.

Grok (xAI), Mistral, DeepSeek, Groq, OpenRouter, Together, Fireworks,
Perplexity, Ollama and many others all speak the **OpenAI Chat Completions**
wire format. So Lynx doesn't need a bespoke adapter per provider — it needs the
one ``OpenAIAgent`` pointed at the right ``base_url`` with the right key. This
module is the convenience layer that does exactly that::

    from lynx import ToolSet, run_agent, compile_policy
    from lynx.adapters.openai_compat import openai_compatible_agent

    agent = openai_compatible_agent("deepseek", tools=tools, model="deepseek-chat")
    await run_agent(agent, "...", tools=tools, policy=compile_policy(...))

The registry holds only the **stable** parts — the endpoint and the env var the
key lives in. It deliberately ships **no default model**: model identifiers
change constantly across providers, so the caller always names the model. That
keeps this module correct without a maintenance treadmill.

Note the easy-to-confuse pair: ``grok`` is xAI's model API (``api.x.ai``);
``groq`` is Groq's fast-inference cloud (``api.groq.com``). Both are listed.

Requires ``pip install lynx-agent[openai]`` (or ``pip install openai``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from lynx.adapters.openai_sdk import OpenAIAgent
from lynx.core.types import ToolSet

__all__ = [
    "PROVIDERS",
    "Provider",
    "openai_compatible_agent",
    "resolve_credentials",
]


@dataclass(frozen=True, slots=True)
class Provider:
    """An OpenAI-compatible endpoint: the stable bits Lynx needs to reach it.

    ``env_key`` is the environment variable the API key is read from when the
    caller doesn't pass one explicitly. ``key_optional`` marks local providers
    (Ollama) that accept any/no key.
    """

    name: str
    base_url: str
    env_key: str
    key_optional: bool = False


# Stable infrastructure only — endpoints + env var names. No model defaults on
# purpose (those drift); the caller always specifies `model=`.
PROVIDERS: Mapping[str, Provider] = MappingProxyType(
    {
        "openai": Provider("openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
        "grok": Provider("grok", "https://api.x.ai/v1", "XAI_API_KEY"),
        "mistral": Provider("mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
        "deepseek": Provider("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
        "groq": Provider("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
        "openrouter": Provider("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
        "together": Provider("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
        "fireworks": Provider(
            "fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"
        ),
        "perplexity": Provider("perplexity", "https://api.perplexity.ai", "PERPLEXITY_API_KEY"),
        "ollama": Provider(
            "ollama", "http://localhost:11434/v1", "OLLAMA_API_KEY", key_optional=True
        ),
    }
)


def _lookup(provider: str | Provider) -> Provider:
    if isinstance(provider, Provider):
        return provider
    try:
        return PROVIDERS[provider]
    except KeyError:
        known = ", ".join(sorted(PROVIDERS))
        raise ValueError(
            f"unknown provider {provider!r}. Known: {known}. "
            f"For anything else, pass a Provider(...) or use OpenAIAgent(base_url=..., api_key=...)."
        ) from None


def resolve_credentials(
    provider: str | Provider,
    *,
    api_key: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Resolve a provider to ``(base_url, api_key)``. Pure — no client built.

    Key precedence: explicit ``api_key`` arg, else ``env[provider.env_key]``.
    A ``key_optional`` provider (Ollama) falls back to the placeholder
    ``"not-needed"`` so the OpenAI SDK, which insists on a non-empty key, is
    satisfied for local servers that ignore it.
    """
    p = _lookup(provider)
    environ: Mapping[str, str] = os.environ if env is None else env
    key = api_key or environ.get(p.env_key)
    if not key:
        if p.key_optional:
            key = "not-needed"
        else:
            raise ValueError(
                f"no API key for provider {p.name!r}: pass api_key=... or set "
                f"the {p.env_key} environment variable."
            )
    return p.base_url, key


def openai_compatible_agent(
    provider: str | Provider,
    *,
    tools: ToolSet,
    model: str,
    system: str = "",
    api_key: str | None = None,
) -> OpenAIAgent:
    """Build an ``OpenAIAgent`` aimed at an OpenAI-compatible provider.

    ``provider`` is a registry key (``"grok"``, ``"mistral"``, ``"deepseek"``,
    ``"groq"``, ``"openrouter"``, …) or a ``Provider`` you construct yourself.
    ``model`` is always required — provider model names change too often to
    default safely. The returned agent owns its client (auto-closes via
    ``async with`` / ``aclose()``), exactly like ``OpenAIAgent()``.
    """
    base_url, key = resolve_credentials(provider, api_key=api_key)
    return OpenAIAgent(tools=tools, model=model, system=system, base_url=base_url, api_key=key)
