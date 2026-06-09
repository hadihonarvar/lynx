"""@tool and .shadow decorators — the front door for tool authors."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Coroutine
from typing import Any, Literal

from gazelle.core.mediator import RegisteredTool, get_registry
from gazelle.core.types import ToolMetadata


def tool(
    *,
    cost: Literal["low", "medium", "high"] = "low",
    reversible: bool = True,
    scope: list[str] | tuple[str, ...] = (),
    blast_radius_hint: int | Callable[..., int] | None = None,
    name: str | None = None,
    description: str | None = None,
) -> Callable[..., Any]:
    """Mark a function as agent-invocable.

    The decorated function MUST be async. If you have a sync function, wrap it
    with `asyncio.to_thread` inside an async shim before decorating.

    Example::

        @tool(cost="low", reversible=False, scope=["filesystem:write"])
        async def shell(cmd: str) -> str:
            ...
    """

    def decorator(
        fn: Callable[..., Coroutine[Any, Any, Any]],
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(
                f"@tool requires async function; {fn.__name__} is sync. "
                "Wrap sync code with asyncio.to_thread() inside an async shim."
            )

        tool_name = name or fn.__name__
        desc = description or (inspect.getdoc(fn) or "").strip()

        def metadata_factory(args: dict[str, Any]) -> ToolMetadata:
            hint: int | None
            if callable(blast_radius_hint):
                try:
                    hint = int(blast_radius_hint(**args))
                except Exception:
                    hint = None
            else:
                hint = blast_radius_hint
            return ToolMetadata(
                cost=cost,
                reversible=reversible,
                scope=tuple(scope),
                blast_radius_hint=hint,
                has_shadow=False,  # set True by .shadow attachment
            )

        registered = RegisteredTool(
            name=tool_name,
            description=desc,
            fn=fn,
            shadow_fn=None,
            metadata_factory=metadata_factory,
        )
        get_registry().register(registered)

        # Attach helpers for shadow registration and metadata introspection
        fn._tool_name = tool_name  # type: ignore[attr-defined]
        fn._metadata_factory = metadata_factory  # type: ignore[attr-defined]

        def shadow_decorator(
            shadow_fn: Callable[..., Coroutine[Any, Any, Any]],
        ) -> Callable[..., Coroutine[Any, Any, Any]]:
            if not asyncio.iscoroutinefunction(shadow_fn):
                raise TypeError("shadow function must be async")
            registered.shadow_fn = shadow_fn
            # mark metadata has_shadow=True via a wrapped factory
            base_factory = registered.metadata_factory

            def with_shadow(args: dict[str, Any]) -> ToolMetadata:
                base = base_factory(args)
                return ToolMetadata(
                    cost=base.cost,
                    reversible=base.reversible,
                    scope=base.scope,
                    blast_radius_hint=base.blast_radius_hint,
                    has_shadow=True,
                )

            registered.metadata_factory = with_shadow
            return shadow_fn

        fn.shadow = shadow_decorator  # type: ignore[attr-defined]
        return fn

    return decorator


def shadow(
    parent: Callable[..., Coroutine[Any, Any, Any]],
) -> Callable[..., Any]:
    """Alternative API: `@shadow(real_fn)` instead of `@real_fn.shadow`."""
    return parent.shadow  # type: ignore[attr-defined]
