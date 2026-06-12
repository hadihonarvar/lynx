"""Executors — pluggable execution for approved actions.

Policy decides *whether* an action runs; an ``Executor`` decides *where and
how*. The mediator routes every real execution (allow / transform /
approval-granted) through one executor; dry-runs always call the shadow
in-process (shadows are side-effect-free by contract and need no isolation).

Lynx ships two executors and a router:

  * ``inline_executor()``     — call the tool right here (the default;
                                identical to pre-seam behavior)
  * ``subprocess_executor()`` — fresh interpreter + best-effort rlimits.
                                Crash/runaway protection, **NOT a security
                                boundary** (see ``lynx.sandbox`` docstring)
  * ``route_executor({...})`` — pick an executor per tool via the
                                ``@tool(isolation=...)`` metadata hint

Real isolation — Docker, gVisor, Firecracker, E2B, Modal — is **yours to
plug in**: implement the one-method protocol below (~20 lines for Docker;
see the integration cookbook). Lynx defines the chokepoint where isolation
attaches; the security boundary is whatever you put behind it — the same
stance as "you bring the database."
"""

from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from lynx.core.types import ActionRequest, ActionResult, ToolDef

__all__ = [
    "Executor",
    "inline_executor",
    "route_executor",
    "subprocess_executor",
]


@runtime_checkable
class Executor(Protocol):
    """Executes one approved action and returns its result.

    Receives the full ``ActionRequest`` (args are the *effective* args —
    already transformed if policy said TRANSFORM) and the ``ToolDef``.
    Implementations should return ``ActionResult(ok=False, error=...)`` for
    failures rather than raising; if one does raise, the mediator converts
    the exception into a failed result — a misbehaving executor never
    crashes the run.
    """

    async def __call__(self, request: ActionRequest, tool: ToolDef) -> ActionResult: ...


def inline_executor(*, timeout_seconds: float | None = None) -> Executor:
    """Run the tool in-process, right here. The default — and (with
    ``timeout_seconds=None``) exactly what the kernel did before the
    executor seam existed.

    ``timeout_seconds`` bounds each tool call's wall clock: on expiry the
    call is cancelled and the action fails with a structured timeout error —
    the run continues and the agent sees ``[error] ...`` and can adapt.
    Cancellation only interrupts cooperative (awaiting) tools; a tool stuck
    in a tight CPU loop never yields and cannot be cancelled in-process —
    use ``subprocess_executor`` for those.
    """

    async def execute(request: ActionRequest, tool: ToolDef) -> ActionResult:
        started = time.perf_counter()
        try:
            if timeout_seconds is not None:
                value = await asyncio.wait_for(
                    tool.fn(**dict(request.args)), timeout=timeout_seconds
                )
            else:
                value = await tool.fn(**dict(request.args))
            return ActionResult(
                ok=True,
                value=value,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except TimeoutError:
            return ActionResult(
                ok=False,
                error=f"TimeoutError: tool {request.tool!r} timed out after {timeout_seconds}s",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ActionResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-500:]}",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

    return execute


def subprocess_executor(
    *,
    cpu_seconds: int = 30,
    max_memory_mb: int = 512,
    timeout_seconds: float = 60.0,
    env_allowlist: tuple[str, ...] = ("PATH", "HOME", "USER", "LANG", "LC_ALL"),
) -> Executor:
    """Run the tool in a fresh Python subprocess with best-effort caps.

    Crash/runaway protection for *trusted but buggy* tools — runaway CPU,
    runaway memory, a hung loop. **NOT a security boundary**: no filesystem,
    network, or syscall isolation; a malicious tool body is not contained.
    For real isolation, plug in your own container/microVM executor.

    Constraints inherited from ``lynx.sandbox.run_in_subprocess``: the tool
    must be a top-level (picklable) async function, and the workspace comes
    from ``request.context.workspace``.
    """
    from lynx.sandbox import SandboxError, run_in_subprocess

    async def execute(request: ActionRequest, tool: ToolDef) -> ActionResult:
        started = time.perf_counter()
        try:
            value = await run_in_subprocess(
                tool.fn,
                dict(request.args),
                cpu_seconds=cpu_seconds,
                max_memory_mb=max_memory_mb,
                workspace=request.context.workspace or None,
                timeout_seconds=timeout_seconds,
                env_allowlist=env_allowlist,
            )
            return ActionResult(
                ok=True,
                value=value,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except asyncio.CancelledError:
            raise
        except SandboxError as exc:
            return ActionResult(
                ok=False,
                error=f"SandboxError: {exc}",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

    return execute


def route_executor(routes: Mapping[str | None, Executor]) -> Executor:
    """Pick an executor per tool via its ``@tool(isolation=...)`` hint.

    The ``None`` key is the default route for tools that declare no
    isolation. A tool whose hint has no matching route fails closed with a
    clear error — it does NOT silently fall back to the default, because
    "I asked for a container and got the host" is exactly the surprise this
    router exists to prevent.

        executor = route_executor({
            None: inline_executor(),                 # default
            "subprocess": subprocess_executor(),
            "container": my_docker_executor,         # yours
        })
    """

    async def execute(request: ActionRequest, tool: ToolDef) -> ActionResult:
        isolation = tool.metadata.isolation
        chosen = routes.get(isolation)
        if chosen is None and isolation is not None:
            return ActionResult(
                ok=False,
                error=(
                    f"no executor routed for isolation {isolation!r} "
                    f"(tool {tool.name!r}); routes: {sorted(k or '(default)' for k in routes)}"
                ),
            )
        if chosen is None:
            return ActionResult(
                ok=False,
                error=f"route_executor has no default (None) route for tool {tool.name!r}",
            )
        return await chosen(request, tool)

    return execute
