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

_ENV_LOCAL = Path(__file__).parent.parent.parent / ".env.local"
_LICENSE_FILE = Path(__file__).parent.parent.parent / "license.json"

load_dotenv(override=True)
load_dotenv(_ENV_LOCAL, override=True)
from pydantic import BaseModel

from src.agent.loop import AgentResult, run_agent, stream_agent
from src.db.client import doc_to_dict, get_store
from src.db.models import ConversationTurn
from src.db.seed import seed_if_empty
from src.tools.get_user_profile import DEFAULT_PREFERENCES, build_preferences
from src.tools.get_user_profile import get_user_profile as _get_user_profile

log = logging.getLogger(__name__)
app = FastAPI(title="Hidden City Flight Agent")

_CHAT_DIR = Path(__file__).parent.parent / "chat"
_UI_PATH     = _CHAT_DIR / "index.html"
_LANDING_PATH = _CHAT_DIR / "landing.html"
_SETUP_PATH   = _CHAT_DIR / "setup.html"


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


def _load_conversation_context(user_id: str, session_id: str) -> tuple[list[dict], dict]:
    """Load prior turns and the user profile in a single RavenDB round trip.

    RavenDB feature used: batched multi-document Load. `session.load([id1, id2])`
    issues one GetDocumentCommand for every id passed, regardless of collection —
    so the session document (Sessions) and the profile document (Users) come back
    in a single request instead of two. This is what lets every turn deterministically
    preload the user's preferences into the system prompt (see loop.py) without
    depending on the model choosing to call get_user_profile as a tool.
    """
    session_doc_id = f"sessions/{user_id}-{session_id}"
    user_doc_id = f"users/{user_id}"

    store = get_store()
    with store.open_session() as session:
        session_raw, user_raw = session.load([session_doc_id, user_doc_id])

    prior_turns: list[dict] = []
    if session_raw is not None:
        turns = doc_to_dict(session_raw).get("turns", [])[-10:]
        prior_turns = [{"role": t["role"], "content": t["content"]} for t in turns]

    preferences = dict(DEFAULT_PREFERENCES)
    if user_raw is not None:
        preferences = build_preferences(doc_to_dict(user_raw))

    return prior_turns, preferences


@app.get("/", response_class=HTMLResponse)
async def landing() -> HTMLResponse:
    if not _LANDING_PATH.exists():
        raise HTTPException(status_code=404, detail="Landing page not found")
    return HTMLResponse(_LANDING_PATH.read_text(encoding="utf-8"))


@app.get("/setup", response_class=HTMLResponse)
async def setup_page() -> HTMLResponse:
    if not _SETUP_PATH.exists():
        raise HTTPException(status_code=404, detail="Setup page not found")
    return HTMLResponse(_SETUP_PATH.read_text(encoding="utf-8"))


@app.get("/ui", response_class=HTMLResponse)
async def ui() -> HTMLResponse:
    if not _UI_PATH.exists():
        raise HTTPException(status_code=404, detail="UI not found")
    return HTMLResponse(_UI_PATH.read_text(encoding="utf-8"))


class SetupRequest(BaseModel):
    openai_api_key: str | None = None
    ravendb_license: str | None = None


@app.get("/api/setup/status")
async def api_setup_status() -> dict:
    """Report which setup keys are already configured, so the wizard can skip them."""
    return {
        "openai_api_key_set": bool(os.getenv("OPENAI_API_KEY")),
        "ravendb_license_set": bool(os.getenv("RAVENDB_LICENSE")) or _LICENSE_FILE.exists(),
    }


@app.post("/api/setup")
async def api_setup(body: SetupRequest) -> dict:
    """Persist API keys to .env.local (never committed — in .gitignore)."""
    # Read existing entries so we can merge
    existing: dict[str, str] = {}
    if _ENV_LOCAL.exists():
        for line in _ENV_LOCAL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    if body.openai_api_key:
        existing["OPENAI_API_KEY"] = body.openai_api_key
        os.environ["OPENAI_API_KEY"] = body.openai_api_key

    if body.ravendb_license:
        existing["RAVENDB_LICENSE"] = body.ravendb_license
        os.environ["RAVENDB_LICENSE"] = body.ravendb_license

    lines = [f"{k}={v}" for k, v in existing.items()]
    _ENV_LOCAL.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"status": "ok"}


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


@app.get("/api/profile")
async def get_profile(user_id: str = "demo") -> dict:
    """Return the user's saved profile/preferences for the chat UI profile panel."""
    return await _get_user_profile(user_id)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    prior_turns, preferences = _load_conversation_context(request.user_id, request.session_id)

    result: AgentResult = await run_agent(
        user_message=request.message,
        prior_turns=prior_turns,
        user_id=request.user_id,
        session_id=request.session_id,
        preferences=preferences,
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
    prior_turns, preferences = _load_conversation_context(request.user_id, request.session_id)

    async def generate():
        try:
            async for chunk in stream_agent(
                user_message=request.message,
                prior_turns=prior_turns,
                user_id=request.user_id,
                session_id=request.session_id,
                preferences=preferences,
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