"""
================================================================
EXAMPLE 12 — "Lynx inside a Django app" (INTEGRATION)
================================================================

GRANDMA-LEVEL PROBLEM:
    Django is the other big Python web framework — it powers a LOT of
    business apps already in production. If you have one of those and want
    to bolt on a safe AI assistant, you DON'T need to migrate. Lynx fits in
    as a small set of views.

    Django 4.1+ supports ASYNC views. With that you can `await runtime.run()`
    directly from a view. For older Django (3.x), use `runtime.run_sync()`
    inside a regular sync view — same as the Flask example.

REAL-WORLD USE CASE:
    Common Django + AI patterns:
      - Internal admin tools where staff trigger AI workflows
      - Customer-facing features (refund self-service, smart search)
      - Webhooks from your CRM that kick off an AI workflow
      - Anything where you already have authentication, ORM, and templates
        and don't want to leave that ecosystem

WHAT THIS EXAMPLE SHOWS:
    - A `LynxAppConfig.ready()` that creates one Runtime singleton on
      startup AND triggers @tool registration
    - Async views (Django 4.1+ pattern) that `await runtime.run(...)`
    - A simple url pattern wiring everything up
    - The same refund workflow as examples 07 / 09 / 11

REQUIRES:
    pip install django

RUN WITH:
    # This file is a self-contained Django-app shim. Save and run:
    DJANGO_SETTINGS_MODULE=examples.12_django_service \\
        python -m django runserver

    # In another terminal:
    curl -X POST localhost:8000/agent/run \\
        -H 'content-type: application/json' \\
        -d '{"customer_id": "C-123", "amount_usd": 5, "reason": "1-day outage"}'

WHY IT LOOKS A BIT LONGER:
    A normal Django project spans many files (settings, apps, views, urls).
    This example squeezes them into ONE file so you can read it top-to-bottom.
    For a real project, lift the pieces into their normal locations.

WHAT YOU'LL SEE:
    JSON responses with run_id, status, final_answer, and (if paused)
    paused_approval_id.  Identical to examples 09 (FastAPI) and 11 (Flask).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import django
    from django.apps import AppConfig
    from django.conf import settings
    from django.http import JsonResponse
    from django.urls import path
except ImportError as exc:
    raise SystemExit("This example requires django: pip install django") from exc

from lynx import FinalAnswer, Message, Runtime, ToolCall, tool
from lynx.core.mediator import get_registry
from lynx.policy import load_policy_file
from lynx.stores.sqlite import SQLiteStore


# ---------------------------------------------------------------------------
# Tools — module-level registration
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
# Agent (same scripted shape as previous examples)
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
# Django app + Runtime singleton
# ---------------------------------------------------------------------------


class LynxAppConfig(AppConfig):
    name = "examples.12_django_service"
    runtime: Runtime  # populated in ready()

    def ready(self) -> None:
        policy_path = Path(__file__).resolve().parent / "policies" / "refund.yaml"
        LynxAppConfig.runtime = Runtime(
            store=SQLiteStore(Path(__file__).resolve().parent.parent / ".lynx" / "django.db"),
            policy=load_policy_file(policy_path),
        )


# ---------------------------------------------------------------------------
# Views (async — requires Django 4.1+; for older Django use runtime.run_sync)
# ---------------------------------------------------------------------------


async def run_agent(request) -> JsonResponse:
    body = json.loads(request.body or b"{}")
    runtime = LynxAppConfig.runtime
    agent = ScriptedRefundAgent(body["customer_id"], float(body["amount_usd"]), body["reason"])
    result = await runtime.run(
        agent=agent,
        task=f"Refund {body['customer_id']} ${body['amount_usd']:.2f}",
        principal={"kind": "service", "id": "django-demo"},
        environment="prod",
    )
    return JsonResponse(
        {
            "run_id": result.run_id,
            "status": str(result.status),
            "final_answer": result.final_answer,
            "paused_approval_id": result.paused_approval_id,
        }
    )


async def get_run(request, run_id: str) -> JsonResponse:
    runtime = LynxAppConfig.runtime
    run = runtime.get_run(run_id)
    if run is None:
        return JsonResponse({"error": f"Run {run_id} not found"}, status=404)
    return JsonResponse(
        {
            "run_id": run.id,
            "status": str(run.status),
            "started_at": run.started_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        }
    )


async def approve(request, approval_id: str) -> JsonResponse:
    body = json.loads(request.body or b"{}")
    runtime = LynxAppConfig.runtime
    approver = body.get("approver", "anonymous")
    await runtime.approve(approval_id, approver=approver)
    approval = runtime.store.get_approval(approval_id)
    if approval is None:
        return JsonResponse({"error": "approval not found"}, status=404)
    args = json.loads(approval["action"])["args"]
    agent = ScriptedRefundAgent(args["customer_id"], args["amount_usd"], args["reason"])
    result = await runtime.resume(agent=agent, run_id=approval["run_id"], approver=approver)
    return JsonResponse(
        {
            "run_id": result.run_id,
            "status": str(result.status),
            "final_answer": result.final_answer,
        }
    )


# ---------------------------------------------------------------------------
# URL routes — at module level so this file works as DJANGO_SETTINGS_MODULE
# ---------------------------------------------------------------------------


urlpatterns = [
    path("agent/run", run_agent),
    path("agent/runs/<str:run_id>", get_run),
    path("agent/approvals/<str:approval_id>/approve", approve),
]


# ---------------------------------------------------------------------------
# Minimal Django settings — bundled into this file so the example is
# runnable as a single-file Django app.
# ---------------------------------------------------------------------------


DEBUG = True
SECRET_KEY = "lynx-demo-not-for-production"  # noqa: S105
ROOT_URLCONF = __name__
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = [__name__ + ".LynxAppConfig"]
DATABASES: dict[str, Any] = {}
MIDDLEWARE: list[Any] = []


def main() -> None:
    """Allow running directly: `python examples/12_django_service.py runserver`"""
    settings.configure(
        DEBUG=DEBUG,
        SECRET_KEY=SECRET_KEY,
        ROOT_URLCONF=__name__,
        ALLOWED_HOSTS=ALLOWED_HOSTS,
        INSTALLED_APPS=[__name__ + ".LynxAppConfig"],
        DATABASES={},
        MIDDLEWARE=[],
    )
    django.setup()
    from django.core.management import execute_from_command_line
    import sys

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
