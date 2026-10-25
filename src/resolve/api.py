"""HTTP API: customer chat (SSE), operator approvals, audit log, and the single-page UI.

Authentication is out of scope for the demo: the `X-Customer-Id` header stands in for a session
cookie. What matters is *where* identity comes from - the request, never the model - and that a
conversation is bound to one customer when it is created.
"""

# No `from __future__ import annotations`: FastAPI resolves the closures' annotations at runtime.
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

import anthropic
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from resolve import approvals, audit, conversations
from resolve.agent import Agent, AgentEvent, AnthropicModel, ConversationStateError, ModelClient
from resolve.config import Settings
from resolve.db import Database
from resolve.seed import seed

STATIC_DIR = Path(__file__).parent / "static"


class NewConversation(BaseModel):
    customer_id: str


class ChatMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class Decision(BaseModel):
    approve: bool
    operator: str = Field(min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=500)


def create_app(settings: Settings, model: ModelClient | None = None) -> FastAPI:
    """`model` is injectable for tests; by default a real client is built when a key exists."""
    db = Database(settings.db_path)
    if db.query_one("SELECT name FROM sqlite_master WHERE name = 'customers'") is None:
        seed(db)
    if model is None and settings.api_key:
        model = AnthropicModel(anthropic.AsyncAnthropic(api_key=settings.api_key))
    agent = (
        Agent(
            db,
            model,
            model_name=settings.model,
            effort=settings.effort,
            max_tokens=settings.max_tokens,
        )
        if model
        else None
    )

    app = FastAPI(title="Resolve", version="0.1.0")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(LookupError)
    async def not_found(_: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse({"detail": f"Not found: {exc}"}, status_code=404)

    @app.exception_handler(ConversationStateError)
    async def conflict(_: Request, exc: ConversationStateError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def require_agent() -> Agent:
        if agent is None:
            raise HTTPException(503, "No ANTHROPIC_API_KEY configured: the agent is offline.")
        return agent

    def owned_conversation(
        conversation_id: str, x_customer_id: Annotated[str, Header()]
    ) -> dict[str, Any]:
        conversation = conversations.get(db, conversation_id)
        if conversation is None or conversation["customer_id"] != x_customer_id:
            raise LookupError(conversation_id)
        return conversation

    Conversation = Annotated[dict[str, Any], Depends(owned_conversation)]
    AgentDep = Annotated[Agent, Depends(require_agent)]

    def chat_ready(conversation: Conversation, agent: AgentDep) -> str:
        # Dependencies run before the stream opens, so 404/409/503 stay plain JSON responses.
        agent.check_can_chat(conversation["id"])
        return str(conversation["id"])

    def resume_ready(conversation: Conversation, agent: AgentDep) -> str:
        agent.check_can_resume(conversation["id"])
        return str(conversation["id"])

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "model": settings.model, "agent_online": agent is not None}

    @app.get("/api/customers")
    def customers() -> list[dict[str, Any]]:
        """Demo sign-in: the UI lets you pick which customer you are."""
        return db.query("SELECT id, name, tier FROM customers ORDER BY id")

    @app.post("/api/conversations", status_code=201)
    def create_conversation(body: NewConversation) -> dict[str, Any]:
        if db.query_one("SELECT id FROM customers WHERE id = ?", (body.customer_id,)) is None:
            raise LookupError(body.customer_id)
        return conversations.create(db, body.customer_id)

    @app.get("/api/conversations/{conversation_id}")
    def read_conversation(conversation: Conversation) -> dict[str, Any]:
        pending = [
            a
            for a in approvals.list_approvals(db, "pending")
            if a["conversation_id"] == conversation["id"]
        ]
        return {
            **conversation,
            "pending_approvals": pending,
            "ready_to_resume": conversation["status"] == "awaiting_approval" and not pending,
            "transcript": conversations.transcript(conversations.history(db, conversation["id"])),
        }

    @app.post("/api/conversations/{conversation_id}/messages", response_class=EventSourceResponse)
    async def send_message(
        conversation_id: Annotated[str, Depends(chat_ready)], body: ChatMessage, agent: AgentDep
    ) -> AsyncIterator[ServerSentEvent]:
        async for event in agent.chat(conversation_id, body.text):
            yield _sse(event)

    @app.post("/api/conversations/{conversation_id}/resume", response_class=EventSourceResponse)
    async def resume(
        conversation_id: Annotated[str, Depends(resume_ready)], agent: AgentDep
    ) -> AsyncIterator[ServerSentEvent]:
        async for event in agent.resume(conversation_id):
            yield _sse(event)

    @app.get("/api/approvals")
    def list_approvals(status: str | None = None) -> list[dict[str, Any]]:
        return approvals.list_approvals(db, status)

    @app.post("/api/approvals/{approval_id}/decision")
    def decide(approval_id: str, body: Decision) -> dict[str, Any]:
        try:
            return approvals.decide(
                db, approval_id, approve=body.approve, operator=body.operator, note=body.note
            )
        except approvals.ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/audit")
    def audit_log(limit: int = 100) -> list[dict[str, Any]]:
        return audit.recent(db, min(limit, 500))

    @app.post("/api/reset", status_code=204)
    def reset() -> None:
        """Restore the demo fixtures (clears conversations, approvals and the audit log)."""
        seed(db)

    return app


def _sse(event: AgentEvent) -> ServerSentEvent:
    return ServerSentEvent(event=event.type, data=event.data)


def app_from_env() -> FastAPI:
    """App factory for `uvicorn --factory` (used by the Dockerfile)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return create_app(Settings.from_env())


def main() -> None:
    uvicorn.run(app_from_env(), host="127.0.0.1", port=8000)
