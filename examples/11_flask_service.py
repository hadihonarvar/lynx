"""
================================================================
EXAMPLE 11 — "Lynx behind a Flask web service" (INTEGRATION)
================================================================

GRANDMA-LEVEL PROBLEM:
    A lot of people already have Flask apps — it's the most popular
    Python web framework. If you have a Flask service and want to add
    a safe AI assistant to it, you don't need to switch frameworks.
    Lynx drops in.

    The difference between this and the FastAPI example: Flask is
    SYNCHRONOUS. So instead of `await runtime.run(...)` you use
    `runtime.run_sync(...)` — same job, different syntax.

REAL-WORLD USE CASE:
    Adding an AI feature to an existing Flask app:
      - An admin panel that lets staff trigger AI tasks
      - A simple internal tool ("ask the bot to summarize this")
      - A webhook receiver that fires AI workflows
      - Anything where you already have Flask routes and don't want
        to bring in async machinery

WHAT THIS EXAMPLE SHOWS:
    - One Runtime singleton attached to the Flask app
    - A sync `@app.route` that calls `runtime.run_sync(...)`
    - The same refund workflow as example 07 / 09, but synchronous
    - Approval handled via a sync endpoint

REQUIRES:
    pip install flask

RUN WITH:
    flask --app examples.11_flask_service run --debug

    # then in another terminal:
    curl -X POST localhost:5000/agent/run \\
        -H 'content-type: application/json' \\
        -d '{"customer_id": "C-123", "amount_usd": 5, "reason": "1-day outage"}'

WHAT YOU'LL SEE:
    JSON responses with run_id, status, final_answer, and (if paused)
    paused_approval_id.  Lynx's machinery runs synchronously behind Flask.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

try:
    from flask import Flask, jsonify, request
except ImportError as exc:
    raise SystemExit("This example requires flask: pip install flask") from exc

from lynx import FinalAnswer, Message, Runtime, ToolCall, tool
from lynx.core.mediator import get_registry
from lynx.policy import load_policy_file
from lynx.stores.sqlite import SQLiteStore

# ---------------------------------------------------------------------------
# Tools (same as the FastAPI example)
# ---------------------------------------------------------------------------


@tool(cost="low", reversible=True, scope=["customer:read"])
async def get_customer(customer_id: str) -> dict:
    fake_db = {
        "C-123": {"name": "Alice", "plan": "Pro"},
        "C-456": {"name": "Bob", "plan": "Team"},
        "C-789": {"name": "Carol", "plan": "Pro"},
    }
    return fake_db.get(customer_id, {"error": "not found"})


@tool(cost="medium", reversible=False, scope=["customer:write", "money:transfer"])
async def refund_customer(customer_id: str, amount_usd: float, reason: str) -> dict:
    return {"refunded": amount_usd, "to": customer_id, "reason": reason, "txn": "TXN-XYZ"}


@refund_customer.shadow
async def _refund_shadow(customer_id: str, amount_usd: float, reason: str) -> dict:
    return {"would_refund": amount_usd, "to": customer_id}


# ---------------------------------------------------------------------------
# Agent (same scripted shape as the FastAPI example)
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
# Flask app + Runtime singleton
# ---------------------------------------------------------------------------


def create_app() -> Flask:
    app = Flask(__name__)

    policy_path = Path(__file__).resolve().parent / "policies" / "refund.yaml"
    app.runtime = Runtime(  # type: ignore[attr-defined]
        store=SQLiteStore(Path(__file__).resolve().parent.parent / ".lynx" / "flask.db"),
        policy=load_policy_file(policy_path),
    )
    return app


app = create_app()


# ---------------------------------------------------------------------------
# Endpoints — synchronous, using runtime.run_sync()
# ---------------------------------------------------------------------------


@app.route("/")
def root() -> Any:
    return jsonify(
        {
            "service": "Lynx Flask demo",
            "endpoints": [
                "POST /agent/run                                  start a run (sync)",
                "GET  /agent/runs/<run_id>                        inspect a run",
                "POST /agent/approvals/<approval_id>/approve      approve + resume",
            ],
            "tools_registered": get_registry().names(),
        }
    )


@app.route("/agent/run", methods=["POST"])
def run_agent() -> Any:
    body = request.get_json() or {}
    agent = ScriptedRefundAgent(body["customer_id"], float(body["amount_usd"]), body["reason"])
    # Flask is sync — use run_sync to bridge to Lynx's async runtime.
    result = app.runtime.run_sync(  # type: ignore[attr-defined]
        agent=agent,
        task=f"Refund {body['customer_id']} ${body['amount_usd']:.2f}",
        principal={"kind": "service", "id": "flask-demo"},
        environment="prod",
    )
    return jsonify(
        {
            "run_id": result.run_id,
            "status": str(result.status),
            "final_answer": result.final_answer,
            "paused_approval_id": result.paused_approval_id,
        }
    )


@app.route("/agent/runs/<run_id>")
def get_run(run_id: str) -> Any:
    run = app.runtime.get_run(run_id)  # type: ignore[attr-defined]
    if run is None:
        return jsonify({"error": f"Run {run_id} not found"}), 404
    return jsonify(
        {
            "run_id": run.id,
            "status": str(run.status),
            "started_at": run.started_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            "last_step_seq": run.last_step_seq,
            "error": run.error,
        }
    )


@app.route("/agent/approvals/<approval_id>/approve", methods=["POST"])
def approve(approval_id: str) -> Any:
    body = request.get_json() or {}
    approver = body.get("approver", "anonymous")
    # Sync-call the async approve + resume.
    asyncio.run(app.runtime.approve(approval_id, approver=approver))  # type: ignore[attr-defined]
    approval = app.runtime.store.get_approval(approval_id)  # type: ignore[attr-defined]
    if approval is None:
        return jsonify({"error": "approval not found"}), 404
    args = json.loads(approval["action"])["args"]
    agent = ScriptedRefundAgent(args["customer_id"], args["amount_usd"], args["reason"])
    result = asyncio.run(
        app.runtime.resume(agent=agent, run_id=approval["run_id"], approver=approver)  # type: ignore[attr-defined]
    )
    return jsonify(
        {
            "run_id": result.run_id,
            "status": str(result.status),
            "final_answer": result.final_answer,
        }
    )
