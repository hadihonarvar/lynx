"""Compressor seam — pluggable tool-result compression (token optimization).

The seam's contract: a fresh tool result is passed through the compressor
before it enters the conversation, so the compressed text is what the model
sees, what the journal records, and what a resumed run replays. The default
(no compressor) changes nothing; the reference compressors only touch
successful string results; a compressor that raises fails OPEN — the model
gets the original output, never a dropped one.
"""

from __future__ import annotations

from typing import Any

import pytest

from lynx import (
    ActionRequest,
    ActionResult,
    Compressor,
    ExecutionContext,
    FinalAnswer,
    Message,
    Principal,
    ToolCall,
    ToolDef,
    ToolSet,
    auto_deny,
    compile_policy,
    compose_compressors,
    dedup_compressor,
    external_filter_compressor,
    identity_compressor,
    route_compressor,
    run_agent,
    tool,
    truncate_compressor,
)
from lynx.core.types import ToolMetadata, now_utc

ALLOW_ALL = "version: 1\ndefaults: { on_no_match: allow }\nrules: []"


# --- helpers ----------------------------------------------------------------


def _request(tool_name: str = "t", *, compress: str | None = None) -> ActionRequest:
    return ActionRequest(
        tool=tool_name,
        args={},
        declared=ToolMetadata(cost="low", reversible=True, scope=(), compress=compress),
        context=ExecutionContext(
            principal=Principal(kind="user", id="u"),
            environment="dev",
            workspace=".",
            correlation_id="c",
            step_seq=0,
            timestamp=now_utc(),
        ),
    )


def _tool(name: str = "t", *, compress: str | None = None) -> ToolDef:
    async def fn() -> str:  # pragma: no cover - never executed in unit tests
        return ""

    return ToolDef(
        name=name,
        description="",
        fn=fn,
        shadow_fn=None,
        metadata=ToolMetadata(cost="low", reversible=True, scope=(), compress=compress),
    )


def _ok(value: Any) -> ActionResult:
    return ActionResult(ok=True, value=value)


# --- unit: truncate ---------------------------------------------------------


async def test_truncate_keeps_head_and_tail_and_elides_middle():
    comp = truncate_compressor(max_chars=100, keep_head_ratio=0.6)
    big = "H" * 500 + "T" * 500
    out = await comp(_ok(big), _request(), _tool())
    assert len(out.value) < len(big)
    assert out.value.startswith("H")
    assert out.value.endswith("T")
    assert "elided by lynx truncate_compressor" in out.value


async def test_truncate_leaves_small_text_untouched():
    comp = truncate_compressor(max_chars=1000)
    small = _ok("short")
    out = await comp(small, _request(), _tool())
    assert out is small  # identical object, no copy


async def test_truncate_passes_through_non_string_and_errors():
    comp = truncate_compressor(max_chars=1)
    payload = _ok({"k": "v"})
    assert await comp(payload, _request(), _tool()) is payload
    err = ActionResult(ok=False, error="x" * 100)
    assert await comp(err, _request(), _tool()) is err


def test_truncate_rejects_bad_params():
    with pytest.raises(ValueError):
        truncate_compressor(max_chars=0)
    with pytest.raises(ValueError):
        truncate_compressor(keep_head_ratio=1.5)


# --- unit: dedup ------------------------------------------------------------


async def test_dedup_collapses_consecutive_duplicate_lines():
    comp = dedup_compressor()
    text = "err\n" + "same\n" * 9 + "end"
    out = await comp(_ok(text), _request(), _tool())
    assert "same  (x9)" in out.value
    assert "err" in out.value
    assert "end" in out.value
    assert len(out.value) < len(text)


async def test_dedup_noop_when_no_runs():
    comp = dedup_compressor()
    text = "a\nb\nc"
    out = await comp(_ok(text), _request(), _tool())
    assert out.value == text  # smaller-or-equal guard returns original


def test_dedup_rejects_bad_min_run():
    with pytest.raises(ValueError):
        dedup_compressor(min_run=1)


# --- unit: compose ----------------------------------------------------------


async def test_compose_applies_left_to_right():
    # dedup first (collapses the repeats), then truncate the remainder.
    comp = compose_compressors(dedup_compressor(), truncate_compressor(max_chars=20))
    text = "x" * 200 + "\n" + "dup\n" * 5
    out = await comp(_ok(text), _request(), _tool())
    assert "dup  (x5)" in out.value or "elided" in out.value
    assert len(out.value) < len(text)


async def test_compose_empty_is_identity():
    comp = compose_compressors()
    payload = _ok("anything")
    assert await comp(payload, _request(), _tool()) is payload


# --- unit: route ------------------------------------------------------------


async def test_route_picks_compressor_by_tool_hint():
    comp = route_compressor(
        {
            None: truncate_compressor(max_chars=10),
            "raw": identity_compressor(),
        }
    )
    big = _ok("Z" * 100)
    # default route compresses
    out_default = await comp(big, _request(), _tool())
    assert len(out_default.value) < 100
    # "raw"-hinted tool opts out
    out_raw = await comp(big, _request(compress="raw"), _tool(compress="raw"))
    assert out_raw.value == "Z" * 100


