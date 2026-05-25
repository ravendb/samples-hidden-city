"""
FastAPI entrypoint.
Session loading/saving happens here; the loop itself is stateless.
"""
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

load_dotenv(override=True)
from pydantic import BaseModel

from src.agent.loop import AgentResult, run_agent, stream_agent
from src.db.client import doc_to_dict, get_store
from src.db.models import ConversationTurn
from src.db.seed import seed_if_empty

log = logging.getLogger(__name__)
app = FastAPI(title="Hidden City Flight Agent")

_UI_PATH = Path(__file__).parent.parent / "chat" / "index.html"


@app.on_event("startup")
async def _startup() -> None:
    import asyncio

    host = os.getenv("HOST", "127.0.0.1")
    port = os.getenv("PORT", "8000")
    print("\n  Hidden City Flight Agent", flush=True)
    print(f"  Chat UI  →  http://{host}:{port}/", flush=True)
    print("  DB       →  seeding check...", flush=True)
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, seed_if_empty)
    except Exception as _e:
        import traceback
        print(f"  DB       →  seed failed: {_e}", flush=True)
        traceback.print_exc()
        log.exception("DB seed failed — continuing without fixture data")

    token = os.getenv("TRAVELPAYOUTS_TOKEN")
    if token:
        try:
            from src.scraper.run import run as _run_scraper
            await _run_scraper()
        except Exception as _e:
            print(f"  Travelpayouts → failed: {_e}", flush=True)
            log.exception("Travelpayouts fetch failed at startup")
    else:
        print("  Travelpayouts → TRAVELPAYOUTS_TOKEN not set, skipping", flush=True)
    print("", flush=True)


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
    """Load last 10 turns from RavenDB session document as OpenAI messages."""
    doc_id = f"sessions/{user_id}-{session_id}"
    store = get_store()
    with store.open_session() as session:
        raw = session.load(doc_id)

    if raw is None:
        return []

    raw_dict = doc_to_dict(raw)
    turns = raw_dict.get("turns", [])[-10:]
    return [{"role": t["role"], "content": t["content"]} for t in turns]


@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
async def ui() -> HTMLResponse:
    if not _UI_PATH.exists():
        raise HTTPException(status_code=404, detail="UI not found")
    return HTMLResponse(_UI_PATH.read_text(encoding="utf-8"))


@app.get("/api/airports")
async def get_airports() -> list[dict]:
    """Return all airport documents for the map."""
    store = get_store()
    with store.open_session() as session:
        docs = list(session.query(collection_name="Airports").take(10_000))
    return [doc_to_dict(d) for d in docs]


@app.get("/api/routes")
async def get_routes() -> list[dict]:
    """Return all route documents for the map arcs."""
    store = get_store()
    with store.open_session() as session:
        docs = list(session.query(collection_name="Routes").take(10_000))
    result = [doc_to_dict(d) for d in docs]
    return result


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


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    import json
    prior_turns = _load_prior_turns(request.user_id, request.session_id)

    async def generate():
        try:
            async for chunk in stream_agent(
                user_message=request.message,
                prior_turns=prior_turns,
            ):
                yield chunk
        except Exception as exc:
            log.exception("stream_agent error")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


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