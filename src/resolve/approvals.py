"""Human-in-the-loop: operators approve or reject tool calls that were parked by policy."""

from __future__ import annotations

import json
from typing import Any

from resolve import audit
from resolve.db import Database, dumps, utcnow
from resolve.tools import ToolContext, call_tool


class ApprovalError(Exception):
    pass


def list_approvals(db: Database, status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM approvals"
    rows = (
        db.query(f"{sql} WHERE status = ? ORDER BY created_at", (status,))
        if status
        else db.query(f"{sql} ORDER BY created_at DESC")
    )
    return [_decode(row) for row in rows]


def get_approval(db: Database, approval_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    return _decode(row) if row else None


def decide(
    db: Database, approval_id: str, *, approve: bool, operator: str, note: str | None = None
) -> dict[str, Any]:
    """Record the decision. Approval re-runs the original call, so policy is checked again."""
    approval = get_approval(db, approval_id)
    if approval is None:
        raise ApprovalError(f"Unknown approval {approval_id}")
    status = "approved" if approve else "rejected"
    # Claim the row first so two operators can never execute the same action twice.
    with db.connect() as conn:
        claimed = conn.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, note = ?, decided_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (status, operator, note, utcnow().isoformat(), approval_id),
        ).rowcount
    if not claimed:
        raise ApprovalError(f"Approval {approval_id} is already {approval['status']}")

    if approve:
        ctx = ToolContext(
            db=db,
            customer_id=approval["customer_id"],
            actor="operator",
            conversation_id=approval["conversation_id"],
            approved_by=operator,
        )
        executed = call_tool(ctx, approval["tool_name"], approval["arguments"])
        result = {"is_error": executed.is_error, "data": executed.data}
    else:
        rejection = {"status": "rejected_by_specialist", "note": note or "No reason given."}
        result = {"is_error": False, "data": rejection}
    with db.connect() as conn:
        conn.execute("UPDATE approvals SET result = ? WHERE id = ?", (dumps(result), approval_id))
    audit.record(
        db,
        actor="operator",
        tool="approval_decision",
        arguments={"approval_id": approval_id, "decision": status, "note": note},
        outcome=status,
        detail=result,
        customer_id=approval["customer_id"],
        conversation_id=approval["conversation_id"],
    )
    return {**approval, "status": status, "result": result, "decided_by": operator, "note": note}


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "arguments": json.loads(row["arguments"]),
        "result": json.loads(row["result"]) if row["result"] else None,
    }
