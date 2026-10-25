"""The tool layer and its guardrails, with no model involved."""

from __future__ import annotations

from typing import Any

import pytest

from resolve import approvals, audit
from resolve.db import Database
from resolve.tools import REGISTRY, anthropic_tools, call_tool
from tests.conftest import ctx_for

SEATTLE = {"line1": "400 Pine St", "city": "Seattle", "region": "WA", "postal_code": "98101",
           "country": "us"}  # fmt: skip


def order_row(db: Database, order_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    assert row is not None
    return row


def test_every_tool_schema_is_strict_compatible() -> None:
    def objects(node: Any) -> list[dict[str, Any]]:
        if isinstance(node, dict):
            found = [node] if node.get("type") == "object" else []
            return found + [o for v in node.values() for o in objects(v)]
        if isinstance(node, list):
            return [o for v in node for o in objects(v)]
        return []

    definitions = anthropic_tools()
    assert [d["name"] for d in definitions] == list(REGISTRY)
    for definition in definitions:
        assert definition["strict"] is True
        assert definition["description"]
        for obj in objects(definition["input_schema"]):
            assert obj["additionalProperties"] is False, definition["name"]


def test_lookup_customer_lists_only_own_orders(db: Database) -> None:
    result = call_tool(ctx_for(db), "lookup_customer", {})
    assert result.data["name"] == "Ava Chen"
    assert {o["id"] for o in result.data["orders"]} == {"NW-5001", "NW-5002", "NW-5003", "NW-5004"}


def test_get_order_normalises_id_and_reports_refundable_amount(db: Database) -> None:
    result = call_tool(ctx_for(db), "get_order", {"order_id": " nw-5001 "})
    assert not result.is_error
    assert result.data["refundable_remaining"] == "$144.00"


def test_other_customers_order_looks_like_a_missing_one(db: Database) -> None:
    foreign = call_tool(ctx_for(db), "get_order", {"order_id": "NW-5101"})
    missing = call_tool(ctx_for(db), "get_order", {"order_id": "NW-9999"})
    assert foreign.is_error
    assert missing.is_error
    assert foreign.data["message"].replace("5101", "") == missing.data["message"].replace(
        "9999", ""
    )
    outcomes = [row["outcome"] for row in audit.recent(db)]
    assert outcomes[:2] == ["error", "denied"]  # the cross-tenant attempt is flagged as denied


@pytest.mark.parametrize(
    "tool",
    ["get_order", "track_shipment", "issue_refund", "update_shipping_address"],
)
def test_no_tool_reaches_another_customers_order(db: Database, tool: str) -> None:
    args: dict[str, Any] = {"order_id": "NW-5102"}
    if tool == "issue_refund":
        args |= {"amount": 10, "reason": "test"}
    if tool == "update_shipping_address":
        args["new_address"] = SEATTLE
    result = call_tool(ctx_for(db), tool, args)
    assert result.is_error
    assert result.data["error"] == "order_not_found"
    assert order_row(db, "NW-5102")["refunded_cents"] == 0


def test_customer_note_is_marked_untrusted(db: Database) -> None:
    result = call_tool(ctx_for(db, "C-1002"), "get_order", {"order_id": "NW-5102"})
    assert "pre-approved" in result.data["customer_note"]["untrusted"]


def test_track_shipment(db: Database) -> None:
    shipped = call_tool(ctx_for(db, "C-1002"), "track_shipment", {"order_id": "NW-5101"})
    assert shipped.data["carrier"] == "FedEx"
    assert len(shipped.data["events"]) == 2
    unshipped = call_tool(ctx_for(db), "track_shipment", {"order_id": "NW-5003"})
    assert unshipped.data["status"] == "not shipped (processing)"


@pytest.mark.parametrize("query", ['refund" OR body:*', "NEAR(refund warranty)", "return-policy"])
def test_search_is_safe_against_fts_syntax(db: Database, query: str) -> None:
    result = call_tool(ctx_for(db), "search_help_center", {"query": query})
    assert not result.is_error
    assert all("untrusted" in article for article in result.data["articles"])


@pytest.mark.parametrize(
    ("query", "slug"),
    [
        ("refund", "returns-and-refunds"),
        ("Is there a warranty?", "warranty"),
        ("shoe size", "shoe-sizing"),
    ],
)
def test_search_ranks_the_matching_article_first(db: Database, query: str, slug: str) -> None:
    result = call_tool(ctx_for(db), "search_help_center", {"query": query})
    assert result.data["articles"][0]["slug"] == slug


def test_address_change_before_shipping(db: Database) -> None:
    args = {"order_id": "NW-5003", "new_address": SEATTLE}
    result = call_tool(ctx_for(db), "update_shipping_address", args)
    assert result.data["new_address"] == "400 Pine St, Seattle, WA 98101, US"
    assert order_row(db, "NW-5003")["shipping_address"] == result.data["new_address"]


def test_address_locked_after_shipping(db: Database) -> None:
    args = {"order_id": "NW-5101", "new_address": SEATTLE}
    result = call_tool(ctx_for(db, "C-1002"), "update_shipping_address", args)
    assert result.data["error"] == "address_locked"
    assert "Austin" in order_row(db, "NW-5101")["shipping_address"]


def test_invalid_arguments_are_reported_not_raised(db: Database) -> None:
    result = call_tool(ctx_for(db), "issue_refund", {"order_id": "5001", "amount": -3})
    assert result.is_error
    assert result.data["error"] == "invalid_arguments"
    fields = {d["field"] for d in result.data["details"]}
    assert fields == {"order_id", "amount", "reason"}
    assert audit.recent(db, 1)[0]["outcome"] == "invalid"


def test_unknown_tool(db: Database) -> None:
    assert call_tool(ctx_for(db), "drop_tables", {}).data["error"] == "unknown_tool"


def test_small_refund_is_immediate(db: Database) -> None:
    args = {"order_id": "NW-5002", "amount": 24.0, "reason": "Dented"}
    result = call_tool(ctx_for(db), "issue_refund", args)
    assert result.data["status"] == "refunded"
    assert order_row(db, "NW-5002")["refunded_cents"] == 2400


def test_refund_cannot_exceed_remaining(db: Database) -> None:
    args = {"order_id": "NW-5202", "amount": 120.0, "reason": "Broken"}
    result = call_tool(ctx_for(db, "C-1003"), "issue_refund", args)
    assert result.data["error"] == "exceeds_refundable_amount"


def test_large_refund_waits_for_approval_then_executes_once(db: Database) -> None:
    args = {"order_id": "NW-5001", "amount": 144.0, "reason": "Too small"}
    parked = call_tool(ctx_for(db), "issue_refund", args)
    assert parked.data["status"] == "pending_approval"
    assert parked.approval_id
    assert order_row(db, "NW-5001")["refunded_cents"] == 0

    decided = approvals.decide(db, parked.approval_id, approve=True, operator="jordan")
    assert decided["result"]["data"]["status"] == "refunded"
    assert decided["result"]["data"]["approved_by"] == "jordan"
    assert order_row(db, "NW-5001")["refunded_cents"] == 14400

    with pytest.raises(approvals.ApprovalError, match="already approved"):
        approvals.decide(db, parked.approval_id, approve=True, operator="sam")
    assert order_row(db, "NW-5001")["refunded_cents"] == 14400


def test_rejected_refund_changes_nothing(db: Database) -> None:
    args = {"order_id": "NW-5001", "amount": 144.0, "reason": "Too small"}
    parked = call_tool(ctx_for(db), "issue_refund", args)
    assert parked.approval_id
    decided = approvals.decide(
        db, parked.approval_id, approve=False, operator="jordan", note="Worn"
    )
    assert decided["result"]["data"] == {"status": "rejected_by_specialist", "note": "Worn"}
    assert order_row(db, "NW-5001")["refunded_cents"] == 0
    assert audit.recent(db, 1)[0]["tool"] == "approval_decision"


def test_escalation_creates_ticket(db: Database) -> None:
    args = {"summary": "Stove broke after two uses", "priority": "normal"}
    result = call_tool(ctx_for(db, "C-1003"), "escalate_to_human", args)
    ticket = db.query_one("SELECT * FROM tickets WHERE id = ?", (result.data["ticket_id"],))
    assert ticket is not None
    assert ticket["customer_id"] == "C-1003"
