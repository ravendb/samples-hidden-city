"""
FastAPI entrypoint — POST /chat is the only inbound surface.
Session loading/saving happens here; the loop itself is stateless.
"""
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

load_dotenv()
from pydantic import BaseModel

from src.agent.loop import AgentResult, run_agent
from src.db.client import get_store
from src.db.models import ConversationTurn

log = logging.getLogger(__name__)
app = FastAPI(title="Hidden City Flight Agent")


class ChatRequest(BaseModel):
    user_id: str
    session_id: str = "1"
    message: str


class ChatResponse(BaseModel):
    response: str
    session_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tool_calls: int


def _load_prior_turns(user_id: str, session_id: str) -> list[dict]:
    """Load last 10 turns from RavenDB session document as Anthropic messages."""
    doc_id = f"sessions/{user_id}-{session_id}"
    store = get_store()
    with store.open_session() as session:
        raw = session.load(doc_id)

    if raw is None:
        return []

    turns = raw.get("turns", [])[-10:]  # last 10 turns only
    return [{"role": t["role"], "content": t["content"]} for t in turns]


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    prior_turns = _load_prior_turns(request.user_id, request.session_id)

    result: AgentResult = await run_agent(
        user_message=request.message,
        prior_turns=prior_turns,
    )

    if result.tool_token_warnings:
        log.warning("Token budget warnings: %s", result.tool_token_warnings)

    return ChatResponse(
        response=result.response,
        session_id=request.session_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        total_tokens=result.total_tokens,
        tool_calls=result.tool_calls,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> dict:
    """Returns per-request token budget targets for the demo dashboard."""
    return {
        "budget": {
            "system_prompt_tokens": 200,
            "user_message_tokens": 100,
            "tool_results_tokens": 800,
            "response_tokens": 400,
            "total_tokens": 1500,
        },
        "naive_baseline_tokens": "40000-100000",
    }
