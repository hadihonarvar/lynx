"""Lynx — a stateless, type-safe policy kernel for AI agent tool calls.

The single source of truth for the package version is ``__version__`` below;
``pyproject.toml`` reads it dynamically (see ``[tool.hatch.version]``).
"""

from lynx.approvals import (
    ApprovalHandler,
    auto_approve,
    auto_deny,
    callback_approval,
    cli_prompt_approval,
)
from lynx.core.scheduler import run_agent
from lynx.core.types import (
    ActionRequest,
    ActionResult,
    ApprovalDecision,
    ApprovalRequest,
    AuditEvent,
    Budget,
    Decision,
    ExecutionContext,
    FinalAnswer,
    Message,
    Principal,
    RunResult,
    ToolCall,
    ToolDef,
    ToolMetadata,
    ToolSet,
    Verdict,
)
from lynx.decorators import shadow, tool
from lynx.durability import (
    DuplicateRecord,
    RunStore,
    RunView,
    StepRecord,
    StepView,
    idempotency_key,
    replay,
    step_record_from_json,
    step_record_to_json,
)
from lynx.policy import (
    PolicyBundle,
    allow,
    approve_required,
    compile_policy,
    deny,
    dry_run,
    load_policy_file,
    transform,
)
from lynx.sdk import Agent, AgentAction
from lynx.sinks import (
    Sink,
    callback_sink,
    jsonl_sink,
    multi_sink,
    noop_sink,
    stdout_sink,
)

__version__ = "2.1.0"

__all__ = [
    "ActionRequest",
    "ActionResult",
    "Agent",
    "AgentAction",
    "ApprovalDecision",
    "ApprovalHandler",
    "ApprovalRequest",
    "AuditEvent",
    "Budget",
    "Decision",
    "DuplicateRecord",
    "ExecutionContext",
    "FinalAnswer",
    "Message",
    "PolicyBundle",
    "Principal",
    "RunResult",
    "RunStore",
    "RunView",
    "Sink",
    "StepRecord",
    "StepView",
    "ToolCall",
    "ToolDef",
    "ToolMetadata",
    "ToolSet",
    "Verdict",
    "__version__",
    "allow",
    "approve_required",
    "auto_approve",
    "auto_deny",
    "callback_approval",
    "callback_sink",
    "cli_prompt_approval",
    "compile_policy",
    "deny",
    "dry_run",
    "idempotency_key",
    "jsonl_sink",
    "load_policy_file",
    "multi_sink",
    "noop_sink",
    "replay",
    "run_agent",
    "shadow",
    "stdout_sink",
    "step_record_from_json",
    "step_record_to_json",
    "tool",
    "transform",
]
