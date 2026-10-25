"""The agent loop: stream a Claude turn, run the requested tools, repeat.

A hand-written loop rather than the SDK tool runner, because a turn can be *parked*: when a
tool call needs human approval, the loop stops with the assistant's `tool_use` unanswered and
the other results saved. Hours later (another request, maybe another process) `resume()` adds
the operator's outcome as the matching `tool_result` and continues. The runner keeps history
in memory and cannot be resumed from storage, so it does not fit that shape.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
from anthropic.types import Message

from resolve import approvals, conversations
from resolve.config import Effort
from resolve.db import Database, dumps
from resolve.prompts import SYSTEM_PROMPT
from resolve.tools import ToolContext, anthropic_tools, call_tool, outcome_label

log = logging.getLogger(__name__)

MAX_STEPS = 12
REFUSAL_REPLY = "Sorry, I can't help with that request. Is there anything else I can do?"


class ModelStream(Protocol):
    text_stream: AsyncIterator[str]

    async def get_final_message(self) -> Message: ...


class ModelClient(Protocol):
    def stream(self, params: dict[str, Any]) -> AbstractAsyncContextManager[ModelStream]: ...


class AnthropicModel:
    """Production client: a thin seam over `AsyncAnthropic.messages.stream`."""

    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        self._client = client

    def stream(self, params: dict[str, Any]) -> AbstractAsyncContextManager[ModelStream]:
        return self._client.messages.stream(**params)


@dataclass(frozen=True)
class AgentEvent:
    type: str  # text | tool_call | tool_result | approval_required | done | error
    data: dict[str, Any] = field(default_factory=dict)


class ConversationStateError(Exception):
    """The conversation cannot take this action right now (HTTP 409)."""


class Agent:
    def __init__(
        self, db: Database, model: ModelClient, *, model_name: str, effort: Effort, max_tokens: int
    ) -> None:
        self.db = db
        self._model = model
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Everything before `messages` is constant, so it is cached once for all conversations.
        self._base_params: dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_tokens,
            "system": [
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            "tools": anthropic_tools(),
            "tool_choice": {"type": "auto"},  # forced tool choice is a 400 on Opus 5.5
            "thinking": {"type": "adaptive"},  # always on for Opus 5.5; effort sets the depth
            "output_config": {"effort": effort},
            "cache_control": {"type": "ephemeral"},  # automatic breakpoint on the growing tail
        }

    def check_can_chat(self, conversation_id: str) -> dict[str, Any]:
        """Pre-flight for the API, so a 404/409 is returned before streaming starts."""
        conversation = self._load(conversation_id, "active")
        if self._locks[conversation_id].locked():
            raise ConversationStateError("A reply is already being generated.")
        return conversation

    def check_can_resume(self, conversation_id: str) -> dict[str, Any]:
        conversation = self._load(conversation_id, "awaiting_approval")
        if self._undecided(conversation_id):
            raise ConversationStateError("An approval is still pending.")
        return conversation

    async def chat(self, conversation_id: str, text: str) -> AsyncIterator[AgentEvent]:
        async with self._locks[conversation_id]:
            conversation = self._load(conversation_id, "active")
            conversations.append(self.db, conversation_id, "user", [{"type": "text", "text": text}])
            async for event in self._run(conversation):
                yield event

    async def resume(self, conversation_id: str) -> AsyncIterator[AgentEvent]:
        """Continue a parked turn once every approval in it has been decided."""
        async with self._locks[conversation_id]:
            conversation = self.check_can_resume(conversation_id)
            results = []
            for entry in conversations.parked_results(self.db, conversation_id):
                if "approval_id" in entry:
                    approval = approvals.get_approval(self.db, entry["approval_id"])
                    assert approval is not None  # guaranteed by check_can_resume under the lock
                    assert approval["result"] is not None
                    outcome = approval["result"]
                    entry = _tool_result(entry["tool_use_id"], outcome["data"], outcome["is_error"])
                    status = outcome_label(outcome["data"], outcome["is_error"])
                    yield AgentEvent("tool_result", {"id": entry["tool_use_id"], "status": status})
                results.append(entry)
            conversations.unpark(self.db, conversation_id, results)
            async for event in self._run(conversation):
                yield event

    async def _run(self, conversation: dict[str, Any]) -> AsyncIterator[AgentEvent]:
        ctx = ToolContext(
            db=self.db,
            customer_id=conversation["customer_id"],
            actor="agent",
            conversation_id=conversation["id"],
        )
        for _ in range(MAX_STEPS):
            params = {
                **self._base_params,
                "messages": conversations.history(self.db, conversation["id"]),
            }
            try:
                async with self._model.stream(params) as stream:
                    async for delta in stream.text_stream:
                        yield AgentEvent("text", {"delta": delta})
                    message = await stream.get_final_message()
            except anthropic.APIError as exc:
                log.exception("Claude API call failed")
                yield AgentEvent("error", {"message": describe_api_error(exc)})
                return
            _log_usage(message)

            tool_uses = [b for b in message.content if b.type == "tool_use"]
            if message.stop_reason == "refusal":
                # A refusal can cut a tool_use off mid-input: never run it, and keep the turn
                # out of history so the next request does not carry an unanswered tool_use.
                category = message.stop_details.category if message.stop_details else None
                yield AgentEvent(
                    "text", {"delta": ("\n\n" if message.content else "") + REFUSAL_REPLY}
                )
                yield AgentEvent("done", {"stop_reason": "refusal", "category": category})
                return
            if message.stop_reason == "max_tokens" and tool_uses:
                yield AgentEvent("error", {"message": "The reply was cut off. Please try again."})
                return

            conversations.append(
                self.db,
                conversation["id"],
                "assistant",
                [block.to_dict(mode="json") for block in message.content],
            )
            if message.stop_reason == "pause_turn":
                continue
            if not tool_uses:
                yield AgentEvent("done", {"stop_reason": message.stop_reason})
                return

            results: list[dict[str, Any]] = []
            parked: list[dict[str, Any]] = []
            for block in tool_uses:
                yield AgentEvent(
                    "tool_call", {"id": block.id, "name": block.name, "input": block.input}
                )
                result = await asyncio.to_thread(call_tool, ctx, block.name, block.input)
                status = outcome_label(result.data, result.is_error)
                yield AgentEvent(
                    "tool_result", {"id": block.id, "name": block.name, "status": status}
                )
                if result.approval_id:
                    parked.append({"tool_use_id": block.id, "approval_id": result.approval_id})
                    results.append(parked[-1])
                else:
                    results.append(_tool_result(block.id, result.data, result.is_error))

            if parked:
                conversations.park(self.db, conversation["id"], results)
                yield AgentEvent(
                    "approval_required", {"approval_ids": [p["approval_id"] for p in parked]}
                )
                return
            # All results of a parallel batch go back together, in a single user message.
            conversations.append(self.db, conversation["id"], "user", results)

        yield AgentEvent("error", {"message": "Stopped after too many tool steps."})

    def _load(self, conversation_id: str, required_status: str) -> dict[str, Any]:
        conversation = conversations.get(self.db, conversation_id)
        if conversation is None:
            raise LookupError(conversation_id)
        if conversation["status"] != required_status:
            raise ConversationStateError(
                "Waiting for a specialist to review a pending action."
                if conversation["status"] == "awaiting_approval"
                else "Nothing is waiting for approval."
            )
        return conversation

    def _undecided(self, conversation_id: str) -> bool:
        for entry in conversations.parked_results(self.db, conversation_id):
            if "approval_id" in entry:
                approval = approvals.get_approval(self.db, entry["approval_id"])
                if approval is None or approval["result"] is None:
                    return True
        return False


def _tool_result(tool_use_id: str, data: dict[str, Any], is_error: bool) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": dumps(data),
        "is_error": is_error,
    }


def _log_usage(message: Message) -> None:
    usage = message.usage
    log.info(
        "claude turn stop=%s in=%s cache_read=%s cache_write=%s out=%s request_id=%s",
        message.stop_reason,
        usage.input_tokens,
        usage.cache_read_input_tokens,
        usage.cache_creation_input_tokens,
        usage.output_tokens,
        getattr(message, "_request_id", None),
    )


def describe_api_error(exc: anthropic.APIError) -> str:
    match exc:
        case anthropic.AuthenticationError() | anthropic.PermissionDeniedError():
            return "The assistant is misconfigured (Claude API key rejected)."
        case anthropic.RateLimitError() | anthropic.OverloadedError():
            return "The assistant is busy right now. Please try again in a minute."
        case anthropic.APIConnectionError():
            return "Could not reach the assistant service. Please try again."
        case anthropic.APIStatusError():
            return f"The assistant hit an error (HTTP {exc.status_code}). Please try again."
        case _:
            return "The assistant hit an unexpected error. Please try again."
