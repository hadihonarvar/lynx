"""Proxy entrypoint for the live integration test.

Runs `serve_mcp_proxy` in front of tests/mcp_live_upstream.py with a policy
that allows reads, dry-runs writes, and denies deletes. Launched as a child
process by tests/test_mcp_proxy_live.py; an MCP client connects to THIS over
stdio, and this connects to the upstream server over stdio.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from lynx.policy import compile_policy
from lynx.proxy.mcp_proxy import serve_mcp_proxy

POLICY = """
version: 1
defaults:
  on_no_match: deny
  on_missing_shadow: approve_required
rules:
  - id: allow-reads
    priority: 10
    match: { tool: read_file }
    decision: allow
  - id: preview-writes
    priority: 10
    match: { tool: write_file }
    decision: dry_run
  - id: block-deletes
    priority: 10
    match: { tool: delete_file }
    decision: deny
    reason: "deletes are blocked by the proxy policy"
"""

_UPSTREAM = Path(__file__).with_name("mcp_live_upstream.py")


def main() -> None:
    workdir = sys.argv[1]
    upstream = [sys.executable, str(_UPSTREAM), workdir]
    asyncio.run(serve_mcp_proxy(upstream, policy=compile_policy(POLICY)))


if __name__ == "__main__":
    main()
