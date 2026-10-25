"""HTTP layer: SSE chat, tenancy on conversations, the approval flow end to end."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from resolve.api import create_app
from resolve.config import Settings
from resolve.replay import ReplayModel, Turn, text, tools

AVA = {"X-Customer-Id": "C-1001"}


def client_with(settings: Settings, turns: list[Turn] | None = None) -> TestClient:
    return TestClient(create_app(settings, ReplayModel(turns) if turns is not None else None))


def sse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for chunk in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in chunk.splitlines() if ": " in line)
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


def start(client: TestClient) -> str:
    response = client.post("/api/conversations", json={"customer_id": "C-1001"})
    assert response.status_code == 201
    conversation_id: str = response.json()["id"]
    return conversation_id


def test_ui_and_health_without_api_key(settings: Settings) -> None:
    client = client_with(settings)
    assert "Resolve" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/health").json()["agent_online"] is False
    assert len(client.get("/api/customers").json()) == 3
    conversation_id = start(client)
    response = client.post(f"/api/conversations/{conversation_id}/messages", json={"text": "hi"},
                           headers=AVA)  # fmt: skip
    assert response.status_code == 503


def test_chat_streams_text_and_tool_events(settings: Settings) -> None:
    client = client_with(
        settings, [tools(("get_order", {"order_id": "NW-5002"})), text("It was delivered.")]
    )
    conversation_id = start(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "NW-5002?"}, headers=AVA
    )
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    assert [e for e, _ in events] == ["tool_call", "tool_result", "text", "done"]
    assert events[2][1] == {"delta": "It was delivered."}

    transcript = client.get(f"/api/conversations/{conversation_id}", headers=AVA).json()[
        "transcript"
    ]
    assert [entry["kind"] for entry in transcript] == ["user", "tool", "assistant"]
    assert transcript[1]["outcome"] == "ok"


def test_conversations_are_private_to_their_customer(settings: Settings) -> None:
    client = client_with(settings, [])
    conversation_id = start(client)
    other = {"X-Customer-Id": "C-1002"}
    assert client.get(f"/api/conversations/{conversation_id}", headers=other).status_code == 404
    response = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=other
    )
    assert response.status_code == 404
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 422  # no identity


def test_approval_flow_over_http(settings: Settings) -> None:
    refund = {"order_id": "NW-5001", "amount": 144.0, "reason": "Too small"}
    client = client_with(settings, [tools(("issue_refund", refund)), text("Refund approved.")])
    conversation_id = start(client)
    url = f"/api/conversations/{conversation_id}"
    events = sse_events(client.post(f"{url}/messages", json={"text": "refund"}, headers=AVA).text)
    assert events[-1][0] == "approval_required"

    state = client.get(url, headers=AVA).json()
    assert state["status"] == "awaiting_approval"
    assert not state["ready_to_resume"]
    assert client.post(f"{url}/messages", json={"text": "hello?"}, headers=AVA).status_code == 409
    assert client.post(f"{url}/resume", headers=AVA).status_code == 409

    (pending,) = client.get("/api/approvals", params={"status": "pending"}).json()
    decision = {"approve": True, "operator": "jordan"}
    decided = client.post(f"/api/approvals/{pending['id']}/decision", json=decision)
    assert decided.json()["result"]["data"]["status"] == "refunded"
    again = client.post(f"/api/approvals/{pending['id']}/decision", json=decision)
    assert again.status_code == 409

    assert client.get(url, headers=AVA).json()["ready_to_resume"] is True
    resumed = sse_events(client.post(f"{url}/resume", headers=AVA).text)
    assert ("text", {"delta": "Refund approved."}) in resumed
    assert client.get(url, headers=AVA).json()["status"] == "active"

    trail = [(row["actor"], row["tool"], row["outcome"]) for row in client.get("/api/audit").json()]
    assert trail == [  # newest first
        ("operator", "approval_decision", "approved"),
        ("operator", "issue_refund", "ok"),
        ("agent", "issue_refund", "pending_approval"),
    ]


def test_validation_and_reset(settings: Settings) -> None:
    client = client_with(settings, [])
    conversation_id = start(client)
    url = f"/api/conversations/{conversation_id}/messages"
    assert client.post(url, json={"text": ""}, headers=AVA).status_code == 422
    assert client.post("/api/conversations", json={"customer_id": "C-404"}).status_code == 404
    assert client.post("/api/reset").status_code == 204
    assert client.get(f"/api/conversations/{conversation_id}", headers=AVA).status_code == 404
