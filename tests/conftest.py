from __future__ import annotations

from pathlib import Path

import pytest

from resolve.config import Settings
from resolve.db import Database
from resolve.seed import seed
from resolve.tools import ToolContext


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "resolve.db")
    seed(database)
    return database


@pytest.fixture
def settings(db: Database) -> Settings:
    return Settings(
        api_key=None,
        model="claude-opus-5-5",
        effort="medium",
        max_tokens=4000,
        db_path=db.path,
        mcp_customer_id="C-1001",
    )


def ctx_for(db: Database, customer_id: str = "C-1001", approved_by: str | None = None) -> ToolContext:
    return ToolContext(db=db, customer_id=customer_id, actor="agent", approved_by=approved_by)
