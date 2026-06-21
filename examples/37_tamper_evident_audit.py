"""
================================================================
EXAMPLE 37 — "Prove nobody altered the audit log" (OBSERVABILITY)
================================================================

SCENARIO:
    An audit trail you can edit isn't an audit trail. Lynx streams every
    governance decision to a sink; `jsonl_sink` writes one event per line, but
    anyone who can open the file can change a body, drop a denial, or reorder
    events undetected.

    `hash_chained_sink` closes that. Each line carries a fingerprint of itself
    chained to the line before it:

        hash(line N) = sha256(hash(line N-1) + canonical_json(event N))

    Because every fingerprint folds in the one before it, any edit, deletion, or
    reorder breaks every fingerprint downstream — caught by `verify_chain` (or
    the `lynx verify` CLI). It's a drop-in for `jsonl_sink` and composes with
    `multi_sink`. Tamper-EVIDENT: it proves nobody altered the log.

WHAT THIS EXAMPLE SHOWS:
    - Writing audit events through `hash_chained_sink`.
    - `verify_chain` reporting an intact chain.
    - Tampering with one line, and `verify_chain` pinpointing the broken line.

REQUIRES:
    pip install lynx-agent        # stdlib hashlib only — no extra deps

RUN WITH:
    python examples/37_tamper_evident_audit.py
    # then try:  lynx verify <the temp file it prints>
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from lynx import AuditEvent, hash_chained_sink, verify_chain


def _event(seq: int, kind: str, **body: object) -> AuditEvent:
    return AuditEvent(
        correlation_id="run-42",
        bundle_id="bundle-abc",
        seq=seq,
        kind=kind,
        timestamp=datetime.now(UTC),
        body=body,
    )


async def main() -> None:
    path = Path(tempfile.mkdtemp()) / "audit.jsonl"

    # 1. Stream a few governance events through the tamper-evident sink.
    with open(path, "w", encoding="utf-8") as handle:
        sink = hash_chained_sink(handle)
        await sink(_event(0, "policy.evaluated", tool="shell", verdict="allow"))
        await sink(_event(1, "policy.evaluated", tool="shell", verdict="deny",
                          reason="rm -rf / is never allowed"))
        await sink(_event(2, "tool.called", tool="http_get", url="https://api.example.com"))

    print(f"wrote audit log: {path}\n")

    # 2. Verify the untouched chain.
    result = verify_chain(str(path))
    print(f"untouched -> intact={result.intact}  events={result.lines}")
    assert result.intact

    # 3. Tamper: quietly flip the recorded denial to an allow.
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["body"]["verdict"] = "allow"  # cover up the block
    lines[1] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 4. Verify again — the edit is caught at the exact line.
    result = verify_chain(str(path))
    print(f"tampered  -> intact={result.intact}  broken_at=line {result.broken_at}")
    print(f"             reason: {result.reason}")
    assert not result.intact and result.broken_at == 2

    print(f"\nTry it yourself:  lynx verify {path}")


if __name__ == "__main__":
    asyncio.run(main())
