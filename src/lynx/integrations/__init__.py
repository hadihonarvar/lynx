"""Framework-native governance — plug Lynx's PDP into frameworks that own the loop.

Lynx's adapters (``lynx.adapters``) wrap an LLM so *Lynx drives the loop*. The
integrations here are the inverse: the agent framework (OpenAI Agents SDK,
LangChain, PydanticAI, …) owns the loop, and Lynx governs each tool call at the
framework's native hook point — no proxy, no rewrite.

Everything routes through one framework-agnostic primitive, :class:`ToolGuard`,
which is pure orchestration of the existing kernel (``evaluate`` → ``mediate``).
Each per-framework shim is a thin optional extra; the core stays low-dependency
and the PDP stays pure.
"""

from __future__ import annotations

from lynx.integrations.core import GovernedCall, ToolGuard

__all__ = ["GovernedCall", "ToolGuard"]
