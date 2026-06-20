"""Lynx proxies — put the policy kernel *in front of* an existing runtime.

Unlike the framework adapters (which wrap an agent SDK so Lynx drives the loop),
a proxy interposes Lynx on a transport an existing client already speaks. The
client points at Lynx instead of the real backend and gets policy + audit +
approvals + dry-run for free — zero code change on either side.

Currently ships:

* ``mcp_proxy`` — a governing Model Context Protocol server that forwards to an
  upstream MCP server, evaluating every ``call_tool`` against a ``PolicyBundle``
  and streaming ``AuditEvent``\\s to your sinks.
"""
