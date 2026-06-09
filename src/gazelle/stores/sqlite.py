"""SQLite-backed step journal + hash-chained audit log.

Schema lives in docs/01-data-model.md. WAL mode enabled for concurrent reads.

For MVP we keep journal and audit in the same database file; they can be
split later by swapping in another backend.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from gazelle.core.types import (
    GENESIS_HASH,
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
    canonical_json,
    now_utc,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    goal            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    policy_bundle_id TEXT NOT NULL,
    budget          TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    status          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    resume_token    TEXT,
    last_step_seq   INTEGER NOT NULL DEFAULT -1,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id);

CREATE TABLE IF NOT EXISTS steps (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id),
    seq             INTEGER NOT NULL,
    model_call      TEXT,
    action          TEXT,
    decision        TEXT,
    result          TEXT,
    checkpoint_blob BLOB NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT NOT NULL,
    UNIQUE (run_id, seq)
);
CREATE INDEX IF NOT EXISTS steps_run ON steps(run_id, seq);

CREATE TABLE IF NOT EXISTS audit_events (
    id              TEXT PRIMARY KEY,
    prev            TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    body            TEXT NOT NULL,
    signature       BLOB
);
CREATE INDEX IF NOT EXISTS audit_events_run ON audit_events(run_id, seq);

CREATE TABLE IF NOT EXISTS approval_requests (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    step_seq        INTEGER NOT NULL,
    action          TEXT NOT NULL,
    decision        TEXT NOT NULL,
    status          TEXT NOT NULL,
    approvers       TEXT NOT NULL,
    granted_by      TEXT,
    resolved_at     TEXT,
    expires_at      TEXT
);
"""


def _ser(obj: Any) -> str | None:
    if obj is None:
        return None
    return canonical_json(obj)


def _de(s: str | None) -> Any:
    if s is None:
        return None
    return json.loads(s)


# ---------------------------------------------------------------------------


