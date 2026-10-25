"""The agent loop against a scripted model: sequencing, approval pause/resume, failure paths."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anthropic
import httpx2
import pytest

from resolve import approvals, conversations
from resolve.agent import Agent, AgentEvent, ConversationStateError, ModelStream
from resolve.db import Database
from resolve.replay import ReplayModel, Turn, text, tools


def make_agent(db: Database, turns: list[Turn]) -> tuple[Agent, ReplayModel]:
    model = ReplayModel(turns)
    return Agent(db, model, model_name="claude-opus-5-5", effort="medium", max_tokens=4000), model


async def collect(events: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in events]


def reply(events: list[AgentEvent]) -> str:
    return "".join(e.data["delta"] for e in events if e.type == "text")


def last_user_blocks(db: Database, conversation_id: str) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = conversations.history(db, conversation_id)[-1]["content"]
    return content


async def test_plain_answer_and_request_shape(db: Database) -> None:
    agent, model = make_agent(db, [text("Hi Ava! How can I help?")])
    conv = conversations.create(db, "C-1001")
    events = await collect(agent.chat(conv["id"], "hello"))

    assert reply(events) == "Hi Ava! How can I help?"
    assert events[-1] == AgentEvent("done", {"stop_reason": "end_turn"})
    params = model.requests[0]
    assert params["model"] == "claude-opus-5-5"
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "medium"}
    assert params["tool_choice"] == {"type": "auto"}
    assert params["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert params["cache_control"] == {"type": "ephemeral"}
    assert all(tool["strict"] for tool in params["tools"])


async def test_parallel_tool_results_return_in_one_user_message(db: Database) -> None:
    agent, model = make_agent(
        db,
        [
            tools(("get_order", {"order_id": "NW-5001"}), ("get_order", {"order_id": "NW-5002"})),
            text("Both orders were delivered."),
        ],
    )
    conv = conversations.create(db, "C-1001")
    events = await collect(agent.chat(conv["id"], "status of 5001 and 5002?"))

    assert [e.type for e in events].count("tool_call") == 2
    second_request = model.requests[1]["messages"]
    assert [m["role"] for m in second_request] == ["user", "assistant", "user"]
    results = second_request[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_01_0", "toolu_01_1"]
    assert all(r["type"] == "tool_result" and not r["is_error"] for r in results)


async def test_tool_errors_are_returned_as_is_error(db: Database) -> None:
    agent, _ = make_agent(
        db, [tools(("get_order", {"order_id": "NW-5101"})), text("I can't find that order.")]
    )
    conv = conversations.create(db, "C-1001")
    events = await collect(agent.chat(conv["id"], "Where is NW-5101?"))

    result = conversations.history(db, conv["id"])[2]["content"][0]
    assert result["is_error"] is True
    assert json.loads(result["content"])["error"] == "order_not_found"
    assert any(e.type == "tool_result" and e.data["status"] == "error" for e in events)


async def test_approval_pauses_the_turn_and_resume_continues_it(db: Database) -> None:
    refund = {"order_id": "NW-5001", "amount": 144.0, "reason": "Too small"}
    agent, model = make_agent(
        db,
        [
            tools(("issue_refund", refund), ("track_shipment", {"order_id": "NW-5001"})),
            text("Your $144.00 refund is approved and on its way."),
        ],
    )
    conv = conversations.create(db, "C-1001")
    events = await collect(agent.chat(conv["id"], "Refund NW-5001 please"))

    assert events[-1].type == "approval_required"
    (approval_id,) = events[-1].data["approval_ids"]
    assert conversations.get(db, conv["id"])["status"] == "awaiting_approval"  # type: ignore[index]
    assert len(model.requests) == 1  # the loop stopped: no tool_result was sent yet

    with pytest.raises(ConversationStateError):
        agent.check_can_chat(conv["id"])
    with pytest.raises(ConversationStateError, match="still pending"):
        agent.check_can_resume(conv["id"])

    approvals.decide(db, approval_id, approve=True, operator="jordan")
    resumed = await collect(agent.resume(conv["id"]))

    assert resumed[0] == AgentEvent("tool_result", {"id": "toolu_01_0", "status": "refunded"})
    assert reply(resumed) == "Your $144.00 refund is approved and on its way."
    results = model.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_01_0", "toolu_01_1"]  # original order
    assert json.loads(results[0]["content"])["status"] == "refunded"
    assert conversations.get(db, conv["id"])["status"] == "active"  # type: ignore[index]


async def test_rejected_approval_reaches_the_model(db: Database) -> None:
    refund = {"order_id": "NW-5001", "amount": 144.0, "reason": "Too small"}
    agent, model = make_agent(db, [tools(("issue_refund", refund)), text("Sorry, declined.")])
    conv = conversations.create(db, "C-1001")
    events = await collect(agent.chat(conv["id"], "Refund NW-5001 please"))
    approvals.decide(db, events[-1].data["approval_ids"][0], approve=False, operator="jo")
    await collect(agent.resume(conv["id"]))

    result = model.requests[1]["messages"][-1]["content"][0]
    assert json.loads(result["content"])["status"] == "rejected_by_specialist"


async def test_refusal_never_runs_tools_or_enters_history(db: Database) -> None:
    refused: Turn = {
        "content": [{"type": "tool_use", "name": "issue_refund", "input": {}}],
        "stop_reason": "refusal",
        "stop_details": {"type": "refusal", "category": None, "explanation": None},
    }
    agent, _ = make_agent(db, [refused])
    conv = conversations.create(db, "C-1001")
    events = await collect(agent.chat(conv["id"], "..."))

    assert "can't help" in reply(events)
    assert events[-1].data["stop_reason"] == "refusal"
    assert not any(e.type == "tool_call" for e in events)
    assert [m["role"] for m in conversations.history(db, conv["id"])] == ["user"]


async def test_truncated_tool_call_is_not_executed(db: Database) -> None:
    cut: Turn = {**tools(("issue_refund", {"order_id": "NW-5002"})), "stop_reason": "max_tokens"}
    agent, _ = make_agent(db, [cut])
    conv = conversations.create(db, "C-1001")
    events = await collect(agent.chat(conv["id"], "refund"))
    assert events[-1].type == "error"
    assert db.query("SELECT * FROM audit_log") == []


class FailingModel:
    @asynccontextmanager
    async def stream(self, params: dict[str, Any]) -> AsyncIterator[ModelStream]:
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx2.Response(429, request=request)
        raise anthropic.RateLimitError("slow down", response=response, body=None)
        yield  # pragma: no cover


async def test_api_errors_become_friendly_error_events(db: Database) -> None:
    agent = Agent(db, FailingModel(), model_name="m", effort="low", max_tokens=100)
    conv = conversations.create(db, "C-1001")
    events = await collect(agent.chat(conv["id"], "hi"))
    assert events == [AgentEvent("error", {"message": events[0].data["message"]})]
    assert "busy" in events[0].data["message"]


async def test_runaway_tool_loops_are_capped(db: Database) -> None:
    loop = [tools(("lookup_customer", {}))] * 12
    agent, _ = make_agent(db, loop)
    conv = conversations.create(db, "C-1001")
    events = await collect(agent.chat(conv["id"], "hi"))
    assert events[-1] == AgentEvent("error", {"message": "Stopped after too many tool steps."})


def test_unknown_conversation(db: Database) -> None:
    agent, _ = make_agent(db, [])
    with pytest.raises(LookupError):
        agent.check_can_chat("CV-missing")
