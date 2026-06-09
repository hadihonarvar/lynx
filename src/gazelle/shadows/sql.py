"""Shadow for SQL execution.

Without a live DB connection, this can only do structural analysis:
identify the operation (SELECT/INSERT/UPDATE/DELETE/DROP), the affected
tables, and whether a WHERE clause is present.

If `conn` is provided, runs EXPLAIN against the real DB for the plan.
"""

from __future__ import annotations

import re
from typing import Any

_OP_PATTERN = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE)\b",
    re.IGNORECASE,
)
_TABLE_PATTERN = re.compile(
    r"\b(?:FROM|INTO|UPDATE|TABLE)\s+([A-Za-z_][A-Za-z0-9_\.]*)",
    re.IGNORECASE,
)
_WHERE_PATTERN = re.compile(r"\bWHERE\b", re.IGNORECASE)
_LIMIT_PATTERN = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)


async def sql_shadow(query: str, conn: Any = None) -> dict[str, Any]:
    op_match = _OP_PATTERN.match(query)
    operation = op_match.group(1).upper() if op_match else "UNKNOWN"
    tables = sorted(set(_TABLE_PATTERN.findall(query)))
    has_where = bool(_WHERE_PATTERN.search(query))
    limit_match = _LIMIT_PATTERN.search(query)
    limit = int(limit_match.group(1)) if limit_match else None
    destructive = operation in {"DELETE", "DROP", "TRUNCATE", "UPDATE"}

    out: dict[str, Any] = {
        "operation": operation,
        "tables": tables,
        "has_where_clause": has_where,
        "limit": limit,
        "destructive": destructive,
        "would_run": query,
        "note": "no real execution — sql_shadow analysis only",
    }
    if destructive and not has_where:
        out["warning"] = f"{operation} without WHERE clause — would affect ALL rows in {tables}"

    if conn is not None:
        try:
            cur = conn.cursor()
            cur.execute(f"EXPLAIN {query}")
            out["explain"] = cur.fetchall()
        except Exception as exc:
            out["explain_error"] = str(exc)

    return out
