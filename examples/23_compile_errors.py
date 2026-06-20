"""
================================================================
EXAMPLE 23 — "Catching policy errors at compile time" (ADVANCED)
================================================================

SCENARIO:
    Lynx tries hard to fail loudly at compile time rather than silently
    at runtime. Every mistake below would otherwise silently never match.
    All of these raise `PolicyCompileError`
    before the bundle is built — surface them in CI with `lynx policy
    lint` and you cannot deploy a never-matching rule.

WHAT THIS EXAMPLE SHOWS:
    - `PolicyCompileError` for: malformed YAML, unknown verdict, unknown
      operator (with typo suggestion), unknown predicate name (with
      suggestion), invalid transform block, malformed between operand,
      ReDoS regex shape rejection
    - The `lynx policy lint` CLI in the same workflow

RUN WITH:
    python examples/23_compile_errors.py
"""

from __future__ import annotations

from lynx.policy import PolicyCompileError, compile_policy

CASES = [
    (
        "malformed YAML",
        "this is: not valid: yaml: <<<",
    ),
    (
        "unknown verdict",
        """
version: 1
rules:
  - id: r
    match: { tool: shell }
    decision: bogus
""",
    ),
    (
        "unknown operator suffix (typo)",
        """
version: 1
rules:
  - id: r
    match: { tool: shell, args.cmd.matchess: 'rm -rf' }
    decision: deny
""",
    ),
    (
        "unknown predicate name (typo)",
        """
version: 1
predicates:
  is_destructive: { tool: shell }
rules:
  - id: r
    match: is_destructiv
    decision: deny
""",
    ),
    (
        "transform without transform block",
        """
version: 1
rules:
  - id: r
    match: { tool: shell }
    decision: transform
""",
    ),
    (
        "transform block with no op",
        """
version: 1
rules:
  - id: r
    match: { tool: shell }
    decision: transform
    transform:
      jsonpath: "$.args.cmd"
""",
    ),
    (
        "between operand reversed (lo > hi)",
        """
version: 1
rules:
  - id: r
    match: { args.x.between: [10, 5] }
    decision: deny
""",
    ),
    (
        "`in` with non-list RHS",
        """
version: 1
rules:
  - id: r
    match: { tool.in: shell }
    decision: deny
""",
    ),
    (
        "ReDoS-shape regex",
        """
version: 1
rules:
  - id: r
    match: { tool: shell, args.cmd.matches: '(a+)+b' }
    decision: deny
""",
    ),
]


def main() -> None:
    print("Each malformed policy below is caught at compile time:\n")
    for label, yaml_text in CASES:
        try:
            compile_policy(yaml_text)
            print(f"  ✗ {label}: UNEXPECTEDLY ACCEPTED")
        except PolicyCompileError as exc:
            # Trim to first line — the messages are designed to be operator-friendly.
            first_line = str(exc).splitlines()[0][:120]
            print(f"  ✓ {label}")
            print(f"      → {first_line}")
        print()

    print("Same thing from the CLI:")
    print("  lynx policy lint path/to/policy.yaml")
    print()
    print("Returns exit code 0 + a summary on success, exit code 1 + the error")
    print("message on failure. Drop it in CI to keep bad policies out of prod.")


if __name__ == "__main__":
    main()
