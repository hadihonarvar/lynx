"""Tamper-evident hash-chain sink + verify_chain contract tests."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

from lynx import (
    AuditEvent,
    hash_chained_sink,
    jsonl_sink,
    multi_sink,
    stdout_sink,
    verify_chain,
)
from lynx.sinks import GENESIS_HASH


def _event(seq: int = 0, kind: str = "test", body: dict | None = None) -> AuditEvent:
    return AuditEvent(
        correlation_id="corr-1",
        bundle_id="bundle-1",
        seq=seq,
        kind=kind,
        timestamp=datetime.now(UTC),
        body=body if body is not None else {"hello": "world"},
    )


async def _write_chain(path, n: int = 3) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        sink = hash_chained_sink(handle)
        for i in range(n):
            await sink(_event(seq=i, body={"i": i}))


# ---------------------------------------------------------------------------
# Sink shape
# ---------------------------------------------------------------------------


async def test_each_line_carries_prev_hash_and_hash() -> None:
    buf = io.StringIO()
    sink = hash_chained_sink(buf)
    await sink(_event(seq=0))
    await sink(_event(seq=1))
    lines = buf.getvalue().strip().split("\n")
    assert len(lines) == 2
    rec0, rec1 = json.loads(lines[0]), json.loads(lines[1])
    # First line links to genesis; second line links to the first's hash.
    assert rec0["prev_hash"] == GENESIS_HASH
    assert rec1["prev_hash"] == rec0["hash"]
    # Same payload fields as jsonl_sink.
    assert rec0["seq"] == 0
    assert rec0["correlation_id"] == "corr-1"


async def test_chained_file_is_superset_of_jsonl() -> None:
    chained, plain = io.StringIO(), io.StringIO()
    chained_sink, plain_sink = hash_chained_sink(chained), jsonl_sink(plain)
    ev = _event(seq=7, body={"a": 1})
    await chained_sink(ev)
    await plain_sink(ev)
    crec = json.loads(chained.getvalue())
    prec = json.loads(plain.getvalue())
    # Every jsonl field is present and identical in the chained record.
    for key, value in prec.items():
        assert crec[key] == value


# ---------------------------------------------------------------------------
# verify_chain
# ---------------------------------------------------------------------------


async def test_intact_chain_verifies(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    await _write_chain(path, n=3)
    result = verify_chain(str(path))
    assert result.intact
    assert result.lines == 3
    assert result.broken_at is None


async def test_empty_file_is_trivially_intact(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text("", encoding="utf-8")
    result = verify_chain(str(path))
    assert result.intact
    assert result.lines == 0


async def test_modified_body_is_detected(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    await _write_chain(path, n=3)
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["body"] = {"i": 999}  # tamper with the payload, leave hashes intact
    lines[1] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_chain(str(path))
    assert not result.intact
    assert result.broken_at == 2
    assert "modified" in result.reason


async def test_deleted_line_is_detected(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    await _write_chain(path, n=3)
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[1]  # drop the middle event
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_chain(str(path))
    assert not result.intact
    # Line 2 (formerly line 3) now points at a prev_hash that's gone.
    assert result.broken_at == 2
    assert "deleted or reordered" in result.reason


async def test_reordered_lines_are_detected(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    await _write_chain(path, n=3)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_chain(str(path))
    assert not result.intact
    assert result.broken_at == 2


async def test_non_chained_file_is_rejected(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        sink = jsonl_sink(handle)
        await sink(_event(seq=0))
    result = verify_chain(str(path))
    assert not result.intact
    assert "not a chained file" in result.reason


async def test_garbled_line_is_rejected(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    await _write_chain(path, n=2)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    result = verify_chain(str(path))
    assert not result.intact
    assert result.broken_at == 3


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


async def test_composes_with_multi_sink(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    mirror = io.StringIO()
    with open(path, "w", encoding="utf-8") as handle:
        sink = multi_sink(hash_chained_sink(handle), stdout_sink(stream=mirror))
        await sink(_event(seq=0))
        await sink(_event(seq=1))
    assert verify_chain(str(path)).intact
    assert mirror.getvalue()  # the other sink still fired
