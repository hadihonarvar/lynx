"""Lynx MCP proxy — govern any MCP server with zero code change.

An MCP client (Claude Desktop, Claude Code, Cursor, …) is configured to launch
*this* process instead of the real MCP server. The proxy:

  1. starts the real ("upstream") MCP server as a child and discovers its tools,
  2. re-exports those tools verbatim (names + JSON schemas) to the client,
  3. routes every ``call_tool`` through the Lynx kernel — ``evaluate`` returns a
     ``Decision``; ``mediate`` dispatches by verdict (allow / deny / dry_run /
     approve_required / transform) — *before* the call reaches upstream,
  4. streams an ``AuditEvent`` for every decision and result to your sinks.

The result: policy-gated, audited, human-approvable MCP tools the user already
has, without touching the agent or the server.

```text
   MCP client ──stdio──▶  Lynx proxy (this)  ──stdio──▶  upstream MCP server
                              │  evaluate → mediate
                              └─▶ sinks (audit)
```

The governance core (`govern_call`) is transport-free and unit-testable: give
it a policy, a ToolSet, and a callable that reaches upstream. `serve_mcp_proxy`
adds the MCP server/client transport on top.

Requires ``pip install lynx-agent[mcp]`` (or ``pip install mcp``). This is a
Phase-1 prototype (see docs/roadmap.md) — the governance path is faithful to the
scheduler; the transport surface is intentionally small.
"""

from __future__ import annotations

import shlex
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from lynx.core.mediator import mediate
from lynx.core.policy import PolicyBundle, evaluate
from lynx.core.types import (
    ActionRequest,
    ActionResult,
    AuditEvent,
    ExecutionContext,
    Principal,
    ToolDef,
    ToolMetadata,
    ToolSet,
    now_utc,
)

__all__ = [
    "GovernedProxy",
    "ToolClassifier",
    "default_classify",
    "govern_call",
    "serve_mcp_proxy",
]

# An upstream caller: given (tool_name, args) actually run the tool upstream and
# return its raw result value. The proxy never calls upstream except through one
# of these, so allow/deny is the only thing standing between the model and the
# real side effect.
UpstreamCaller = Callable[[str, Mapping[str, Any]], Awaitable[Any]]

# A sink is any async callable taking one AuditEvent (matches lynx.sinks.Sink).
Sink = Callable[[AuditEvent], Awaitable[None]]


# ---------------------------------------------------------------------------
# Tool classification — how an upstream tool maps to Lynx metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolClassifier:
    """Maps an upstream tool name → the ``ToolMetadata`` policy matches on.

    Upstream MCP servers don't tell us whether a tool is reversible or what its
    blast radius is, so the proxy must assume. The default posture is
    conservative — irreversible, ``mcp:tool`` scope — so operator policy has to
    *opt tools in*. Every tool also gets a per-tool scope tag ``mcp:<name>`` so
    a policy can target one tool without inventing predicates.
    """

    cost: str = "medium"
    reversible: bool = False
    scope: tuple[str, ...] = ("mcp:tool",)
    # Optional per-name overrides, e.g. {"read_file": ToolMetadata(reversible=True, ...)}.
    overrides: Mapping[str, ToolMetadata] = field(default_factory=lambda: MappingProxyType({}))

    def __call__(self, name: str) -> ToolMetadata:
        if name in self.overrides:
            return self.overrides[name]
        return ToolMetadata(
            cost=self.cost,  # type: ignore[arg-type]
            reversible=self.reversible,
            scope=(*self.scope, f"mcp:{name}"),
            has_shadow=True,  # the proxy attaches a generic preview shadow
        )


default_classify = ToolClassifier()


# ---------------------------------------------------------------------------
# Governance core — transport-free, unit-testable
# ---------------------------------------------------------------------------


def _preview_shadow(name: str) -> Callable[..., Awaitable[dict[str, Any]]]:
    """A pure shadow so `dry_run` works out of the box: previews the call."""

    async def _shadow(**kwargs: Any) -> dict[str, Any]:
        return {"would_call": name, "args": kwargs}

    _shadow.__qualname__ = f"mcp_proxy_shadow[{name}]"
    return _shadow


