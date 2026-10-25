"""The MCP server exposes exactly the registry, scoped to one customer."""

from __future__ import annotations

import json

from resolve import audit
from resolve.db import Database
from resolve.mcp_server import build_server
from resolve.tools import REGISTRY


async def test_lists_the_shared_tools(db: Database) -> None:
    listed = {tool.name: tool for tool in await build_server(db, "C-1001").list_tools()}
    assert list(listed) == list(REGISTRY)
    assert listed["issue_refund"].description == REGISTRY["issue_refund"].description
    assert listed["get_order"].annotations.read_only_hint is True  # type: ignore[union-attr]
    address = listed["update_shipping_address"].input_schema
    assert address["required"] == ["order_id", "new_address"]


async def test_calls_go_through_the_same_guardrails(db: Database) -> None:
    server = build_server(db, "C-1001")
    own = await server.call_tool("get_order", {"order_id": "NW-5002"})
    foreign = await server.call_tool("get_order", {"order_id": "NW-5101"})
    assert not own.is_error  # type: ignore[union-attr]
    assert foreign.is_error  # type: ignore[union-attr]
    assert json.loads(foreign.content[0].text)["error"] == "order_not_found"  # type: ignore[union-attr]
    assert {row["actor"] for row in audit.recent(db)} == {"mcp"}


async def test_large_refund_still_needs_an_operator(db: Database) -> None:
    server = build_server(db, "C-1001")
    args = {"order_id": "NW-5001", "amount": 144.0, "reason": "Too small"}
    result = await server.call_tool("issue_refund", args)
    assert json.loads(result.content[0].text)["status"] == "pending_approval"  # type: ignore[union-attr]
