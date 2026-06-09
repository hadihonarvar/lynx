"""
================================================================
EXAMPLE 09 — "Lynx behind a web service" (ADVANCED)
================================================================

SCENARIO:
    The previous examples ran from the command line. But in real life you
    want your AI assistant available as a web service so other systems
    can use it: a phone app, a website, a Slack bot, a webhook from your
    CRM.

    This example wraps everything from example 07 (the refund agent) in
    a FastAPI HTTP server. Now:

      - A customer-support tool can POST a ticket and get a run ID back.
      - A web dashboard can GET that run ID to see what happened.
      - A supervisor's Slack "Approve" button can POST to a webhook to
        approve a pending refund.
      - Auditors can GET the audit chain as jsonl.

    Lynx still does its work — policy, durability, audit. FastAPI just
    exposes it over HTTP.

REAL-WORLD USE CASE:
    Production deployments where your AI agent is:
      - Behind a SaaS web app
      - Called by other services (microservices architecture)
      - Triggered by webhooks from Stripe / Twilio / Slack
      - Accessed from a phone app

    This file is intentionally close to "copy and adapt" for your own app.

WHAT THIS EXAMPLE SHOWS:
    - One Runtime singleton per process (app.state.runtime)
    - A POST /agent/run endpoint that handles a refund request
    - A GET /agent/runs/{id} endpoint to inspect any run
    - A GET /agent/runs/{id}/audit endpoint for compliance
    - A POST /agent/approvals/{id}/approve that approves AND resumes

REQUIRES:
    pip install fastapi uvicorn

RUN WITH:
    uvicorn examples.09_fastapi_service:app --reload
    # then in another terminal:
    curl -X POST localhost:8000/agent/run \\
        -H 'content-type: application/json' \\
        -d '{"customer_id": "C-123", "amount_usd": 5, "reason": "1-day outage"}'

WHAT YOU'LL SEE:
    JSON responses with run_id, status, final_answer, and (if paused)
    paused_approval_id. The full Lynx machinery operating behind HTTP.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
except ImportError as exc:
    raise SystemExit(
        "This example requires fastapi + uvicorn: pip install fastapi uvicorn"
    ) from exc

from lynx import FinalAnswer, Message, Runtime, ToolCall, tool
from lynx.core.mediator import get_registry
from lynx.policy import load_policy_file
from lynx.stores.sqlite import SQLiteStore

# ---------------------------------------------------------------------------
# Tools (registered at module import — same as example 07)
# ---------------------------------------------------------------------------


@tool(cost="low", reversible=True, scope=["customer:read"])
async def get_customer(customer_id: str) -> dict:
    fake_db = {
        "C-123": {"name": "Alice", "plan": "Pro"},
        "C-456": {"name": "Bob", "plan": "Team"},
        "C-789": {"name": "Carol", "plan": "Pro"},  # fraud watchlist
    }
    return fake_db.get(customer_id, {"error": "not found"})


@tool(cost="medium", reversible=False, scope=["customer:write", "money:transfer"])
async def refund_customer(customer_id: str, amount_usd: float, reason: str) -> dict:
    return {"refunded": amount_usd, "to": customer_id, "reason": reason, "txn": "TXN-XYZ"}


@refund_customer.shadow
async def _refund_shadow(customer_id: str, amount_usd: float, reason: str) -> dict:
    return {"would_refund": amount_usd, "to": customer_id}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class ScriptedRefundAgent:
    def __init__(self, customer_id: str, amount_usd: float, reason: str):
        self._plan = [
            ToolCall("get_customer", {"customer_id": customer_id}, call_id="c1"),
            ToolCall(
                "refund_customer",
                {"customer_id": customer_id, "amount_usd": amount_usd, "reason": reason},
                call_id="c2",
            ),
            FinalAnswer(text=f"Processed refund for {customer_id}."),
        ]
        self._i = 0

    async def step(self, conversation: list[Message]):
        a = self._plan[self._i]
        self._i += 1
        return a


# ---------------------------------------------------------------------------
# App lifecycle: one Runtime singleton per process.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    policy_path = Path(__file__).resolve().parent / "policies" / "refund.yaml"
    app.state.runtime = Runtime(
        store=SQLiteStore(Path(__file__).resolve().parent.parent / ".lynx" / "fastapi.db"),
        policy=load_policy_file(policy_path),
    )
    yield
    app.state.runtime.store.close()


app = FastAPI(lifespan=lifespan, title="Lynx FastAPI demo")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    customer_id: str
    amount_usd: float
    reason: str


class ApprovalAction(BaseModel):
    approver: str
    reason: str | None = None


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "Lynx FastAPI demo",
        "endpoints": [
            "POST /agent/run                                  start a run",
            "GET  /agent/runs/{run_id}                        inspect a run",
            "GET  /agent/runs/{run_id}/audit                  verify the audit chain",
            "POST /agent/approvals/{approval_id}/approve      approve + resume",
            "POST /agent/approvals/{approval_id}/deny         deny + resume",
        ],
        "tools_registered": get_registry().names(),
    }


@app.post("/agent/run")
async def run_agent(req: RunRequest) -> dict[str, Any]:
    """Run the agent synchronously. Returns when done OR paused for approval."""
    agent = ScriptedRefundAgent(req.customer_id, req.amount_usd, req.reason)
    result = await app.state.runtime.run(
        agent=agent,
        task=f"Refund {req.customer_id} ${req.amount_usd:.2f}",
        principal={"kind": "service", "id": "fastapi-demo"},
        environment="prod",
    )
    return {
        "run_id": result.run_id,
        "status": str(result.status),
        "final_answer": result.final_answer,
        "paused_approval_id": result.paused_approval_id,
    }


@app.get("/agent/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    run = app.state.runtime.get_run(run_id)
    if run is None:
        raise HTTPException(404, detail=f"Run {run_id} not found")
    return {
        "run_id": run.id,
        "task_id": run.task_id,
        "status": str(run.status),
        "started_at": run.started_at.isoformat(),
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "last_step_seq": run.last_step_seq,
        "error": run.error,
    }


@app.get("/agent/runs/{run_id}/audit")
async def verify_audit(run_id: str) -> dict[str, Any]:
    ok, err = app.state.runtime.verify_audit(run_id)
    return {"run_id": run_id, "chain_intact": ok, "error": err}


@app.post("/agent/approvals/{approval_id}/approve")
async def approve(approval_id: str, body: ApprovalAction) -> dict[str, Any]:
    await app.state.runtime.approve(approval_id, approver=body.approver)
    approval = app.state.runtime.store.get_approval(approval_id)
    if approval is None:
        raise HTTPException(404)
    args = json.loads(approval["action"])["args"]
    agent = ScriptedRefundAgent(args["customer_id"], args["amount_usd"], args["reason"])
    result = await app.state.runtime.resume(
        agent=agent, run_id=approval["run_id"], approver=body.approver
    )
    return {
        "run_id": result.run_id,
        "status": str(result.status),
        "final_answer": result.final_answer,
    }


@app.post("/agent/approvals/{approval_id}/deny")
async def deny(approval_id: str, body: ApprovalAction) -> dict[str, Any]:
    await app.state.runtime.deny(approval_id, approver=body.approver, reason=body.reason or "")
    return {"denied": True, "approval_id": approval_id}
