"""
================================================================
EXAMPLE 10 — "The full thing: a DevOps AI" (COMPLETE)
================================================================

SCENARIO:
    Picture a smart AI assistant whose job is to help manage your company's
    servers and cloud infrastructure. You'd want it to:

      1. Always show you what's running (read-only — fine, do whatever).
      2. Preview every change BEFORE doing it (in dev environments).
      3. Get YOUR explicit OK for anything in PRODUCTION.
      4. Refuse the truly nuclear commands no matter what (rm -rf /,
         delete-rds-instance, terraform destroy in prod).
      5. Keep a tamper-proof record of everything for your security team
         (in case anyone wants to audit who did what).

    This is the example to read if you only read ONE. It shows EVERY Lynx
    verdict (allow / dry_run / approve / deny / transform) working together
    in one realistic scenario.

REAL-WORLD USE CASE:
    Production DevOps / SRE / Platform-engineering automation:
      - kubectl interactions (the most dangerous + most common)
      - AWS CLI / GCP CLI / Azure CLI
      - Terraform / Pulumi / Ansible
      - Internal tooling (deploy scripts, rollouts, on-call tools)
    Any place where "the bot pushes the button" and the consequences of
    pushing the wrong button are severe.

WHAT THIS EXAMPLE SHOWS:
    Six tools registered (kubectl, aws_cli, terraform, shell, plus their
    shadows). One scripted agent walks through ten actions:
        1. kubectl get pods                     →  allow   ✓
        2. kubectl describe deployment app      →  allow   ✓
        3. aws s3 ls                            →  allow   ✓
        4. terraform plan                       →  dry_run ✓ (preview)
        5. kubectl apply -f bug-fix.yaml (dev)  →  dry_run ✓
        6. kubectl apply -f bug-fix.yaml (prod) →  approve_required ⏸
        7. aws ec2 terminate-instances (prod)   →  approve_required ⏸
        8. shell "rm -rf /"                     →  DENY   ✗
        9. aws rds delete-db-cluster prod       →  DENY   ✗
       10. terraform destroy (prod)             →  DENY   ✗

RUN WITH:
    python examples/10_devops_assistant.py

WHAT YOU'LL SEE:
    Each action's verdict, the reason, what would have happened, and what
    DID happen. At the end: the audit chain verification + the export
    command for compliance.

WHERE TO GO NEXT:
    - Swap ScriptedDevOpsAgent for ClaudeAgent or OpenAIAgent (see 05_).
    - Wrap this in FastAPI (see 09_) for production deployment.
    - Plug in a real Slack approval webhook (see docs/cookbook.md).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from lynx import FinalAnswer, Message, Runtime, ToolCall, tool
from lynx.core.mediator import get_registry
from lynx.policy import load_policy_file
from lynx.stores.sqlite import SQLiteStore

# ---------------------------------------------------------------------------
# Tools — fake stand-ins for the real CLIs. In production they'd shell
# out for real; here we just echo the proposed command so the example
# is safe to run anywhere.
# ---------------------------------------------------------------------------


@tool(cost="low", reversible=False, scope=["k8s:exec"])
async def kubectl(command: str, namespace: str = "default") -> str:
    """Run kubectl <command>. (Demo stub — returns the command it would run.)"""
    return f"$ kubectl --namespace={namespace} {command}"


@kubectl.shadow
async def _kubectl_shadow(command: str, namespace: str = "default") -> dict:
    return {"would_run": f"kubectl --namespace={namespace} {command}", "note": "dry-run only"}


@tool(cost="low", reversible=False, scope=["aws:exec"])
async def aws_cli(command: str, region: str = "us-east-1") -> str:
    """Run aws <command>. (Demo stub.)"""
    return f"$ aws --region={region} {command}"


@aws_cli.shadow
async def _aws_shadow(command: str, region: str = "us-east-1") -> dict:
    return {"would_run": f"aws --region={region} {command}", "note": "dry-run only"}


@tool(cost="medium", reversible=False, scope=["iac:exec"])
async def terraform(command: str, dir: str = ".") -> str:
    """Run terraform <command>. (Demo stub.)"""
    return f"$ terraform -chdir={dir} {command}"


@terraform.shadow
async def _terraform_shadow(command: str, dir: str = ".") -> dict:
    return {"would_run": f"terraform -chdir={dir} {command}", "note": "dry-run only"}


@tool(cost="medium", reversible=False, scope=["shell:exec"])
async def shell(cmd: str) -> str:
    """Generic shell. Policy will hard-block known-bad."""
    return f"$ {cmd}"


@shell.shadow
async def _shell_shadow(cmd: str) -> dict:
    return {"would_run": cmd, "note": "dry-run only"}


# ---------------------------------------------------------------------------
# Agent — the full scripted DevOps run.
# ---------------------------------------------------------------------------


class ScriptedDevOpsAgent:
    """Walks through ten realistic actions to demonstrate every verdict."""

    PLAN = [
        # 1. Read-only inspection — always fine.
        ("kubectl", {"command": "get pods"}, "dev"),
        ("kubectl", {"command": "describe deployment app"}, "dev"),
        ("aws_cli", {"command": "s3 ls"}, "dev"),
        # 2. Non-prod mutation — dry-run preview.
        ("terraform", {"command": "plan"}, "dev"),
        ("kubectl", {"command": "apply -f bug-fix.yaml"}, "dev"),
        # 3. Prod mutation — pause for approval.
        ("kubectl", {"command": "apply -f bug-fix.yaml", "namespace": "production"}, "prod"),
        ("aws_cli", {"command": "ec2 terminate-instances --instance-ids i-abc"}, "prod"),
        # 4. Catastrophic — hard-block.
        ("shell", {"cmd": "rm -rf /"}, "prod"),
        ("aws_cli", {"command": "rds delete-db-cluster --db-cluster-identifier prod-db"}, "prod"),
        ("terraform", {"command": "destroy -auto-approve"}, "prod"),
    ]

    def __init__(self):
        self._i = 0

    async def step(self, conversation: list[Message]):
        if self._i >= len(self.PLAN):
            return FinalAnswer(
                text=(
                    "DevOps walkthrough complete. Every verdict was demonstrated; "
                    "the audit log has the full record."
                )
            )
        tool_name, args, _env = self.PLAN[self._i]
        self._i += 1
        return ToolCall(tool=tool_name, args=args, call_id=f"c{self._i}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run_with_env(env: str, runtime: Runtime, agent_factory) -> None:
    """Run with a specific environment so policy can match `context.environment`."""
    print(f"\n══════════ environment = {env!r} ══════════")
    result = await runtime.run(
        agent=agent_factory(),
        task=f"DevOps walkthrough in {env}",
        principal={"kind": "service", "id": "devops-bot"},
        environment=env,
    )

    print(f"run_id:  {result.run_id}")
    print(f"status:  {result.status}")
    if result.paused_approval_id:
        print(f"paused:  lynx approve {result.paused_approval_id}")
    print(f"final:   {result.final_answer}")
    print()
    print("Step-by-step:")
    for step in runtime.get_steps(result.run_id):
        verdict = step.decision.verdict.value if step.decision else "?"
        symbol = {
            "allow": "✓",
            "dry_run": "🔍",
            "approve_required": "⏸",
            "deny": "✗",
            "transform": "↻",
        }.get(verdict, "?")
        tool_str = f"{step.action.tool}({step.action.args})" if step.action else "?"
        if len(tool_str) > 80:
            tool_str = tool_str[:77] + "..."
        reason = step.decision.reason if step.decision else ""
        print(f"  #{step.seq:>2}  {tool_str}")
        print(f"       → {verdict} {symbol}  {reason}")


async def main() -> None:
    import tempfile

    policy_path = Path(__file__).resolve().parent / "policies" / "devops.yaml"

    with tempfile.TemporaryDirectory() as tmp:
        runtime = Runtime(
            store=SQLiteStore(f"{tmp}/state.db"),
            policy=load_policy_file(policy_path),
        )

        # Run the full plan against the PROD policy environment so we exercise
        # the deny + approve_required rules.
        await run_with_env("prod", runtime, ScriptedDevOpsAgent)

        # Final audit chain check.
        print()
        print("══════════ audit chain ══════════")
        runs = runtime.list_runs(limit=1)
        if runs:
            ok, err = runtime.verify_audit(runs[0].id)
            print(f"  intact: {ok}  {err or ''}")
            print()
            print("Export the audit trail for compliance:")
            print(f"  lynx audit export {runs[0].id} > evidence.jsonl")


if __name__ == "__main__":
    get_registry().clear()

    # Re-decorate so the registry is freshly populated in this process.
    @tool(cost="low", reversible=False, scope=["k8s:exec"])
    async def kubectl(command: str, namespace: str = "default") -> str:
        return f"$ kubectl --namespace={namespace} {command}"

    @kubectl.shadow
    async def _kubectl_shadow(command: str, namespace: str = "default") -> dict:
        return {"would_run": f"kubectl --namespace={namespace} {command}", "note": "dry-run only"}

    @tool(cost="low", reversible=False, scope=["aws:exec"])
    async def aws_cli(command: str, region: str = "us-east-1") -> str:
        return f"$ aws --region={region} {command}"

    @aws_cli.shadow
    async def _aws_shadow(command: str, region: str = "us-east-1") -> dict:
        return {"would_run": f"aws --region={region} {command}", "note": "dry-run only"}

    @tool(cost="medium", reversible=False, scope=["iac:exec"])
    async def terraform(command: str, dir: str = ".") -> str:
        return f"$ terraform -chdir={dir} {command}"

    @terraform.shadow
    async def _terraform_shadow(command: str, dir: str = ".") -> dict:
        return {"would_run": f"terraform -chdir={dir} {command}", "note": "dry-run only"}

    @tool(cost="medium", reversible=False, scope=["shell:exec"])
    async def shell(cmd: str) -> str:
        return f"$ {cmd}"

    @shell.shadow
    async def _shell_shadow(cmd: str) -> dict:
        return {"would_run": cmd, "note": "dry-run only"}

    asyncio.run(main())