async def test_route_unknown_hint_passes_through_when_no_default():
    comp = route_compressor({"logs": dedup_compressor()})  # no None default
    payload = _ok("a" * 100)
    out = await comp(payload, _request(compress="other"), _tool(compress="other"))
    assert out is payload  # fail open: unknown hint, no default -> unchanged


# --- unit: external filter --------------------------------------------------


async def test_external_filter_replaces_with_shrunk_stdout():
    # `head -c 5` shrinks any input to its first 5 bytes.
    comp = external_filter_compressor(["head", "-c", "5"])
    out = await comp(_ok("abcdefghij"), _request(), _tool())
    assert out.value == "abcde"


async def test_external_filter_fails_open_on_missing_binary():
    comp = external_filter_compressor(["definitely-not-a-real-binary-xyz"])
    payload = _ok("keep me")
    out = await comp(payload, _request(), _tool())
    assert out is payload


async def test_external_filter_fails_open_when_output_not_smaller():
    comp = external_filter_compressor(["cat"])  # echoes input unchanged
    payload = _ok("same size")
    out = await comp(payload, _request(), _tool())
    assert out is payload


def test_external_filter_rejects_empty_argv():
    with pytest.raises(ValueError):
        external_filter_compressor([])


# --- scheduler wiring -------------------------------------------------------


@tool(reversible=True, scope=("compute:exec",))
async def noisy() -> str:
    """Returns a large, repetitive blob."""
    return "LOG\n" * 50


class _Scripted:
    def __init__(self, *actions: Any) -> None:
        self._actions = list(actions)

    async def step(self, conversation: tuple[Message, ...]) -> Any:
        return self._actions.pop(0)


async def test_compressor_shrinks_result_in_conversation_and_emits_event():
    events: list[tuple[str, dict[str, Any]]] = []

    async def sink(ev: Any) -> None:
        events.append((ev.kind, dict(ev.body)))

    captured: list[str] = []

    async def capture_compressor(
        result: ActionResult, request: ActionRequest, tool: ToolDef
    ) -> ActionResult:
        out = await dedup_compressor()(result, request, tool)
        captured.append(out.value)
        return out

    tools = ToolSet.from_functions(noisy)
    agent = _Scripted(
        ToolCall(tool="noisy", args={}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    result = await run_agent(
        agent,
        task="go",
        tools=tools,
        policy=compile_policy(ALLOW_ALL),
        sinks=(sink,),
        on_approval=auto_deny("none"),
        compressor=capture_compressor,
    )
    assert result.final_answer == "done"
    # The dedup compressor collapsed the 50 repeated lines.
    assert "LOG  (x50)" in captured[0]
    compressed_events = [b for k, b in events if k == "step.compressed"]
    assert len(compressed_events) == 1
    ev = compressed_events[0]
    assert ev["tool"] == "noisy"
    assert ev["after_chars"] < ev["before_chars"]
    assert ev["est_tokens_saved"] >= 0


async def test_compressor_fails_open_and_emits_compress_failed():
    events: list[str] = []

    async def sink(ev: Any) -> None:
        events.append(ev.kind)

    async def boom(result: ActionResult, request: ActionRequest, tool: ToolDef) -> ActionResult:
        raise RuntimeError("compressor exploded")

    tools = ToolSet.from_functions(noisy)
    agent = _Scripted(
        ToolCall(tool="noisy", args={}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    result = await run_agent(
        agent,
        task="go",
        tools=tools,
        policy=compile_policy(ALLOW_ALL),
        sinks=(sink,),
        on_approval=auto_deny("none"),
        compressor=boom,
    )
    # Run still succeeds; original output was used, not dropped.
    assert result.final_answer == "done"
    assert "step.compress_failed" in events
    assert "step.compressed" not in events


async def test_identity_compressor_emits_no_event():
    events: list[str] = []

    async def sink(ev: Any) -> None:
        events.append(ev.kind)

    tools = ToolSet.from_functions(noisy)
    agent = _Scripted(
        ToolCall(tool="noisy", args={}, call_id="c1"),
        FinalAnswer(text="done"),
    )
    await run_agent(
        agent,
        task="go",
        tools=tools,
        policy=compile_policy(ALLOW_ALL),
        sinks=(sink,),
        on_approval=auto_deny("none"),
        compressor=identity_compressor(),
    )
    assert "step.compressed" not in events  # nothing got smaller


def test_reference_compressors_satisfy_protocol():
    assert isinstance(identity_compressor(), Compressor)
    assert isinstance(truncate_compressor(), Compressor)
    assert isinstance(dedup_compressor(), Compressor)
    assert isinstance(route_compressor({}), Compressor)
    assert isinstance(compose_compressors(), Compressor)
    assert isinstance(external_filter_compressor(["cat"]), Compressor)