def build_toolset(
    names: Sequence[str],
    upstream: UpstreamCaller,
    classify: ToolClassifier = default_classify,
) -> ToolSet:
    """Build a ToolSet whose ``fn`` forwards to upstream, with metadata + shadow.

    The ToolDef.fn is what the mediator/executor ultimately runs on ``allow`` /
    ``transform`` / approved actions — and it is the *only* path to upstream.
    """
    defs: dict[str, ToolDef] = {}
    for name in names:

        def make_fn(bound: str) -> Callable[..., Awaitable[Any]]:
            async def _fn(**kwargs: Any) -> Any:
                return await upstream(bound, kwargs)

            _fn.__qualname__ = f"mcp_proxy_tool[{bound}]"
            return _fn

        defs[name] = ToolDef(
            name=name,
            description=f"MCP tool (proxied): {name}",
            fn=make_fn(name),
            shadow_fn=_preview_shadow(name),
            metadata=classify(name),
        )
    return ToolSet(tools=MappingProxyType(defs))


@dataclass(frozen=True, slots=True)
class GovernResult:
    """What `govern_call` returns: the action outcome + the decision verdict."""

    result: ActionResult
    verdict: str


class GovernedProxy:
    """Holds the per-session governance state shared by every proxied call.

    One instance per client connection. Owns the monotonic audit ``seq``, the
    correlation id, the policy bundle, the ToolSet, and the sinks. Stateless in
    spirit — it accumulates nothing about the *content* of calls, only the
    sequence counter the audit stream needs.
    """

    def __init__(
        self,
        *,
        policy: PolicyBundle,
        tools: ToolSet,
        sinks: Sequence[Sink] = (),
        on_approval: Any | None = None,
        principal: Principal = Principal(kind="agent", id="mcp-client"),
        environment: str = "dev",
        workspace: str = ".",
        correlation_id: str | None = None,
        executor: Any | None = None,
    ) -> None:
        self.policy = policy
        self.tools = tools
        self.sinks = tuple(sinks)
        self.environment = environment
        self.workspace = workspace
        self.principal = principal
        self.executor = executor
        self._cid = correlation_id or f"mcp-{uuid.uuid4().hex[:12]}"
        self._seq = 0
        self._step = 0
        if on_approval is None:
            from lynx.approvals import auto_deny

            on_approval = auto_deny("no approval handler configured for MCP proxy")
        self.on_approval = on_approval

    @property
    def correlation_id(self) -> str:
        return self._cid

    async def _emit(self, kind: str, body: dict[str, Any]) -> None:
        event = AuditEvent(
            correlation_id=self._cid,
            bundle_id=self.policy.id,
            seq=self._seq,
            kind=kind,
            timestamp=now_utc(),
            body=MappingProxyType(dict(body)),
        )
        self._seq += 1
        for sink in self.sinks:
            try:
                await sink(event)
            except Exception:  # a broken sink never breaks proxying
                pass

    def _context(self) -> ExecutionContext:
        self._step += 1
        return ExecutionContext(
            principal=self.principal,
            environment=self.environment,
            workspace=self.workspace,
            correlation_id=self._cid,
            step_seq=self._step,
            timestamp=now_utc(),
        )

    async def call(self, name: str, args: Mapping[str, Any]) -> GovernResult:
        """Govern one tool call. Mirrors the scheduler's evaluate→emit→mediate."""
        return await govern_call(self, name, args)


