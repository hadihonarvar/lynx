"""
================================================================
EXAMPLE 35 — "One policy, any model provider" (INTEGRATIONS)
================================================================

SCENARIO:
    Grok (xAI), Mistral, DeepSeek, Groq, OpenRouter, Together, Fireworks,
    Perplexity and Ollama all speak the OpenAI Chat Completions wire format.
    So Lynx needs no per-provider adapter — just `OpenAIAgent` pointed at the
    right endpoint. `lynx.adapters.openai_compat` is the convenience layer:

        from lynx.adapters.openai_compat import openai_compatible_agent
        agent = openai_compatible_agent("deepseek", tools=tools, model="deepseek-chat")

    The whole point for Lynx: the POLICY is provider-agnostic. The same
    `PolicyBundle` gates the same tools no matter which model proposes the
    calls — swap providers without touching governance.

WHAT THIS EXAMPLE SHOWS:
    - The provider registry (`PROVIDERS`) — stable base_url + env key per
      provider, no volatile model defaults (you always pass `model=`).
    - Building a governed agent for whichever provider you have a key for.
    - That `run_agent(agent, ..., policy=POLICY)` is byte-identical across
      providers — only the agent construction line changes.

    `grok` is xAI (api.x.ai); `groq` is Groq's cloud (api.groq.com) — distinct.

REQUIRES:
    pip install lynx-agent[openai]
    plus a key for whichever provider you pick, e.g. XAI_API_KEY,
    MISTRAL_API_KEY, DEEPSEEK_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY.
    (Ollama needs no key — just a local server.)

RUN WITH:
    python examples/35_multi_provider.py                 # list providers + dry build
    DEEPSEEK_API_KEY=sk-... python examples/35_multi_provider.py deepseek deepseek-chat
"""

from __future__ import annotations

import asyncio
import os
import sys

from lynx import ToolSet, compile_policy, run_agent, stdout_sink, tool
from lynx.adapters.openai_compat import PROVIDERS, openai_compatible_agent, resolve_credentials


@tool(reversible=True, scope=("net:read",))
async def get_weather(city: str) -> str:
    """Return a canned weather string for a city."""
    return f"{city}: 21°C, clear"


# One policy, reused for every provider: reads are allowed, anything else denied.
POLICY = """
version: 1
defaults: { on_no_match: deny }
rules:
  - id: allow-weather
    match: { tool: get_weather }
    decision: allow
"""


async def main() -> None:
    tools = ToolSet.from_functions(get_weather)
    policy = compile_policy(POLICY)

    print("OpenAI-compatible providers Lynx can target:\n")
    for name, p in sorted(PROVIDERS.items()):
        has_key = "✓ key set" if (p.key_optional or os.environ.get(p.env_key)) else "· no key"
        print(f"  {name:11} {p.base_url:38} {p.env_key:20} {has_key}")

    # Pick a provider + model from argv, default to grok.
    provider = sys.argv[1] if len(sys.argv) > 1 else "grok"
    model = sys.argv[2] if len(sys.argv) > 2 else "grok-4"

    print(f"\nGoverned agent for provider={provider!r}, model={model!r}:")
    try:
        resolve_credentials(provider)  # raises if no key configured
    except ValueError as exc:
        print(f"  (skipping live call — {exc})")
        print("  The construction line would be:")
        print(f'    agent = openai_compatible_agent("{provider}", tools=tools, model="{model}")')
        print("  …and run_agent(agent, task, tools=tools, policy=POLICY) is identical")
        print("  for EVERY provider — the policy boundary doesn't change.")
        return

    async with openai_compatible_agent(provider, tools=tools, model=model) as agent:
        result = await run_agent(
            agent,
            "What's the weather in Paris?",
            tools=tools,
            policy=policy,
            sinks=(stdout_sink(),),
        )
    print(f"\nfinal_answer: {result.final_answer}")


if __name__ == "__main__":
    asyncio.run(main())
