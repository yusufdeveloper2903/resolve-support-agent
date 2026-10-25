"""Append-only audit trail: every tool call and every operator decision lands here."""

from __future__ import annotations

from typing import Any

from resolve.db import Database, dumps, utcnow


def record(
    db: Database,
    *,
    actor: str,
    tool: str,
    arguments: dict[str, Any],
    outcome: str,
    detail: dict[str, Any],
    customer_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, customer_id, conversation_id, tool, arguments, "
            "outcome, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                utcnow().isoformat(),
                actor,
                customer_id,
                conversation_id,
                tool,
                dumps(arguments),
                outcome,
                dumps(detail),
            ),
        )


def recent(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