async def govern_call(
    proxy: GovernedProxy,
    name: str,
    args: Mapping[str, Any],
) -> GovernResult:
    """Route a single MCP ``call_tool`` through the Lynx kernel.

    Faithful to ``core/scheduler.py``: build the request, ``evaluate`` to a
    decision, emit ``policy.evaluated``, emit ``action.started`` /
    ``action.dry_run`` (and ``approval.requested`` when relevant), ``mediate``,
    then emit ``action.completed`` / ``action.failed``.
    """
    try:
        tool = proxy.tools.get(name)
    except KeyError:
        await proxy._emit("action.failed", {"tool": name, "reason": "unknown tool"})
        return GovernResult(
            result=ActionResult(ok=False, error=f"unknown tool: {name!r}"),
            verdict="deny",
        )

    request = ActionRequest(
        tool=name,
        args=MappingProxyType(dict(args)),
        declared=tool.metadata,
        context=proxy._context(),
    )

    decision = evaluate(proxy.policy, request, request.context)
    verdict = decision.verdict.value
    await proxy._emit(
        "policy.evaluated",
        {
            "tool": name,
            "verdict": verdict,
            "reason": decision.reason,
            "matched_rules": list(decision.matched_rules),
        },
    )

    action_kind = "action.dry_run" if verdict == "dry_run" else "action.started"
    await proxy._emit(action_kind, {"tool": name, "verdict": verdict})
    if verdict == "approve_required":
        await proxy._emit(
            "approval.requested", {"tool": name, "approvers": list(decision.approvers)}
        )

    result = await mediate(request, decision, proxy.tools, proxy.on_approval, proxy.executor)

    # Same outcome ladder as core/scheduler.py, so the audit vocabulary is
    # identical whether a call goes through run_agent or the proxy:
    #   ok + dry_run            → action.dry_run_completed
    #   ok                      → action.completed
    #   fail + deny/approve_req → action.denied   (a policy refusal, not a crash)
    #   fail                    → action.failed
    if verdict == "approve_required":
        await proxy._emit(
            "approval.granted" if result.ok else "approval.denied",
            {"tool": name, "ok": result.ok, "error": result.error},
        )

    if result.ok:
        outcome_kind = "action.dry_run_completed" if verdict == "dry_run" else "action.completed"
        await proxy._emit(
            outcome_kind,
            {"tool": name, "verdict": verdict, "duration_ms": result.duration_ms},
        )
    else:
        outcome_kind = (
            "action.denied" if verdict in ("deny", "approve_required") else "action.failed"
        )
        await proxy._emit(outcome_kind, {"tool": name, "verdict": verdict, "reason": result.error})
    return GovernResult(result=result, verdict=verdict)


# ---------------------------------------------------------------------------
# MCP transport — wire the governance core to real stdio MCP server/client
# ---------------------------------------------------------------------------


def _result_to_text(result: ActionResult) -> str:
    """Render an ActionResult for return to the MCP client as text content."""
    if result.ok:
        val = result.value
        return val if isinstance(val, str) else repr(val)
    return f"[lynx denied] {result.error}"


async def serve_mcp_proxy(
    upstream_command: str | list[str],
    *,
    policy: PolicyBundle,
    sinks: Sequence[Sink] = (),
    on_approval: Any | None = None,
    classify: ToolClassifier = default_classify,
    principal: Principal = Principal(kind="agent", id="mcp-client"),
    environment: str = "dev",
    workspace: str = ".",
    server_name: str = "lynx-mcp-proxy",
) -> None:
    """Run the governing proxy: serve MCP downstream, forward MCP upstream.

    Blocks until the downstream client disconnects. The upstream server runs as
    a child process for the proxy's lifetime.
    """
    try:
        import mcp.types as mcp_types
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server
    except ImportError as exc:  # pragma: no cover - exercised only without mcp
        raise ImportError(
            "serve_mcp_proxy requires the 'mcp' package. Install: pip install mcp"
        ) from exc

    if isinstance(upstream_command, list):
        if not upstream_command:
            raise ValueError("serve_mcp_proxy: command list cannot be empty")
        up = StdioServerParameters(command=upstream_command[0], args=upstream_command[1:])
    else:
        parts = shlex.split(upstream_command)
        if not parts:
            raise ValueError("serve_mcp_proxy: command string cannot be empty")
        up = StdioServerParameters(command=parts[0], args=parts[1:])

    async with stdio_client(up) as (u_read, u_write):
        async with ClientSession(u_read, u_write) as upstream:
            await upstream.initialize()
            listed = await upstream.list_tools()
            upstream_tools = list(listed.tools)
            names = [t.name for t in upstream_tools]

            async def call_upstream(tool: str, args: Mapping[str, Any]) -> Any:
                res = await upstream.call_tool(tool, arguments=dict(args))
                return res.content

            tools = build_toolset(names, call_upstream, classify)
            proxy = GovernedProxy(
                policy=policy,
                tools=tools,
                sinks=sinks,
                on_approval=on_approval,
                principal=principal,
                environment=environment,
                workspace=workspace,
            )

            server = Server(server_name)

            # The mcp lowlevel server's decorators are untyped; ignore is local.
            @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
            async def _list() -> list[Any]:
                # Re-export upstream tool descriptors verbatim (schemas intact).
                return upstream_tools

            @server.call_tool()  # type: ignore[untyped-decorator]
            async def _call(name: str, arguments: dict[str, Any] | None) -> list[Any]:
                governed = await proxy.call(name, arguments or {})
                text = _result_to_text(governed.result)
                return [mcp_types.TextContent(type="text", text=text)]

            async with stdio_server() as (d_read, d_write):
                await server.run(d_read, d_write, server.create_initialization_options())
