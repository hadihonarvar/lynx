"""Cooperative cancellation — a kill-switch the kernel actually honors.

The problem this solves (the loudest operability complaint in the wild): a
"stop" that only takes effect between turns lets a runaway agent keep editing
files for minutes after you hit it. Lynx owns the propose→decide→execute
chokepoint, so it can do better: pass a ``CancelToken`` to ``run_agent`` /
``run_graph`` and the kernel checks it at every step boundary AND immediately
before each tool executes. The worst case between "cancel" and "stopped" is
one in-flight model call or one in-flight tool call — never the rest of the
run.

``CancelToken`` is a trivial stdlib object (no asyncio.Event needed, so it is
safe to create before any event loop exists and to share across threads for
the set side):

    cancel = CancelToken()
    task = asyncio.create_task(run_agent(agent, "...", tools=t, policy=p, cancel=cancel))
    ...
    cancel.cancel("user pressed stop")   # from anywhere — a signal handler, a
                                         # web request, another task
    result = await task
    # result.error == "cancelled: user pressed stop"

Any object with a truthy ``cancelled`` property and an optional ``reason`` str
also works — the kernel only reads those two attributes, so an
``asyncio.Event`` wrapper or your own flag object is accepted too.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["CancelToken", "Cancelled"]


@runtime_checkable
class Cancelled(Protocol):
    """What the kernel reads from a cancel object: is it tripped, and why."""

    @property
    def cancelled(self) -> bool: ...

    @property
    def reason(self) -> str: ...


class CancelToken:
    """A one-way latch. Flip it from anywhere; the kernel stops at the next
    checkpoint. Idempotent — the first reason wins; later ``cancel()`` calls
    are no-ops."""

    __slots__ = ("_cancelled", "_reason")

    def __init__(self) -> None:
        self._cancelled = False
        self._reason = ""

    def cancel(self, reason: str = "cancelled by caller") -> None:
        if not self._cancelled:
            self._cancelled = True
            self._reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason
