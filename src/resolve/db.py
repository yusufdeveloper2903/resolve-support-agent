"""SQLite access: one schema, short-lived connections, JSON helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, tier TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL CHECK (status IN ('processing', 'shipped', 'delivered', 'cancelled')),
    placed_at TEXT NOT NULL,
    total_cents INTEGER NOT NULL,
    refunded_cents INTEGER NOT NULL DEFAULT 0,
    shipping_address TEXT NOT NULL,
    customer_note TEXT
);
CREATE TABLE IF NOT EXISTS order_items (
    order_id TEXT NOT NULL REFERENCES orders(id),
    sku TEXT NOT NULL REFERENCES products(sku),
    quantity INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS shipments (
    order_id TEXT PRIMARY KEY REFERENCES orders(id),
    carrier TEXT NOT NULL,
    tracking_number TEXT NOT NULL,
    status TEXT NOT NULL,
    eta TEXT,
    events TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_articles (
    id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL, body TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
    title, body, content='kb_articles', content_rowid='id'
);
CREATE TABLE IF NOT EXISTS refunds (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    amount_cents INTEGER NOT NULL,
    reason TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    conversation_id TEXT,
    priority TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'awaiting_approval')),
    pending_results TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    customer_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    result TEXT,
    decided_by TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    customer_id TEXT,
    conversation_id TEXT,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def dumps(value: Any) -> str:
    """Deterministic JSON (sorted keys) so stored and replayed payloads are byte-stable."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection whose statements commit together, or roll back on error."""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None
