"""Gazelle CLI.

Mirrors the public Python API. Anything you can do in code, you can do here.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from gazelle import __version__
from gazelle.policy import load_policy_file
from gazelle.runtime import runtime as default_runtime

console = Console()


# ---------------------------------------------------------------------------


@click.group()
@click.version_option(__version__, prog_name="gazelle")
def cli() -> None:
    """Gazelle: policy-gated, durable, audited execution for AI agents."""


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


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
    approvers: ["user:local"]
"""


@cli.command()
@click.option("--dir", "directory", default=".", help="Project directory")
def init(directory: str) -> None:
    """Create policy.yaml, gazelle.toml, and .gazelle/ in the given directory."""
    d = Path(directory).resolve()
    (d / ".gazelle").mkdir(exist_ok=True)
    (d / ".gazelle" / "audit").mkdir(exist_ok=True)

    policy_path = d / "policy.yaml"
    if not policy_path.exists():
        policy_path.write_text(_DEFAULT_POLICY)
        console.print(f"[green]✔[/] wrote {policy_path}")
    else:
        console.print(f"[yellow]={policy_path} already exists, skipping[/]")

    toml_path = d / "gazelle.toml"
    if not toml_path.exists():
        toml_path.write_text(
            "[storage]\n"
            'type = "sqlite"\n'
            'path = ".gazelle/state.db"\n\n'
            "[policy]\n"
            'path = "./policy.yaml"\n\n'
            "[runtime]\n"
            'default_environment = "dev"\n'
            'default_workspace = "."\n'
        )
        console.print(f"[green]✔[/] wrote {toml_path}")

    console.print(f"[green]✔[/] initialized gazelle in {d}")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("script", type=click.Path(exists=True))
@click.option("--task", "-t", help="Override the task goal")
@click.option("--policy", "policy_path", default="policy.yaml", help="Policy file")
@click.option("--env", default="dev", help="Environment label")
def run(script: str, task: str | None, policy_path: str, env: str) -> None:
    """Run an agent script.

    The script must expose either:
      - a module-level coroutine `main()` that calls runtime.run(), or
      - a module-level `agent` object satisfying the Agent protocol.
    """
    import runpy
    import sys

    sys.path.insert(0, str(Path(script).resolve().parent))
    default_runtime.configure(policy_path=policy_path)

    namespace = runpy.run_path(script)

    if "main" in namespace and asyncio.iscoroutinefunction(namespace["main"]):
        result = asyncio.run(namespace["main"]())
    elif "agent" in namespace:
        if task is None:
            console.print("[red]✘[/] --task required when script provides only `agent`")
            raise SystemExit(1)
        result = asyncio.run(default_runtime.run(namespace["agent"], task=task, environment=env))
    else:
        console.print("[red]✘[/] script must define `main()` or `agent`")
        raise SystemExit(1)

    if result is None:
        return

    console.print(f"[bold]Run:[/] {result.run_id}")
    console.print(f"[bold]Status:[/] {result.status}")
    if result.final_answer:
        console.print(f"[bold]Final:[/] {result.final_answer}")
    if result.paused_approval_id:
        console.print(
            f"[yellow]Paused for approval:[/] gazelle approve {result.paused_approval_id}"
        )
    if result.error:
        console.print(f"[red]Error:[/] {result.error}")
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("run_id")
@click.argument("script", type=click.Path(exists=True))
@click.option("--policy", "policy_path", default="policy.yaml", help="Policy file")
def resume(run_id: str, script: str, policy_path: str) -> None:
    """Resume a paused run after its pending approval has been granted (or denied).

    Reuses the same script for tool registrations + agent. Approval state is
    read from the database, so this works across process restarts.
    """
    import runpy
    import sys

    sys.path.insert(0, str(Path(script).resolve().parent))
    default_runtime.configure(policy_path=policy_path)

    namespace = runpy.run_path(script)
    if "agent" not in namespace:
        # If the user has main() but no exported agent, instantiate it via
        # the convention that the demo defines.
        if "JanitorAgent" in namespace:
            from pathlib import Path as _P

            namespace["agent"] = namespace["JanitorAgent"](_P.cwd() / "demo-workspace")
        else:
            console.print(
                "[red]✘[/] script must define `agent` (or run from the original "
                "main()'s context) — `gazelle resume` needs to know what agent to use."
            )
            raise SystemExit(1)

    result = asyncio.run(default_runtime.resume(namespace["agent"], run_id=run_id))
    console.print(f"[bold]Resumed:[/] {result.run_id}")
    console.print(f"[bold]Status:[/] {result.status}")
    if result.final_answer:
        console.print(f"[bold]Final:[/] {result.final_answer}")
    if result.paused_approval_id:
        console.print(f"[yellow]Paused again at:[/] gazelle approve {result.paused_approval_id}")
    if result.error:
        console.print(f"[red]Error:[/] {result.error}")
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# ps / show / trace
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--limit", default=20)
def ps(limit: int) -> None:
    """List recent runs."""
    runs = default_runtime.list_runs(limit=limit)
    table = Table(title="Recent runs")
    table.add_column("run_id")
    table.add_column("status")
    table.add_column("started")
    table.add_column("steps")
    for r in runs:
        if r is None:
            continue
        table.add_row(
            r.id,
            str(r.status),
            r.started_at.isoformat(timespec="seconds"),
            str(r.last_step_seq + 1),
        )
    console.print(table)


