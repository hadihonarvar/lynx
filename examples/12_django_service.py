"""
================================================================
EXAMPLE 12 — "Lynx inside a Django app" (INTEGRATION)
================================================================

SCENARIO:
    Same as 09/11 but for Django. Single-file self-contained.

REQUIRES:
    pip install django

RUN WITH:
    python examples/12_django_service.py runserver

NOTE:
    The script puts its parent directory on ``sys.path`` before calling
    ``django.setup()`` so the digit-prefixed module name resolves cleanly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Put `examples/` on the path so that the AppConfig's `name =
# "examples.12_django_service"` import resolves regardless of cwd.
_THIS = Path(__file__).resolve()
_EXAMPLES_DIR = _THIS.parent
_PROJECT_ROOT = _EXAMPLES_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    import django
    from django.apps import AppConfig
    from django.conf import settings
    from django.http import JsonResponse
    from django.urls import path
except ImportError as exc:
    raise SystemExit("This example requires django: pip install django") from exc

from lynx import (  # noqa: E402
    FinalAnswer,
    Message,
    ToolCall,
    ToolSet,
    auto_approve,
    callback_sink,
    load_policy_file,
    run_agent,
    tool,
)


@tool(reversible=True, scope=("customer:read",))
async def get_customer(customer_id: str) -> dict:
    return {"id": customer_id, "name": "Alice"}


@tool(reversible=False, scope=("customer:write",))
async def refund_customer(customer_id: str, amount_usd: float, reason: str) -> dict:
    return {"refunded": amount_usd, "to": customer_id}


@refund_customer.shadow
async def _refund_shadow(customer_id: str, amount_usd: float, reason: str) -> dict:
    return {"would_refund": amount_usd}


class ScriptedRefund:
    def __init__(self, customer_id, amount_usd, reason):
        self._i = 0
        self._plan = [
            ToolCall("get_customer", {"customer_id": customer_id}, call_id="c1"),
            ToolCall(
                "refund_customer",
                {"customer_id": customer_id, "amount_usd": amount_usd, "reason": reason},
                call_id="c2",
            ),
            FinalAnswer(text=f"Refund processed for {customer_id}."),
        ]

    async def step(self, conv: tuple[Message, ...]):
        a = self._plan[self._i]
        self._i += 1
        return a


class LynxAppConfig(AppConfig):
    name = "examples.12_django_service"
    # Django derives the app label from the last dotted segment, which here
    # starts with a digit ("12_django_service") — invalid since Django 6.0.
    # Pin an explicit, identifier-safe label.
    label = "lynx_django_demo"
    tools: ToolSet
    policy: Any

    def ready(self) -> None:
        policy_path = _EXAMPLES_DIR / "policies" / "refund.yaml"
        LynxAppConfig.tools = ToolSet.from_functions(get_customer, refund_customer)
        LynxAppConfig.policy = load_policy_file(policy_path)


async def run_endpoint(request) -> JsonResponse:
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        return JsonResponse({"error": f"invalid JSON: {exc}"}, status=400)
    for required in ("customer_id", "amount_usd", "reason"):
        if required not in body:
            return JsonResponse({"error": f"missing field: {required}"}, status=400)

    denials: list[dict[str, Any]] = []

    async def collect(ev):
        if ev.kind == "action.denied":
            denials.append({"seq": ev.seq, "reason": ev.body.get("reason", "")})

    result = await run_agent(
        ScriptedRefund(body["customer_id"], float(body["amount_usd"]), body["reason"]),
        task=f"Refund {body['customer_id']}",
        tools=LynxAppConfig.tools,
        policy=LynxAppConfig.policy,
        sinks=(callback_sink(collect),),
        on_approval=auto_approve(approver="api"),
    )
    payload = {
        "correlation_id": result.correlation_id,
        "final_answer": result.final_answer,
        "error": result.error,
        "steps_taken": result.steps_taken,
        "denials": denials,
    }
    status = 403 if denials else 200
    return JsonResponse(payload, status=status)


urlpatterns = [path("agent/run", run_endpoint)]


# Settings for single-file Django app
SECRET_KEY = "lynx-demo-not-for-production"
DEBUG = True
ROOT_URLCONF = __name__
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = [__name__ + ".LynxAppConfig"]
DATABASES: dict[str, Any] = {}
MIDDLEWARE: list[Any] = []


def main() -> None:
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

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
