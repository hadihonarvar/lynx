"""Compressors — pluggable tool-result compression (token optimization).

Policy decides *whether* an action runs and the executor decides *where*;
a ``Compressor`` decides *how much of the result the model has to read*. The
scheduler routes every fresh tool result through one compressor (if you pass
one) before it enters the conversation — so the compressed text is what the
model sees, what the journal records, and what a resumed run replays. The
saving compounds: a large result trimmed once is a result *not re-sent in
full on every subsequent step*.

Lynx is **not** a token optimizer and ships no opinion about what to drop.
It owns the seam; the strategy is yours — the same stance as "you bring the
database / you bring the sandbox." The reference compressors below are
conveniences, not the kernel's policy:

  * ``identity_compressor()``    — no-op (the default; documents the seam)
  * ``truncate_compressor()``    — head+tail elision of oversized text
  * ``dedup_compressor()``       — collapse runs of duplicate lines with counts
  * ``compose_compressors(...)`` — chain several, left to right
  * ``route_compressor({...})``  — pick a compressor per tool via the
                                   ``@tool(compress=...)`` hint
  * ``external_filter_compressor(argv)`` — pipe text through any external
                                   filter binary (your own compressor, a
                                   summarizer) over stdin/stdout

A note on RTK (https://github.com/rtk-ai/rtk): RTK saves tokens by *running
the command itself* and exposes no stdin-filter mode, so it cannot
post-process a result Lynx has already produced. Wire RTK at the *tool*
level instead — have your shell tool run ``rtk <cmd>`` / ``rtk proxy
<cmd>`` — and use this seam for the framework-native truncate/dedup pass on
every other tool. See ``examples/32_token_optimization.py``.

Failure stance: a compressor that raises is failed **open** by the scheduler
— the model receives the tool's *original* output. A token optimizer must
never be able to silently drop a real result. The reference compressors only
touch successful, string-valued results; any other result passes through
untouched.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from lynx.core.types import ActionRequest, ActionResult, ToolDef

__all__ = [
    "Compressor",
    "compose_compressors",
    "dedup_compressor",
    "external_filter_compressor",
    "identity_compressor",
    "route_compressor",
    "truncate_compressor",
]


@runtime_checkable
class Compressor(Protocol):
    """Shrinks one tool result before it enters the model's context.

    Receives the ``ActionResult`` the executor produced, the originating
    ``ActionRequest``, and the ``ToolDef`` (so a router can read
    ``tool.metadata.compress``). Returns a result whose ``value`` is the
    text the model should see — usually a smaller version of the original.

    Implementations should return the *original* result for anything they
    don't handle (errors, non-string values, already-small output) rather
    than raise; if one does raise, the scheduler fails open and uses the
    original result — a compressor never crashes a run and never silently
    swallows a real tool output.
    """

    async def __call__(
        self, result: ActionResult, request: ActionRequest, tool: ToolDef
    ) -> ActionResult: ...


def identity_compressor() -> Compressor:
    """Return results unchanged. The default behavior when no compressor is
    passed — provided as a named value so ``route_compressor`` can map a tool
    explicitly to "no compression."
    """

    async def compress(result: ActionResult, request: ActionRequest, tool: ToolDef) -> ActionResult:
        return result

    return compress


def truncate_compressor(*, max_chars: int = 8000, keep_head_ratio: float = 0.6) -> Compressor:
    """Keep the head and tail of an oversized text result, elide the middle.

    Successful results whose ``value`` is a string longer than ``max_chars``
    are trimmed to roughly ``max_chars`` characters: a head slice, a marker
    naming how many characters were dropped, then a tail slice. The head/tail
    split is set by ``keep_head_ratio`` (0.6 = 60% head, 40% tail) — most
    command output puts the signal at the top (a summary) and the bottom (the
    error / final lines), so both ends are worth keeping. Everything else is
    returned untouched.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not 0.0 <= keep_head_ratio <= 1.0:
        raise ValueError("keep_head_ratio must be between 0.0 and 1.0")

    async def compress(result: ActionResult, request: ActionRequest, tool: ToolDef) -> ActionResult:
        value = result.value
        if not result.ok or not isinstance(value, str) or len(value) <= max_chars:
            return result
        head_len = int(max_chars * keep_head_ratio)
        tail_len = max_chars - head_len
        omitted = len(value) - head_len - tail_len
        head = value[:head_len]
        tail = value[len(value) - tail_len :] if tail_len else ""
        marker = f"\n…[{omitted} chars elided by lynx truncate_compressor]…\n"
        return dataclasses.replace(result, value=head + marker + tail)

    return compress