@cli.command()
@click.argument("run_id")
def trace(run_id: str) -> None:
    """Print the step-by-step trace of a run."""
    steps = default_runtime.get_steps(run_id)
    if not steps:
        console.print(f"[yellow]No steps found for {run_id}[/]")
        return
    for step in steps:
        line = f"[bold]#{step.seq}[/]  "
        if step.action:
            line += f"[cyan]{step.action.tool}[/]({_compact(step.action.args)}) "
        if step.decision:
            color = {
                "allow": "green",
                "deny": "red",
                "dry_run": "yellow",
                "approve_required": "magenta",
                "transform": "blue",
            }.get(step.decision.verdict.value, "white")
            line += f"→ [{color}]{step.decision.verdict.value}[/]"
            if step.decision.reason:
                line += f" ({step.decision.reason})"
        if step.result:
            line += "  ✓" if step.result.ok else "  ✗"
        console.print(line)


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------


@cli.command()
def approvals() -> None:
    """List pending approvals."""
    rows = default_runtime.store.list_pending_approvals()
    if not rows:
        console.print("[dim]No pending approvals.[/]")
        return
    table = Table(title="Pending approvals")
    table.add_column("id")
    table.add_column("run_id")
    table.add_column("step")
    table.add_column("action")
    for row in rows:
        action = json.loads(row["action"])
        table.add_row(
            row["id"],
            row["run_id"],
            str(row["step_seq"]),
            f"{action['tool']}({_compact(action['args'])})",
        )
    console.print(table)


@cli.command()
@click.argument("approval_id")
@click.option("--approver", default="cli-user")
def approve(approval_id: str, approver: str) -> None:
    """Approve a pending request. Resume the run separately with `run`."""
    asyncio.run(default_runtime.approve(approval_id, approver=approver))
    console.print(f"[green]✔[/] approved {approval_id} by {approver}")


@cli.command()
@click.argument("approval_id")
@click.option("--approver", default="cli-user")
@click.option("--reason", default="")
def deny(approval_id: str, approver: str, reason: str) -> None:
    """Deny a pending request."""
    asyncio.run(default_runtime.deny(approval_id, approver=approver, reason=reason))
    console.print(f"[red]✘[/] denied {approval_id} by {approver}")


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


@cli.group()
def audit() -> None:
    """Audit log operations."""


@audit.command("verify")
@click.argument("run_id")
def audit_verify(run_id: str) -> None:
    """Verify the integrity of the audit chain for a run."""
    ok, err = default_runtime.verify_audit(run_id)
    if ok:
        console.print(f"[green]✔[/] audit chain for {run_id} is intact")
    else:
        console.print(f"[red]✘[/] audit chain broken: {err}")
        raise SystemExit(6)


@audit.command("export")
@click.argument("run_id")
def audit_export(run_id: str) -> None:
    """Emit the audit chain as jsonl on stdout."""
    for event in default_runtime.audit_chain(run_id):
        click.echo(
            json.dumps(
                {
                    "id": event.id,
                    "prev": event.prev,
                    "seq": event.seq,
                    "kind": event.kind,
                    "timestamp": event.timestamp.isoformat(),
                    "body": event.body,
                }
            )
        )


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


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
        console.print(f"[red]✘[/] {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
    console.print(f"[green]✔[/] {len(bundle.rules)} rules compiled cleanly")
    for r in bundle.rules:
        console.print(f"  [bold]{r.id}[/] (priority {r.priority}) — {r.description}")


@policy.command("bundle-id")
@click.argument("path", default="policy.yaml")
def policy_bundle_id(path: str) -> None:
    bundle = load_policy_file(path)
    console.print(bundle.id)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _compact(args: dict) -> str:
    s = json.dumps(args, separators=(",", "="))
    return s if len(s) < 60 else s[:57] + "..."


if __name__ == "__main__":
    cli()