class SQLiteStore:
    """Single-file store covering Tasks, Runs, Steps, Audit, Approvals.

    Thread-safe via a connection-per-call pattern with WAL mode.
    """

    def __init__(self, path: str | Path = ".gazelle/state.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -----------------------------------------------------------------------
    # Tasks
    # -----------------------------------------------------------------------

    def save_task(self, task: Task) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO tasks
                   (id, goal, created_at, created_by, policy_bundle_id, budget, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.id,
                    task.goal,
                    task.created_at.isoformat(),
                    _ser(asdict(task.created_by)),
                    task.policy_bundle_id,
                    _ser(asdict(task.budget)),
                    _ser(task.metadata),
                ),
            )

    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return Task(
            id=row["id"],
            goal=row["goal"],
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=Principal(**_de(row["created_by"])),
            policy_bundle_id=row["policy_bundle_id"],
            budget=Budget(**_de(row["budget"])),
            metadata=_de(row["metadata"]) or {},
        )

    # -----------------------------------------------------------------------
    # Runs
    # -----------------------------------------------------------------------

    def save_run(self, run: Run) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO runs
                   (id, task_id, status, started_at, ended_at, resume_token,
                    last_step_seq, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.id,
                    run.task_id,
                    run.status.value,
                    run.started_at.isoformat(),
                    run.ended_at.isoformat() if run.ended_at else None,
                    run.resume_token,
                    run.last_step_seq,
                    run.error,
                ),
            )

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return Run(
            id=row["id"],
            task_id=row["task_id"],
            status=RunStatus(row["status"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
            resume_token=row["resume_token"],
            last_step_seq=row["last_step_seq"],
            error=row["error"],
        )

    def list_runs(self, limit: int = 50) -> list[Run]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self.get_run(r["id"]) for r in rows]  # type: ignore[misc]

    # -----------------------------------------------------------------------
    # Steps
    # -----------------------------------------------------------------------

    def save_step(self, step: Step) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO steps
                   (id, run_id, seq, model_call, action, decision, result,
                    checkpoint_blob, started_at, ended_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    step.id,
                    step.run_id,
                    step.seq,
                    _ser(asdict(step.model_call)) if step.model_call else None,
                    _ser(_serialize_action(step.action)) if step.action else None,
                    _ser(asdict(step.decision)) if step.decision else None,
                    _ser(asdict(step.result)) if step.result else None,
                    step.checkpoint_blob,
                    step.started_at.isoformat(),
                    step.ended_at.isoformat(),
                ),
            )

    def get_steps(self, run_id: str) -> list[Step]:
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY seq ASC", (run_id,)
        ).fetchall()
        return [self._row_to_step(r) for r in rows]

    def get_step(self, run_id: str, seq: int) -> Step | None:
        row = self._conn.execute(
            "SELECT * FROM steps WHERE run_id = ? AND seq = ?", (run_id, seq)
        ).fetchone()
        return self._row_to_step(row) if row else None

    def _row_to_step(self, row: sqlite3.Row) -> Step:
        action_data = _de(row["action"])
        return Step(
            id=row["id"],
            run_id=row["run_id"],
            seq=row["seq"],
            model_call=ModelCall(**_de(row["model_call"])) if row["model_call"] else None,
            action=_deserialize_action(action_data) if action_data else None,
            decision=_deserialize_decision(_de(row["decision"])) if row["decision"] else None,
            result=ActionResult(**_de(row["result"])) if row["result"] else None,
            checkpoint_blob=row["checkpoint_blob"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]),
        )

    # -----------------------------------------------------------------------
    # Audit
    # -----------------------------------------------------------------------

    def append_audit(self, event: AuditEvent) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO audit_events
                   (id, prev, run_id, seq, kind, timestamp, body, signature)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id,
                    event.prev,
                    event.run_id,
                    event.seq,
                    event.kind,
                    event.timestamp.isoformat(),
                    _ser(event.body),
                    event.signature,
                ),
            )

    def latest_audit_hash(self, run_id: str) -> str:
        row = self._conn.execute(
            "SELECT id FROM audit_events WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return row["id"] if row else GENESIS_HASH

    def audit_chain(self, run_id: str) -> Iterator[AuditEvent]:
        rows = self._conn.execute(
            "SELECT * FROM audit_events WHERE run_id = ? ORDER BY seq ASC",
            (run_id,),
        )
        for row in rows:
            yield AuditEvent(
                id=row["id"],
                prev=row["prev"],
                run_id=row["run_id"],
                seq=row["seq"],
                kind=row["kind"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                body=_de(row["body"]) or {},
                signature=row["signature"],
            )

    def verify_audit_chain(self, run_id: str) -> tuple[bool, str | None]:
        """Walk the chain and check each event's hash and prev pointer."""
        import hashlib

        prev = GENESIS_HASH
        expected_seq = 0
        for event in self.audit_chain(run_id):
            if event.prev != prev:
                return False, f"prev mismatch at seq {event.seq}"
            if event.seq != expected_seq:
                return False, f"seq jump at {event.seq} (expected {expected_seq})"
            recomputed = hashlib.sha256(
                canonical_json(
                    {
                        "prev": event.prev,
                        "run_id": event.run_id,
                        "seq": event.seq,
                        "kind": event.kind,
                        "timestamp": event.timestamp.isoformat(),
                        "body": event.body,
                    }
                ).encode()
            ).hexdigest()
            if recomputed != event.id:
                return False, f"hash mismatch at seq {event.seq}"
            prev = event.id
            expected_seq += 1
        return True, None

    # -----------------------------------------------------------------------
    # Approvals
    # -----------------------------------------------------------------------

    def save_approval(self, approval: Any) -> None:
        from gazelle.core.mediator import ApprovalRequest

        a: ApprovalRequest = approval
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO approval_requests
                   (id, run_id, step_seq, action, decision, status, approvers,
                    granted_by, resolved_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    a.id,
                    a.run_id,
                    a.step_seq,
                    _ser(_serialize_action(a.action)),
                    _ser(asdict(a.decision)),
                    a.status,
                    _ser(list(a.decision.approvers)),
                    a.granted_by,
                    now_utc().isoformat() if a.status != "pending" else None,
                    None,
                ),
            )

    def list_pending_approvals(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM approval_requests WHERE status = 'pending' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM approval_requests WHERE id = ?", (approval_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_completed_step_by_idempotency_key(self, run_id: str, key: str) -> Step | None:
        """Idempotency check: has an action with this key already succeeded?

        Used by the scheduler to avoid double-executing actions that completed
        before a crash but whose result was not yet flushed to the caller.
        """
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY seq ASC", (run_id,)
        ).fetchall()
        for row in rows:
            if not row["action"]:
                continue
            action_data = _de(row["action"])
            if not action_data or action_data.get("idempotency_key") != key:
                continue
            if not row["result"]:
                continue
            result = _de(row["result"])
            if result and result.get("ok"):
                return self._row_to_step(row)
        return None


# ---------------------------------------------------------------------------
# Serialization helpers (ActionRequest is nested; needs custom logic)
# ---------------------------------------------------------------------------


def _serialize_action(action: ActionRequest) -> dict[str, Any]:
    return {
        "tool": action.tool,
        "args": action.args,
        "declared": asdict(action.declared),
        "context": {
            "principal": asdict(action.context.principal),
            "environment": action.context.environment,
            "workspace": action.context.workspace,
            "run_id": action.context.run_id,
            "step_seq": action.context.step_seq,
            "timestamp": action.context.timestamp.isoformat(),
            "extra": action.context.extra,
        },
        "idempotency_key": action.idempotency_key,
    }


def _deserialize_action(data: dict[str, Any]) -> ActionRequest:
    decl = data["declared"]
    declared = ToolMetadata(
        cost=decl["cost"],
        reversible=decl["reversible"],
        scope=tuple(decl["scope"]),
        blast_radius_hint=decl.get("blast_radius_hint"),
        has_shadow=decl.get("has_shadow", False),
    )
    ctx = data["context"]
    context = ExecutionContext(
        principal=Principal(**ctx["principal"]),
        environment=ctx["environment"],
        workspace=ctx["workspace"],
        run_id=ctx["run_id"],
        step_seq=ctx["step_seq"],
        timestamp=datetime.fromisoformat(ctx["timestamp"]),
        extra=ctx.get("extra", {}),
    )
    return ActionRequest(
        tool=data["tool"],
        args=data["args"],
        declared=declared,
        context=context,
        idempotency_key=data["idempotency_key"],
    )


def _deserialize_decision(data: dict[str, Any]) -> Decision:
    return Decision(
        verdict=Verdict(data["verdict"]),
        reason=data.get("reason", ""),
        matched_rules=tuple(data.get("matched_rules", ())),
        approvers=tuple(data.get("approvers", ())),
        transform_args=data.get("transform_args"),
        timeout_seconds=data.get("timeout_seconds"),
    )
