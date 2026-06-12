"""Type-level tests: immutability, equality, builders."""

from __future__ import annotations

import pytest

from lynx import (
    Budget,
    Decision,
    Principal,
    ToolMetadata,
    ToolSet,
    Verdict,
    tool,
)


def test_principal_is_frozen() -> None:
    p = Principal(kind="user", id="x")
    with pytest.raises((AttributeError, TypeError)):
        p.kind = "service"  # type: ignore[misc]


def test_budget_default_steps() -> None:
    assert Budget().steps is None  # undefined = unlimited; only set caps enforce


def test_verdict_string_form() -> None:
    assert Verdict.ALLOW.value == "allow"
    assert Verdict.DENY.value == "deny"
    assert Verdict.DRY_RUN.value == "dry_run"
    assert Verdict.APPROVE_REQUIRED.value == "approve_required"
    assert Verdict.TRANSFORM.value == "transform"


def test_decision_is_frozen() -> None:
    d = Decision(verdict=Verdict.ALLOW)
    with pytest.raises((AttributeError, TypeError)):
        d.reason = "modified"  # type: ignore[misc]


def test_toolmetadata_is_frozen() -> None:
    m = ToolMetadata(cost="low", reversible=True, scope=("filesystem:read",))
    with pytest.raises((AttributeError, TypeError)):
        m.reversible = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ToolSet behavior
# ---------------------------------------------------------------------------


@tool(reversible=True, scope=("filesystem:read",))
async def list_dir(path: str = ".") -> list[str]:
    """List a directory."""
    return []


@tool(reversible=False, scope=("filesystem:write",))
async def write_file(path: str, content: str) -> str:
    """Write a file."""
    return ""


def test_toolset_from_functions() -> None:
    ts = ToolSet.from_functions(list_dir, write_file)
    assert ts.names() == ("list_dir", "write_file")
    assert len(ts) == 2


def test_toolset_rejects_undecorated() -> None:
    async def plain_fn() -> int:
        return 0

    with pytest.raises(TypeError, match="not decorated"):
        ToolSet.from_functions(plain_fn)


def test_toolset_with_tool_returns_new_set() -> None:
    ts1 = ToolSet.from_functions(list_dir)
    ts2 = ts1.with_tool(write_file.__lynx_meta__)
    assert ts1.names() == ("list_dir",)
    assert ts2.names() == ("list_dir", "write_file")


def test_toolset_without_tool() -> None:
    ts = ToolSet.from_functions(list_dir, write_file)
    ts2 = ts.without_tool("write_file")
    assert ts2.names() == ("list_dir",)
    assert ts.names() == ("list_dir", "write_file")


def test_toolset_union() -> None:
    a = ToolSet.from_functions(list_dir)
    b = ToolSet.from_functions(write_file)
    c = a.union(b)
    assert c.names() == ("list_dir", "write_file")


def test_toolset_get_missing_raises() -> None:
    ts = ToolSet.from_functions(list_dir)
    with pytest.raises(KeyError):
        ts.get("missing")


def test_toolset_mapping_is_read_only() -> None:
    ts = ToolSet.from_functions(list_dir)
    with pytest.raises(TypeError):
        ts.tools["evil"] = None  # type: ignore[index]


def test_toolset_from_functions_rejects_duplicates() -> None:
    @tool(reversible=True, scope=(), name="dup")
    async def a() -> None:
        return None

    @tool(reversible=True, scope=(), name="dup")
    async def b() -> None:
        return None

    with pytest.raises(ValueError, match="Duplicate"):
        ToolSet.from_functions(a, b)


def test_toolset_with_tool_rejects_duplicate_name() -> None:
    ts = ToolSet.from_functions(list_dir)
    with pytest.raises(ValueError):
        ts.with_tool(list_dir.__lynx_meta__)


def test_toolset_union_rejects_overlap() -> None:
    a = ToolSet.from_functions(list_dir)
    b = ToolSet.from_functions(list_dir)
    with pytest.raises(ValueError, match="collision"):
        a.union(b)


def test_double_tool_decoration_raises() -> None:
    async def f() -> None:
        return None

    tool(reversible=True, scope=())(f)
    with pytest.raises(TypeError, match="already decorated"):
        tool(reversible=False, scope=())(f)


def test_budget_no_legacy_fields() -> None:
    """v2 removed Budget.usd (money never enters the kernel). Budget.tokens
    returned in v2.3 — but enforced this time, against adapter-reported
    Usage counts, alongside input_tokens / output_tokens."""
    b = Budget()
    assert not hasattr(b, "usd")
    assert b.tokens is None and b.input_tokens is None and b.output_tokens is None
