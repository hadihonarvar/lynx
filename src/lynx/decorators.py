"""@tool and .shadow decorators.

The decorator attaches a ``ToolDef`` to the function as ``__lynx_meta__``.
No global registration. Users explicitly bundle decorated functions into a
``ToolSet`` at call site.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from lynx.core.types import ToolDef, ToolMetadata

__all__ = ["shadow", "tool"]


def tool(
    *,
    cost: Literal["low", "medium", "high"] = "low",
    reversible: bool = True,
    scope: tuple[str, ...] = (),
    blast_radius_hint: int | None = None,
    isolation: str | None = None,
    compress: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Mark an async function as a tool.

    Attaches a ``ToolDef`` to the function as ``__lynx_meta__``. The function
    is NOT registered in any global state — users explicitly include it in a
    ``ToolSet`` at the call site::

        @tool(reversible=False, scope=("filesystem:write",))
        async def shell(cmd: str) -> str: ...

        tools = ToolSet.from_functions(shell)
        result = await run_agent(agent, "...", tools=tools, ...)
    """

    def decorator(
        fn: Callable[..., Awaitable[Any]],
    ) -> Callable[..., Awaitable[Any]]:
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(
                f"@tool requires async function; {fn.__name__} is sync. "
                "Wrap sync code with asyncio.to_thread() inside an async shim."
            )
        if hasattr(fn, "__lynx_meta__"):
            raise TypeError(
                f"{fn.__name__} is already decorated with @tool; double-decoration "
                "is unsupported because it would silently overwrite metadata."
            )

        tool_name = name or fn.__name__
        desc = description or (inspect.getdoc(fn) or "").strip()

        meta = ToolDef(
            name=tool_name,
            description=desc,
            fn=fn,
            shadow_fn=None,
            metadata=ToolMetadata(
                cost=cost,
                reversible=reversible,
                scope=tuple(scope),
                blast_radius_hint=blast_radius_hint,
                has_shadow=False,
                isolation=isolation,
                compress=compress,
            ),
        )

        # Attach metadata to the function. Tools are picked up by ToolSet
        # reading this attribute.
        fn.__lynx_meta__ = meta

        # Provide ``fn.shadow`` for attaching a shadow twin.
        def shadow_decorator(
            shadow_fn: Callable[..., Awaitable[Any]],
        ) -> Callable[..., Awaitable[Any]]:
            if not asyncio.iscoroutinefunction(shadow_fn):
                raise TypeError("shadow function must be async")
            # Replace the meta with a new one (still frozen) that points at
            # the shadow function and sets has_shadow=True.
            current: ToolDef = fn.__lynx_meta__
            # dataclasses.replace keeps every other metadata field intact —
            # rebuilding field-by-field silently drops newly added fields.
            new_meta = dataclasses.replace(
                current,
                shadow_fn=shadow_fn,
                metadata=dataclasses.replace(current.metadata, has_shadow=True),
            )
            fn.__lynx_meta__ = new_meta
            return shadow_fn

        fn.shadow = shadow_decorator
        return fn

    return decorator


def shadow(
    parent: Callable[..., Awaitable[Any]],
) -> Callable[..., Any]:
    """Alternative API: ``@shadow(real_fn)`` instead of ``@real_fn.shadow``."""
    return parent.shadow
