"""Sandbox tests — kept as in v1 (subprocess sandbox is unchanged).

POSIX-only (resource module unavailable on Windows); skipped there.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Subprocess sandbox uses POSIX resource limits.",
)

from lynx.sandbox import SandboxError, run_in_subprocess  # noqa: E402


async def _identity(x: int) -> int:
    return x * 2


async def _crash(why: str) -> str:
    raise RuntimeError(why)


async def _slow() -> str:
    import time

    time.sleep(10)
    return "should not be reached"


async def test_subprocess_runs_simple_function() -> None:
    out = await run_in_subprocess(_identity, {"x": 21})
    assert out == 42


async def test_subprocess_raises_on_tool_exception() -> None:
    with pytest.raises(SandboxError):
        await run_in_subprocess(_crash, {"why": "boom"})


async def test_subprocess_enforces_timeout() -> None:
    with pytest.raises(SandboxError, match="timeout"):
        await run_in_subprocess(_slow, {}, timeout_seconds=0.5)


async def test_subprocess_supports_main_module_functions(tmp_path) -> None:
    """Functions defined in a script's __main__ must work in the child —
    the sandbox remaps the parent script as the child's __main__ (the same
    fix multiprocessing uses). Regression: this used to fail with
    'sandbox exited 1' on every script-defined tool."""
    import subprocess
    import sys

    script = tmp_path / "main_tool.py"
    script.write_text(
        "import asyncio\n"
        "from lynx.sandbox import run_in_subprocess\n\n"
        "async def double(x: int) -> int:\n"
        "    return x * 2\n\n"
        "async def main():\n"
        "    print(await run_in_subprocess(double, {'x': 21}))\n\n"
        "if __name__ == '__main__':\n"
        "    asyncio.run(main())\n"
    )
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "42"
