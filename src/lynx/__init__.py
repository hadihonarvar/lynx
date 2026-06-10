"""Lynx — framework-agnostic policy-gated durable execution for AI agents.

The single source of truth for the package version is ``__version__`` below;
``pyproject.toml`` reads it dynamically (see ``[tool.hatch.version]``).
"""

from lynx.core.types import (
    ActionRequest,
    ActionResult,
    AuditEvent,
    Budget,
    Decision,
    ExecutionContext,
    ModelCall,
    Principal,
    Run,
    RunStatus,
    Step,
    Task,
    ToolMetadata,
    Verdict,
)
from lynx.decorators import shadow, tool
from lynx.policy import allow, approve_required, deny, dry_run, rule, transform
from lynx.runtime import Runtime, runtime
from lynx.sdk import AgentAction, FinalAnswer, Message, ToolCall

__version__ = "1.0.0"

__all__ = [
    "ActionRequest",
    "ActionResult",
    "AgentAction",
    "AuditEvent",
    "Budget",
    "Decision",
    "ExecutionContext",
    "FinalAnswer",
    "Message",
    "ModelCall",
    "Principal",
    "Run",
    "RunStatus",
    "Runtime",
    "Step",
    "Task",
    "ToolCall",
    "ToolMetadata",
    "Verdict",
    "__version__",
    "allow",
    "approve_required",
    "deny",
    "dry_run",
    "rule",
    "runtime",
    "shadow",
    "tool",
    "transform",
]
