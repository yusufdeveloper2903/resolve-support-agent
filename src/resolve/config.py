"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# `.env` in the project, then a shared key file one level up (local dev convenience).
ENV_FILES = (PROJECT_ROOT / ".env", PROJECT_ROOT.parent / ".anthropic.env")

Effort = Literal["low", "medium", "high", "xhigh", "max"]


def load_env_files(paths: tuple[Path, ...] = ENV_FILES) -> None:
    """Load KEY=VALUE files without overriding variables that are already set."""
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    model: str
    effort: Effort
    max_tokens: int
    db_path: Path
    mcp_customer_id: str

    @classmethod
    def from_env(cls) -> Settings:
        load_env_files()
        effort = os.environ.get("CLAUDE_EFFORT", "medium")
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ValueError(f"CLAUDE_EFFORT must be low|medium|high|xhigh|max, got {effort!r}")
        return cls(
            api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
            effort=cast(Effort, effort),
            max_tokens=int(os.environ.get("CLAUDE_MAX_TOKENS", "16000")),
            db_path=Path(os.environ.get("RESOLVE_DB_PATH", PROJECT_ROOT / "data" / "resolve.db")),
            mcp_customer_id=os.environ.get("RESOLVE_MCP_CUSTOMER_ID", "C-1001"),
        )
