"""A stand-in for Claude that replays scripted turns: used by tests and the CI eval mode.

A turn is `{"content": [...blocks...], "stop_reason": "..."}` - the same shape the eval
recorder saves from live runs - so recorded and hand-written scripts are interchangeable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from anthropic.types import Message

Turn = dict[str, Any]


class ScriptExhausted(Exception):
    pass


class ReplayStream:
    def __init__(self, message: Message) -> None:
        self._message = message
        self.text_stream: AsyncIterator[str] = self._texts()

    async def _texts(self) -> AsyncIterator[str]:
        for block in self._message.content:
            if block.type == "text":
                yield block.text

    async def get_final_message(self) -> Message:
        return self._message


class ReplayModel:
    """Pops one scripted turn per request and keeps every request for assertions."""

    def __init__(self, turns: list[Turn]) -> None:
        self._turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    @asynccontextmanager
    async def stream(self, params: dict[str, Any]) -> AsyncIterator[ReplayStream]:
        self.requests.append(params)
        if not self._turns:
            raise ScriptExhausted(f"No scripted turn left for request #{len(self.requests)}")
        yield ReplayStream(to_message(self._turns.pop(0), len(self.requests)))

    @property
    def remaining(self) -> int:
        return len(self._turns)


def to_message(turn: Turn, index: int = 1) -> Message:
    content = [
        {**block, "id": block.get("id", f"toolu_{index:02d}_{n}")}
        if block["type"] == "tool_use"
        else block
        for n, block in enumerate(turn["content"])
    ]
    has_tools = any(b["type"] == "tool_use" for b in content)
    return Message.model_validate(
        {
            "id": f"msg_replay_{index:02d}",
            "type": "message",
            "role": "assistant",
            "model": "replay",
            "content": content,
            "stop_reason": turn.get("stop_reason", "tool_use" if has_tools else "end_turn"),
            "stop_sequence": None,
            "stop_details": turn.get("stop_details"),
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    )


def text(value: str) -> Turn:
    return {"content": [{"type": "text", "text": value}]}


def tools(*calls: tuple[str, dict[str, Any]], say: str | None = None) -> Turn:
    """A turn that requests tools (several = a parallel batch), optionally after some text."""
    blocks: list[dict[str, Any]] = [{"type": "text", "text": say}] if say else []
    blocks += [{"type": "tool_use", "name": name, "input": args} for name, args in calls]
    return {"content": blocks}
