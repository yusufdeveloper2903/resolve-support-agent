"""Tool plumbing shared by the Claude agent and the MCP server.

A tool is a pydantic input model plus a handler. `call_tool` is the single entry point:
it validates arguments, enforces policy (inside handlers), and writes the audit log.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from resolve import audit
from resolve.db import Database, dumps, utcnow
from resolve.policy import PolicyViolation

Actor = Literal["agent", "mcp", "operator"]


@dataclass(frozen=True)
class ToolContext:
    """Who is acting, and for which customer. Never derived from model output."""

    db: Database
    customer_id: str
    actor: Actor
    conversation_id: str | None = None
    approved_by: str | None = None
    now: datetime = field(default_factory=utcnow)


class ToolError(Exception):
    """An expected failure (bad id, nothing found). Returned to the model as `is_error`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolResult:
    data: dict[str, Any]
    is_error: bool = False
    approval_id: str | None = None  # set when the call is parked for human approval

    def to_content(self) -> str:
        return dumps(self.data)


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NoArguments(ToolInput):
    pass


Handler = Callable[[ToolContext, Any], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[ToolInput]
    handler: Handler
    read_only: bool

    @property
    def input_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = _clean_schema(self.input_model.model_json_schema())
        return schema

    def anthropic_definition(self) -> dict[str, Any]:
        # strict: the API guarantees inputs match the schema; we still validate with pydantic.
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


REGISTRY: dict[str, ToolSpec] = {}


def tool(input_model: type[ToolInput], *, read_only: bool) -> Callable[[Handler], Handler]:
    """Register a handler. Its docstring is the description the model sees."""

    def register(handler: Handler) -> Handler:
        name = handler.__name__
        REGISTRY[name] = ToolSpec(
            name=name,
            description=" ".join((handler.__doc__ or "").split()),
            input_model=input_model,
            handler=handler,
            read_only=read_only,
        )
        return handler

    return register


def call_tool(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> ToolResult:
    spec = REGISTRY.get(name)
    try:
        if spec is None:
            raise ToolError("unknown_tool", f"No tool named {name!r}.")
        result = spec.handler(ctx, spec.input_model.model_validate(arguments))
        outcome = "pending_approval" if result.approval_id else "ok"
    except ValidationError as exc:
        errors = [
            {"field": ".".join(map(str, e["loc"])), "problem": e["msg"]} for e in exc.errors()
        ]
        result = ToolResult({"error": "invalid_arguments", "details": errors}, is_error=True)
        outcome = "invalid"
    except PolicyViolation as exc:
        result = ToolResult({"error": exc.code, "message": str(exc)}, is_error=True)
        outcome = "denied"
    except ToolError as exc:
        result = ToolResult({"error": exc.code, "message": str(exc)}, is_error=True)
        outcome = "error"
    audit.record(
        ctx.db,
        actor=ctx.actor,
        tool=name,
        arguments=arguments,
        outcome=outcome,
        detail=result.data,
        customer_id=ctx.customer_id,
        conversation_id=ctx.conversation_id,
    )
    return result


def _clean_schema(node: Any) -> Any:
    """Drop pydantic's cosmetic `title`/`default` keys; everything else is plain JSON Schema."""
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    cleaned = {k: _clean_schema(v) for k, v in node.items() if k not in ("title", "default")}
    if "properties" in node:  # property *names* may legitimately be "title" or "default"
        cleaned["properties"] = {k: _clean_schema(v) for k, v in node["properties"].items()}
    return cleaned