def dedup_compressor(*, min_run: int = 2) -> Compressor:
    """Collapse runs of identical consecutive lines into one line + a count.

    A successful string result with at least ``min_run`` repeats of the same
    line in a row (think a stack trace echoed 400 times, or a progress bar's
    leftovers) becomes ``<line>  (xN)``. If the result didn't actually get
    smaller, the original is returned. Non-string / error / single-line
    results pass through.
    """
    if min_run < 2:
        raise ValueError("min_run must be at least 2")

    async def compress(result: ActionResult, request: ActionRequest, tool: ToolDef) -> ActionResult:
        value = result.value
        if not result.ok or not isinstance(value, str) or "\n" not in value:
            return result
        lines = value.split("\n")
        out: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            j = i
            while j + 1 < n and lines[j + 1] == lines[i]:
                j += 1
            run = j - i + 1
            if run >= min_run:
                out.append(f"{lines[i]}  (x{run})")
            else:
                out.extend([lines[i]] * run)
            i = j + 1
        compressed = "\n".join(out)
        if len(compressed) >= len(value):
            return result
        return dataclasses.replace(result, value=compressed)

    return compress


def compose_compressors(*compressors: Compressor) -> Compressor:
    """Chain compressors left to right; each sees the previous one's output.

    ``compose_compressors(dedup_compressor(), truncate_compressor())`` first
    collapses duplicate lines, then truncates whatever's left — so the cap
    applies to the already-deduplicated text. With no arguments this is an
    identity compressor.
    """

    async def compress(result: ActionResult, request: ActionRequest, tool: ToolDef) -> ActionResult:
        for c in compressors:
            result = await c(result, request, tool)
        return result

    return compress


def route_compressor(routes: Mapping[str | None, Compressor]) -> Compressor:
    """Pick a compressor per tool via its ``@tool(compress=...)`` hint.

    The ``None`` key is the default route for tools that declare no hint. A
    tool whose hint has no matching route is **not** an error — it simply
    isn't compressed (the result passes through). This is the deliberate
    opposite of ``route_executor``, which fails closed: a missing isolation
    route is a security surprise worth stopping the run over, but a missing
    compression route just means "don't bother shrinking this one."

        compressor = route_compressor({
            None: truncate_compressor(),          # default for unhinted tools
            "logs": dedup_compressor(),
            "raw": identity_compressor(),         # opt a tool out explicitly
        })
    """

    async def compress(result: ActionResult, request: ActionRequest, tool: ToolDef) -> ActionResult:
        chosen = routes.get(tool.metadata.compress)
        if chosen is None:
            chosen = routes.get(None)
        if chosen is None:
            return result
        return await chosen(result, request, tool)

    return compress


def external_filter_compressor(argv: Sequence[str], *, timeout_seconds: float = 10.0) -> Compressor:
    """Pipe a text result through an external filter binary over stdin/stdout.

    Spawns ``argv`` once per result, writes the tool's string ``value`` to
    its stdin, and replaces the value with the process's stdout. For wiring a
    compressor you maintain outside Python — a summarizer, a custom log
    reducer, a ``jq`` projection. (Note: this is *not* how you use RTK — RTK
    has no stdin mode and must run the command itself at the tool level.)

    Fails **open**: a missing binary, a non-zero exit, a timeout, or output
    that isn't actually smaller all return the original result unchanged. The
    tool's real output is never lost to a flaky filter.
    """
    if not argv:
        raise ValueError("argv must be non-empty")
    argv = tuple(argv)

    async def compress(result: ActionResult, request: ActionRequest, tool: ToolDef) -> ActionResult:
        value = result.value
        if not result.ok or not isinstance(value, str) or not value:
            return result
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(value.encode()), timeout=timeout_seconds
            )
        except asyncio.CancelledError:
            if proc is not None:
                proc.kill()
            raise
        except (TimeoutError, OSError):
            if proc is not None:
                proc.kill()
            return result
        if proc.returncode != 0:
            return result
        text = stdout.decode(errors="replace")
        if len(text) >= len(value):
            return result
        return dataclasses.replace(result, value=text)

    return compress
