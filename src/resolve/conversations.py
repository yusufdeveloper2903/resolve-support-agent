"""Conversation storage. History is append-only: messages are replayed to the API byte-for-byte,
which keeps the prompt cache warm and thinking blocks valid."""

from __future__ import annotations

import json
from typing import Any

from resolve.db import Database, dumps, new_id, utcnow

Message = dict[str, Any]

_INSERT_MESSAGE = (
    "INSERT INTO messages (conversation_id, seq, role, content) VALUES "
    "(?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE conversation_id = ?), ?, ?)"
)


def create(db: Database, customer_id: str) -> dict[str, Any]:
    conversation = {
        "id": new_id("CV"),
        "customer_id": customer_id,
        "status": "active",
        "created_at": utcnow().isoformat(),
    }
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO conversations (id, customer_id, status, created_at) VALUES (?, ?, ?, ?)",
            tuple(conversation.values()),
        )
    return conversation


def get(db: Database, conversation_id: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT id, customer_id, status, created_at FROM conversations WHERE id = ?",
        (conversation_id,),
    )


def history(db: Database, conversation_id: str) -> list[Message]:
    rows = db.query(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY seq",
        (conversation_id,),
    )
    return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]


def append(db: Database, conversation_id: str, role: str, content: list[dict[str, Any]]) -> None:
    with db.connect() as conn:
        conn.execute(_INSERT_MESSAGE, (conversation_id, conversation_id, role, dumps(content)))


def park(db: Database, conversation_id: str, results: list[dict[str, Any]]) -> None:
    """Hold a turn's tool results until every approval in it is decided."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE conversations SET status = 'awaiting_approval', pending_results = ? "
            "WHERE id = ?",
            (dumps(results), conversation_id),
        )


def parked_results(db: Database, conversation_id: str) -> list[dict[str, Any]]:
    row = db.query_one("SELECT pending_results FROM conversations WHERE id = ?", (conversation_id,))
    return json.loads(row["pending_results"]) if row and row["pending_results"] else []


def unpark(db: Database, conversation_id: str, results: list[dict[str, Any]]) -> None:
    """Append the completed tool results and reopen the conversation, atomically."""
    with db.connect() as conn:
        conn.execute(_INSERT_MESSAGE, (conversation_id, conversation_id, "user", dumps(results)))
        conn.execute(
            "UPDATE conversations SET status = 'active', pending_results = NULL WHERE id = ?",
            (conversation_id,),
        )


def transcript(messages: list[Message]) -> list[dict[str, Any]]:
    """A UI-friendly view: user/assistant text plus one entry per tool call with its outcome."""
    outcomes: dict[str, str] = {}
    for message in messages:
        for block in message["content"]:
            if block["type"] == "tool_result":
                data = json.loads(block["content"])
                outcomes[block["tool_use_id"]] = (
                    "error" if block.get("is_error") else str(data.get("status", "ok"))
                )
    entries: list[dict[str, Any]] = []
    for message in messages:
        for block in message["content"]:
            if block["type"] == "text" and block["text"].strip():
                entries.append({"kind": message["role"], "text": block["text"]})
            elif block["type"] == "tool_use":
                entries.append(
                    {
                        "kind": "tool",
                        "id": block["id"],
                        "name": block["name"],
                        "input": block["input"],
                        "outcome": outcomes.get(block["id"], "pending_approval"),
                    }
                )
    return entries
