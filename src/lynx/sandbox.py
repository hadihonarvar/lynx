"""Subprocess sandbox helper.

NOT a security boundary. The ``run_in_subprocess`` helper runs a tool body in
a fresh Python interpreter with a stripped environment and best-effort POSIX
resource limits. It is intended to bound the blast radius of *trusted but
buggy* tools (runaway memory, runaway CPU, accidental writes to the wrong
directory) — not to contain an adversary.

What it actually provides:

  * Fresh Python interpreter (new heap, no shared in-process state)
  * ``RLIMIT_CPU`` and ``RLIMIT_AS`` set best-effort (POSIX; no-op on Windows
    and partially honoured on macOS for RLIMIT_AS)
  * Working directory pinned to ``workspace`` if given
  * Stripped ``os.environ`` (only ``env_allowlist`` keys passed through)
  * Wall-clock timeout that kills + reaps the child process

What it does NOT provide:

  * No filesystem isolation. The child can read/write the workspace dir and
    any path the user can reach.
  * No network isolation. The child can open arbitrary sockets.
  * No syscall filtering (no seccomp, no namespaces, no chroot, no container).
  * No protection against a malicious tool body — the tool function is shipped
    via ``pickle`` and runs as the same user.

For real isolation, run Lynx inside a container, a microVM, or use nsjail /
firejail / bubblewrap around the whole process.
"""

from __future__ import annotations

import asyncio
import json
import pickle
import sys
import tempfile
import textwrap
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


class SandboxError(RuntimeError):
    pass


async def run_in_subprocess(
    fn: Callable[..., Awaitable[Any]],
    args: dict[str, Any],
    *,
    cpu_seconds: int = 30,
    max_memory_mb: int = 512,
    workspace: str | None = None,
    timeout_seconds: float = 60.0,
    env_allowlist: tuple[str, ...] = ("PATH", "HOME", "USER", "LANG", "LC_ALL"),
) -> Any:
    """Run `fn(**args)` in a fresh Python subprocess with best-effort caps.

    See module docstring for a precise description of what this does and does
    not protect against. Returns the value the coroutine returned, JSON-routed
    via ``default=str`` (so non-JSON values may come back as their ``str()``).
    """
    if not asyncio.iscoroutinefunction(fn):
        raise SandboxError("subprocess sandbox supports async tools only")

    import os

    # Functions defined in a script's ``__main__`` pickle by reference to the
    # module name "__main__" — which, in the child, is the wrapper script.
    # Replicate multiprocessing's fix: ship the parent script's path and have
    # the child load it under a private name, then alias it as "__main__"
    # before unpickling. (The child loads it with __name__ != "__main__", so
    # the script's ``if __name__ == "__main__":`` guard does not re-run.)
    main_file = ""
    if getattr(fn, "__module__", None) == "__main__":
        main_mod = sys.modules.get("__main__")
        main_file = getattr(main_mod, "__file__", "") or ""
        if not main_file:
            raise SandboxError(
                f"cannot sandbox {getattr(fn, '__qualname__', fn)!r}: it is defined "
                "in an interactive __main__ with no file. Define the tool in an "
                "importable module (or a script file) instead."
            )

    try:
        pickled = pickle.dumps({"fn": fn, "args": args})
    except (pickle.PicklingError, AttributeError, TypeError) as exc:
        # Local / lambda / closure-capturing functions are not pickleable.
        # Surface a clear sandbox error rather than a raw PicklingError.
        raise SandboxError(
            f"cannot pickle tool function {getattr(fn, '__qualname__', fn)!r}: {exc}. "
            "The subprocess sandbox requires a top-level async function."
        ) from exc

    with tempfile.TemporaryDirectory() as tmp:
        payload_path = Path(tmp) / "payload.pkl"
        result_path = Path(tmp) / "result.json"
        payload_path.write_bytes(pickled)

        # Use repr() so the inner script's string literal is always escaped
        # safely on every platform regardless of what the tmp path contains.
        wrapper = textwrap.dedent(
            f"""
            import asyncio, json, pickle, resource, sys
            try:
                resource.setrlimit(resource.RLIMIT_CPU, ({cpu_seconds}, {cpu_seconds}))
            except Exception as exc:
                print(f"[lynx-sandbox] RLIMIT_CPU unsupported: {{exc}}", file=sys.stderr)
            try:
                resource.setrlimit(
                    resource.RLIMIT_AS,
                    ({max_memory_mb * 1024 * 1024}, {max_memory_mb * 1024 * 1024}),
                )
            except Exception as exc:
                print(f"[lynx-sandbox] RLIMIT_AS unsupported: {{exc}}", file=sys.stderr)

            main_file = {main_file!r}
            if main_file:
                # The pickled function lives in the parent script's __main__.
                # Load that script under a private name (its __main__ guard
                # stays off) and alias it so unpickling resolves against it.
                import importlib.util
                spec = importlib.util.spec_from_file_location("__lynx_main__", main_file)
                mod = importlib.util.module_from_spec(spec)
                sys.modules["__lynx_main__"] = mod
                spec.loader.exec_module(mod)
                sys.modules["__main__"] = mod

            with open({str(payload_path)!r}, "rb") as f:
                payload = pickle.load(f)
            value = asyncio.run(payload["fn"](**payload["args"]))
            with open({str(result_path)!r}, "w") as f:
                json.dump({{"ok": True, "value": value}}, f, default=str)
            """
        )
        script = Path(tmp) / "wrapper.py"
        script.write_text(wrapper)

        env = {k: v for k, v in os.environ.items() if k in env_allowlist}
        # Propagate sys.path so pickled function references resolve, but drop
        # empty entries — sys.path[0] is usually "" which would resolve
        # relative to the child's cwd and silently shadow library modules.
        path_entries = [p for p in sys.path if p and p != "."]
        if path_entries:
            env["PYTHONPATH"] = os.pathsep.join(path_entries)

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            cwd=workspace or tmp,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Guarantee the child is killed + reaped on ANY exit path
        # (timeout, cancellation, parser exception). Otherwise the
        # subprocess plus its stdout/stderr pipes leak file descriptors.
        try:
            try:
                _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            except TimeoutError as exc:
                raise SandboxError(f"sandbox timeout after {timeout_seconds}s") from exc

            if proc.returncode != 0:
                stderr = stderr_b.decode(errors="replace")[-1000:]
                raise SandboxError(f"sandbox exited {proc.returncode}: {stderr}")

            try:
                with result_path.open() as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                raise SandboxError(f"sandbox produced no result: {exc}") from exc

            return data["value"]
        finally:
            if proc.returncode is None:
                proc.kill()
                try:
                    await proc.wait()
                except BaseException:
                    pass
