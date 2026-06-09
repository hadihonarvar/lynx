"""
================================================================
EXAMPLE 03 — "See it before it's real" (SIMPLE)
================================================================

SCENARIO:
    Imagine the smart assistant wants to write a letter for you. You want
    to read the letter BEFORE it goes in the mailbox — what if there's a
    typo? What if the assistant made up something silly?

    With Lynx, you write a rule: "always show me the letter first."

    The assistant writes; Lynx hands you a PREVIEW (what it would write,
    how many bytes, whether it would overwrite an existing letter) — but
    no ink touches paper. You see the preview, decide if it's right,
    and only then save for real.

    In the computer world, this is "dry-run" — execute in pencil first.

REAL-WORLD USE CASE:
    Anything where "look before you leap" matters:
      - Generating config files
      - Writing migration SQL
      - Sending emails
      - Posting to social media
      - Pushing infrastructure changes
    The agent proposes the exact thing it wants to do; Lynx shows you the
    PREVIEW; if the preview looks fine, the agent (or you) approves the
    real run.

WHAT THIS EXAMPLE SHOWS:
    - A `@tool` with a `.shadow` (the side-effect-free twin)
    - A DRY_RUN verdict
    - The agent sees the PREVIEW but the disk is untouched
    - After the example finishes, the would-be file is NOT on disk

RUN WITH:
    python examples/03_preview_writes.py

WHAT YOU'LL SEE:
    Step 0:  write_file({path: "...", content: "..."})  →  dry_run  ✓
             Preview: would write 47 bytes to /tmp/.../letter.txt
                      first 200 chars: "Dear customer, thank you ..."
                      would_overwrite: False

    After:   letter.txt does NOT exist on disk.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from lynx import FinalAnswer, Message, Runtime, ToolCall, tool
from lynx.core.mediator import get_registry
from lynx.policy import compile_policy
from lynx.stores.sqlite import SQLiteStore

# ---------------------------------------------------------------------------
# Tool + Shadow — the real write_file, plus its preview twin.
# ---------------------------------------------------------------------------


@tool(cost="low", reversible=False, scope=["filesystem:write"])
async def write_file(path: str, content: str) -> str:
    """Save text to a file. The REAL one — actually writes to disk."""
    Path(path).write_text(content)
    return f"wrote {len(content)} bytes to {path}"


@write_file.shadow
async def _write_file_preview(path: str, content: str) -> dict:
    """The PREVIEW twin. Returns what the write would do, without doing it."""
    p = Path(path)
    return {
        "would_write": path,
        "bytes": len(content.encode()),
        "would_overwrite": p.exists(),
        "first_200_chars": content[:200],
    }


# ---------------------------------------------------------------------------
# Policy — "any write_file gets a dry-run preview."
# ---------------------------------------------------------------------------


POLICY = """
version: 1
defaults:
  on_no_match: allow

rules:
  - id: write-dry-run-first
    description: "Show writes before doing them"
    match:
      tool: write_file
    decision: dry_run
    reason: "Preview the write; nothing touches disk yet"
"""


# ---------------------------------------------------------------------------
# Agent — proposes one file write.
# ---------------------------------------------------------------------------


class LetterWriter:
    def __init__(self, letter_path: Path) -> None:
        self.letter_path = letter_path
        self._step = 0
        self._plan = [
            ToolCall(
                tool="write_file",
                args={
                    "path": str(letter_path),
                    "content": (
                        "Dear customer,\n\n"
                        "Thank you for your patience. Your refund has been "
                        "processed and should appear within 3-5 business days.\n\n"
                        "Warm regards,\nSupport"
                    ),
                },
                call_id="c1",
            ),
            FinalAnswer(text="Letter drafted; preview shown without writing."),
        ]

    async def step(self, conversation: list[Message]):
        action = self._plan[self._step]
        self._step += 1
        return action


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        letter = Path(tmp) / "letter.txt"
        runtime = Runtime(
            store=SQLiteStore(f"{tmp}/state.db"),
            policy=compile_policy(POLICY),
        )

        print(f"Target file: {letter}")
        print(f"Exists before run? {letter.exists()}")
        print()

        result = await runtime.run(
            agent=LetterWriter(letter),
            task="Draft a customer letter and let me see it first.",
            principal={"kind": "user", "id": "demo"},
        )

        for step in runtime.get_steps(result.run_id):
            if step.action and step.action.tool == "write_file":
                preview = step.result.value if step.result else {}
                print("Step 0: write_file proposed.")
                print("  Verdict:  dry_run")
                print(f"  Preview:  would write {preview.get('preview', {}).get('bytes')} bytes")
                print(
                    f"            would overwrite: {preview.get('preview', {}).get('would_overwrite')}"
                )
                print(
                    f"            first chars: {preview.get('preview', {}).get('first_200_chars')[:80]}..."
                )

        print()
        print(f"Exists after run?  {letter.exists()}")
        if not letter.exists():
            print("  → DRY-RUN WORKED: the disk is untouched, only a preview was shown.")


if __name__ == "__main__":
    get_registry().clear()

    @tool(cost="low", reversible=False, scope=["filesystem:write"])
    async def write_file(path: str, content: str) -> str:
        Path(path).write_text(content)
        return f"wrote {len(content)} bytes to {path}"

    @write_file.shadow
    async def _write_file_preview(path: str, content: str) -> dict:
        p = Path(path)
        return {
            "would_write": path,
            "bytes": len(content.encode()),
            "would_overwrite": p.exists(),
            "first_200_chars": content[:200],
        }

    asyncio.run(main())
