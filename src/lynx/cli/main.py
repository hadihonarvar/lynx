"""Lynx CLI — minimal: init, run, trace, policy lint, policy bundle-id, --version."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

from lynx import __version__
from lynx.policy import load_policy_file

__all__ = ["cli"]


_DEFAULT_POLICY = """\
version: 1
defaults:
  on_missing_shadow: approve_required
  on_no_match: deny

rules:
  - id: read-only-allow
    description: Read-only tools are always fine
    match:
      declared.scope.contains_any: ["filesystem:read", "net:read"]
    decision: allow

  - id: shell-rm-rf-root
    description: Never delete from filesystem root
    match:
      tool: shell
      args.cmd.matches: '^\\s*rm\\s+(-[rRf]+\\s+)+/(\\s|$)'
    decision: deny
    reason: "rm -rf / is never allowed"

  - id: irreversible-needs-approval
    description: Irreversible actions require explicit approval
    match:
      declared.reversible: false
    decision: approve_required
"""


@click.group()
@click.version_option(__version__, prog_name="lynx")
def cli() -> None:
    """Lynx: stateless, type-safe policy kernel for AI agent tool calls."""


@cli.command()
@click.option("--dir", "directory", default=".", help="Project directory")
@click.option("--force", is_flag=True, help="Overwrite policy.yaml even if it already exists")
def init(directory: str, force: bool) -> None:
    """Write a starter policy.yaml in the given directory.

    Creates the directory if it doesn't already exist.
    """
    d = Path(directory).resolve()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        click.echo(f"could not create {d}: {exc}", err=True)
        sys.exit(1)
    policy_path = d / "policy.yaml"
    if policy_path.exists() and not force:
        click.echo(f"= {policy_path} already exists (use --force to overwrite)", err=True)
        sys.exit(1)
    try:
        policy_path.write_text(_DEFAULT_POLICY)
    except OSError as exc:
        click.echo(f"could not write {policy_path}: {exc}", err=True)
        sys.exit(1)
    click.echo(f"wrote {policy_path}")


@cli.command()
@click.argument("script", type=click.Path(exists=True))
def run(script: str) -> None:
    """Run a Python script that defines an async ``main()`` coroutine.

    The script owns the runtime call (``await run_agent(...)``); Lynx just
    imports it and executes ``main()``.
    """
    import runpy

    script_dir = str(Path(script).resolve().parent)
    inserted_path = False
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
        inserted_path = True
    try:
        namespace = runpy.run_path(script)

        if "main" not in namespace or not asyncio.iscoroutinefunction(namespace["main"]):
            click.echo(
                "script must define an async `main()` coroutine that calls run_agent(...)",
                err=True,
            )
            sys.exit(1)

        asyncio.run(namespace["main"]())
    finally:
        # Don't leak script_dir into sys.path after the command finishes —
        # matters when `lynx run` is called from inside another Python process.
        if inserted_path:
            try:
                sys.path.remove(script_dir)
            except ValueError:
                pass


@cli.command()
@click.argument("records_file", type=click.Path(exists=True))
@click.option("--run-id", default=None, help="Only show records for this run id")
def trace(records_file: str, run_id: str | None) -> None:
    """Reconstruct a journaled run from a JSON-lines records file.

    Reads one StepRecord per line (the ``step_record_to_json`` format used
    by file-backed RunStore implementations — see the cookbook) and prints
    what happened at every step: tool, verdict, outcome, and any uncertain
    (intent-without-result) actions.
    """
    import json

    from lynx.durability import replay, step_record_from_json

    records = []
    run_ids_seen: set[str] = set()
    with open(records_file, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                click.echo(f"line {lineno}: not JSON: {exc}", err=True)
                sys.exit(1)
            if "run_id" not in obj:
                if "correlation_id" in obj:
                    click.echo(
                        f"line {lineno}: this looks like an audit-sink file "
                        "(jsonl_sink output), not a RunStore journal — trace "
                        "reads StepRecords (step_record_to_json format)",
                        err=True,
                    )
                else:
                    click.echo(f"line {lineno}: not a StepRecord (no run_id)", err=True)
                sys.exit(1)
            run_ids_seen.add(obj["run_id"])
            if run_id is not None and obj["run_id"] != run_id:
                continue  # skip cheaply before building the full record
            try:
                records.append(step_record_from_json(line))
            except Exception as exc:
                click.echo(f"line {lineno}: unparseable record: {exc}", err=True)
                sys.exit(1)
    if not records:
        click.echo("no records found", err=True)
        sys.exit(1)
    if run_id is None and len(run_ids_seen) > 1:
        # replay() keys steps by step number — mixing runs would produce a
        # plausible-looking but wrong reconstruction.
        listing = ", ".join(sorted(run_ids_seen))
        click.echo(
            f"file contains {len(run_ids_seen)} runs ({listing}) — pick one with --run-id", err=True
        )
        sys.exit(1)

    view = replay(records)
    click.echo(f"run {view.run_id}: {view.records} records, {view.attempts} attempt(s)")
    for s in view.steps:
        if s.tool is None:
            click.echo(f"  step {s.step}: final answer: {s.message}")
            continue
        status = "?" if s.ok is None else ("ok" if s.ok else "failed")
        flag = ""
        if s.uncertain:
            flag = "  [UNCERTAIN: intent without result — action may have executed]"
        elif s.resolved_uncertain:
            flag = "  [resolved uncertain retry — the original attempt may still have executed]"
        verdict = s.verdict or "-"
        click.echo(f"  step {s.step}: {s.tool} verdict={verdict} {status}{flag}")
        if s.message:
            click.echo(f"           {s.message[:100]}")
    if view.final_answer is not None:
        click.echo(f"final: {view.final_answer}")
    else:
        click.echo("final: (run incomplete)")


@cli.group()
def policy() -> None:
    """Policy file operations."""


@policy.command("lint")
@click.argument("path", default="policy.yaml")
def policy_lint(path: str) -> None:
    """Compile-check a policy file and print rule summary."""
    try:
        bundle = load_policy_file(path)
    except Exception as exc:
        click.echo(f"{type(exc).__name__}: {exc}", err=True)
        sys.exit(1)
    click.echo(f"{len(bundle.rules)} rules compiled cleanly")
    for r in bundle.rules:
        click.echo(f"  {r.id} (priority {r.priority}) - {r.description}")


@policy.command("bundle-id")
@click.argument("path", default="policy.yaml")
def policy_bundle_id(path: str) -> None:
    """Print the content-addressed bundle ID for a policy file."""
    try:
        bundle = load_policy_file(path)
    except Exception as exc:
        click.echo(f"{type(exc).__name__}: {exc}", err=True)
        sys.exit(1)
    click.echo(bundle.id)


if __name__ == "__main__":
    cli()
