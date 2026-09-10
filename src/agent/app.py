"""
FastAPI entrypoint.
Session loading/saving happens here; the loop itself is stateless.
"""
import asyncio
import logging
import os
import socket
import tempfile
import threading
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from ravendb.documents.subscriptions.options import (
    SubscriptionCreationOptions,
    SubscriptionOpeningStrategy,
    SubscriptionWorkerOptions,
)
from ravendb.exceptions.raven_exceptions import RavenException

_ENV_FILE = Path(__file__).parent.parent.parent / ".env"
_LICENSE_FILE = Path(__file__).parent.parent.parent / "license.json"

load_dotenv(_ENV_FILE, override=True)
from pydantic import BaseModel

from src.agent.loop import AgentResult, run_agent, stream_agent
from src.db.client import doc_to_dict, get_store
from src.db.models import ConversationTurn
from src.db.seed import seed_if_empty
from src.tools.get_user_profile import DEFAULT_PREFERENCES, build_preferences
from src.tools.get_user_profile import get_user_profile as _get_user_profile
from src.tools.save_conversation import persist_turn
from src.tools.openai_status import is_valid as _openai_key_is_valid
from src.tools.openai_status import set_valid as _set_openai_key_valid
from src.tools.travelpayouts_status import is_valid as _travelpayouts_token_is_valid
from src.tools.travelpayouts_status import set_valid as _set_travelpayouts_token_valid
from src.tools.update_user_profile import update_user_profile as _update_user_profile
from src.tools.user_attachments import (
    ATTACHMENT_TYPES,
    get_user_attachment,
    list_user_attachments,
    save_user_attachment,
)

log = logging.getLogger(__name__)
app = FastAPI(title="Hidden City Flight Agent")

_CHAT_DIR = Path(__file__).parent.parent / "chat"
_UI_PATH     = _CHAT_DIR / "index.html"
_LANDING_PATH = _CHAT_DIR / "landing.html"
_SETUP_PATH   = _CHAT_DIR / "setup.html"
_PROFILE_PATH = _CHAT_DIR / "profile.html"


_SCRAPE_MARKER = Path(tempfile.gettempdir()) / "hidden_city_last_scrape_ppid.txt"


def _is_reload_restart() -> bool:
    """True when this startup is a `--reload` worker respawn, not a genuine
    fresh launch. uvicorn --reload keeps one supervisor process alive across
    file-change restarts and only re-spawns the worker subprocess, so
    os.getppid() in the worker stays constant across reloads and only
    changes when a brand new supervisor (a real `start.ps1` run) starts --
    that's what lets the full ~85-origin Travelpayouts scrape run once per
    dev session instead of on every saved file."""
    ppid = str(os.getppid())
    if _SCRAPE_MARKER.exists() and _SCRAPE_MARKER.read_text().strip() == ppid:
        return True
    _SCRAPE_MARKER.write_text(ppid)
    return False


def _run_scraper_background() -> None:
    """Runs the full Travelpayouts scrape on its own thread with its own event
    loop, fully isolated from uvicorn's main loop -- see the call site in
    _startup() for why. asyncio.create_task() alone was NOT enough: this
    coroutine calls put_document() (src/db/client.py) synchronously for every
    route via the (non-async) ravendb client, interleaved with the async
    httpx calls -- confirmed for real that those synchronous writes
    monopolize the main event loop badly enough that uvicorn's own
    "Application startup complete" logging was delayed until the scrape
    finished anyway, identical to the original blocking-await bug it was
    meant to fix (same millisecond in the logs, on two separate pods). A
    dedicated thread + its own loop can't be starved by anything happening on
    the main loop, regardless of what mix of sync/async work runs inside."""
    try:
        from src.scraper.run import run as _run_scraper

        asyncio.run(_run_scraper())
    except Exception as _e:
        print(f"  Travelpayouts → failed: {_e}", flush=True)
        log.exception("Travelpayouts fetch failed at startup")


# ---- Price drop alerts: /ws/alerts fans a RavenDB Data Subscription on the
# PriceAlerts collection (populated by src/worker/run.py) out to connected
# browsers. Each agent pod runs its own subscription under a name unique to
# that pod (hostname), so with k8s/agent/deployment.yaml's replicas: 2 every
# pod gets a full independent replay of the collection instead of the two
# pods splitting a single shared subscription's documents between them --
# a browser connected to either pod's websocket sees every alert. Still push
# end to end, no polling. ----
_alert_websockets: set[WebSocket] = set()


async def _broadcast_alert(alert: dict) -> None:
    dead = []
    for ws in list(_alert_websockets):
        try:
            await ws.send_json(alert)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _alert_websockets.discard(ws)


def _run_alert_subscription_worker(loop: asyncio.AbstractEventLoop) -> None:
    subscription_name = f"hidden-city-alerts-ui-{socket.gethostname()}"
    store = get_store()
    try:
        store.subscriptions.create_for_options(
            SubscriptionCreationOptions(query="from PriceAlerts", name=subscription_name)
        )
    except RavenException as e:
        if "already in use" not in str(e).lower():
            log.exception("Failed to create alert subscription %r", subscription_name)
            return

    def _handle_batch(batch) -> None:
        for item in batch.items:
            asyncio.run_coroutine_threadsafe(_broadcast_alert(item.result), loop)

    worker = store.subscriptions.get_subscription_worker(
        SubscriptionWorkerOptions(
            subscription_name,
            strategy=SubscriptionOpeningStrategy.WAIT_FOR_FREE,
        )
    )
    try:
        worker.run(_handle_batch).result()
    except Exception:
        log.exception("Alert subscription worker stopped")
    finally:
        worker.close()


@app.websocket("/ws/alerts")
async def alerts_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    _alert_websockets.add(websocket)
    try:
        while True:
            # Nothing is expected from the browser -- this just blocks until
            # the client disconnects, which is what raises WebSocketDisconnect.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _alert_websockets.discard(websocket)


async def _print_links_after_startup(host: str, port: str) -> None:
    """Fires one event-loop tick after this coroutine is scheduled, which is
    after uvicorn logs "Application startup complete." (it logs that line
    synchronously right after the startup event handler returns, before the
    loop gets back around to this task) -- so the links land at the bottom,
    not buried under seeding/Travelpayouts scrape output further up."""
    await asyncio.sleep(0.1)
    ravendb_url = os.getenv("RAVENDB_URL", "http://localhost:8080")
    print("\n  ── Links ──", flush=True)
    print(f"  Chat UI         →  http://{host}:{port}/", flush=True)
    print(f"  Swagger UI      →  http://{host}:{port}/docs", flush=True)
    print(f"  RavenDB Studio  →  {ravendb_url}", flush=True)
    print("", flush=True)


@app.on_event("startup")
async def _startup() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    port = os.getenv("PORT", "8001")
    print("\n  Hidden City Flight Agent", flush=True)
    print("  DB       →  seeding check...", flush=True)
    # Not caught: ensure_database (called from seed_if_empty) already retries
    # transient RavenDB unavailability on its own for up to 90s (see
    # src/db/seed.py) -- if it still raises after that, the database is
    # genuinely unusable, and every request would fail with
    # DatabaseDoesNotExistException anyway. A failed startup event makes
    # uvicorn exit non-zero, which is what lets Kubernetes' pod restart policy
    # retry the whole thing -- swallowing this here used to leave the pod
    # reporting Ready and serving 500s forever instead (confirmed in
    # practice), since /health doesn't check DB connectivity.
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, seed_if_empty)

    threading.Thread(target=_run_alert_subscription_worker, args=(loop,), daemon=True).start()

    openai_key = os.getenv("OPENAI_API_KEY")
    if _is_key_configured("OPENAI_API_KEY"):
        from src.agent.loop import validate_openai_key

        openai_key_ok = await validate_openai_key(openai_key)
        _set_openai_key_valid(openai_key_ok)
        if not openai_key_ok:
            print("  OpenAI     → key rejected (401 Unauthorized) — chat will fail until fixed.", flush=True)
            print("  OpenAI     → fix OPENAI_API_KEY in Profile → API keys & tokens, or in .env.", flush=True)
    else:
        _set_openai_key_valid(None)
        print("  OpenAI     → OPENAI_API_KEY not set — chat will fail until configured.", flush=True)

    token = os.getenv("TRAVELPAYOUTS_TOKEN")
    if token:
        from src.scraper.travelpayouts import validate_token

        token_ok = await validate_token(token)
        _set_travelpayouts_token_valid(token_ok)
        if not token_ok:
            print("  Travelpayouts → token rejected (401 Unauthorized) — live price lookups disabled.", flush=True)
            print("  Travelpayouts → fix TRAVELPAYOUTS_TOKEN in Profile → API keys & tokens, or in .env.", flush=True)
        elif _is_reload_restart():
            print("  Travelpayouts → skipping full scrape (--reload restart, already scraped this session)", flush=True)
        else:
            # Run on a separate thread, not just asyncio.create_task() on
            # this loop: this scrape is sequential over ~80 origins
            # (src/scraper/run.py), easily runs 10+ minutes, and mixes async
            # httpx calls with SYNCHRONOUS RavenDB writes (put_document,
            # src/db/client.py -- the ravendb client has no async API). A
            # plain create_task() still let those synchronous writes
            # monopolize this event loop badly enough that uvicorn's own
            # "Application startup complete" was delayed until the scrape
            # finished anyway -- confirmed for real (identical millisecond
            # in the logs on two separate pods), the same symptom as the
            # original blocking-await bug it was meant to fix. A dedicated
            # thread with its own event loop (asyncio.run() inside
            # _run_scraper_background) can't be starved by anything on the
            # main loop no matter what it does internally. With
            # k8s/agent/deployment.yaml's replicas: 2, two fresh pods used
            # to hit Travelpayouts with the same scrape at once on every
            # cold start (the _is_reload_restart() guard above is a
            # local-process PPID marker that never matches in a fresh pod),
            # which is what pushed a real rollout past its progress
            # deadline. The scrape still writes the same data to RavenDB
            # either way -- only *when the pod is allowed to answer
            # /health* changes, not what happens.
            threading.Thread(target=_run_scraper_background, daemon=True).start()
    else:
        _set_travelpayouts_token_valid(None)
        print("  Travelpayouts → TRAVELPAYOUTS_TOKEN not set, skipping", flush=True)
    print("", flush=True)
    asyncio.create_task(_print_links_after_startup(host, port))


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
        loaded = session.load([session_doc_id, user_doc_id])
        session_raw = loaded.get(session_doc_id)
        user_raw = loaded.get(user_doc_id)

    prior_turns: list[dict] = []
    if session_raw is not None:
        turns = doc_to_dict(session_raw).get("turns", [])[-5:]
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


@app.get("/profile", response_class=HTMLResponse)
async def profile_page() -> HTMLResponse:
    if not _PROFILE_PATH.exists():
        raise HTTPException(status_code=404, detail="Profile page not found")
    return HTMLResponse(_PROFILE_PATH.read_text(encoding="utf-8"))


# Browsers request this from every page root regardless of which route served
# the HTML, and it was 404ing on every single one — an inline SVG needs no
# binary asset file to manage, and every modern browser accepts SVG favicons.
_FAVICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y="0.85em" font-size="90">✈️</text></svg>'


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


class SetupRequest(BaseModel):
    openai_api_key: str | None = None
    ravendb_license: str | None = None
    travelpayouts_token: str | None = None


_ENV_EXAMPLE_PLACEHOLDERS = {
    # Literal values from .env.example -- a fresh .env copied from that template
    # carries these verbatim, so a plain truthiness check reports them as "set".
    "OPENAI_API_KEY": "sk-...",
    "TRAVELPAYOUTS_TOKEN": "...",
}


def _is_key_configured(name: str) -> bool:
    value = os.getenv(name)
    return bool(value) and value != _ENV_EXAMPLE_PLACEHOLDERS.get(name)


@app.get("/api/setup/status")
async def api_setup_status() -> dict:
    """Report which setup keys are already configured, so the wizard can skip them."""
    return {
        "openai_api_key_set": _is_key_configured("OPENAI_API_KEY"),
        "openai_api_key_valid": _openai_key_is_valid(),
        "ravendb_license_set": bool(os.getenv("RAVENDB_LICENSE")) or _LICENSE_FILE.exists(),
        "travelpayouts_set": _is_key_configured("TRAVELPAYOUTS_TOKEN"),
        "travelpayouts_valid": _travelpayouts_token_is_valid(),
    }


def _set_env_value(key: str, value: str) -> None:
    """Update one KEY=value line in .env in place, preserving every other line
    (comments, unrelated keys) instead of rewriting the whole file."""
    lines = _ENV_FILE.read_text(encoding="utf-8").splitlines() if _ENV_FILE.exists() else []
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.post("/api/setup")
async def api_setup(body: SetupRequest) -> dict:
    """Persist API keys to .env (repo root, never committed — in .gitignore)."""
    if body.openai_api_key:
        from src.agent.loop import validate_openai_key

        if not await validate_openai_key(body.openai_api_key):
            raise HTTPException(
                status_code=400,
                detail="OpenAI API key rejected (401 Unauthorized) — "
                "check the value at platform.openai.com → API keys.",
            )
        _set_env_value("OPENAI_API_KEY", body.openai_api_key)
        os.environ["OPENAI_API_KEY"] = body.openai_api_key
        _set_openai_key_valid(True)

    if body.ravendb_license:
        _set_env_value("RAVENDB_LICENSE", body.ravendb_license)
        os.environ["RAVENDB_LICENSE"] = body.ravendb_license

    if body.travelpayouts_token:
        from src.scraper.travelpayouts import validate_token

        if not await validate_token(body.travelpayouts_token):
            raise HTTPException(
                status_code=400,
                detail="Travelpayouts token rejected (401 Unauthorized) — "
                "check the value at travelpayouts.com → API access.",
            )
        _set_env_value("TRAVELPAYOUTS_TOKEN", body.travelpayouts_token)
        os.environ["TRAVELPAYOUTS_TOKEN"] = body.travelpayouts_token
        _set_travelpayouts_token_valid(True)

    return {"status": "ok"}


@app.get("/api/airports")
async def get_airports() -> list[dict]:
    """Return all airport documents for the map."""
    store = get_store()
    with store.open_session() as session:
        docs = list(session.query_collection("Airports").take(10_000))
    return [doc_to_dict(d) for d in docs]


@app.get("/api/routes")
async def get_routes() -> list[dict]:
    """Return all route documents for the map arcs."""
    store = get_store()
    with store.open_session() as session:
        docs = list(session.query_collection("Routes").take(10_000))
    result = [doc_to_dict(d) for d in docs]
    return result


@app.get("/api/profile")
async def get_profile(user_id: str = "demo") -> dict:
    """Return the user's saved profile/preferences for the chat UI profile panel."""
    return await _get_user_profile(user_id)


class ProfileDetailsRequest(BaseModel):
    user_id: str = "demo"
    name: str | None = None
    carry_on_only: bool | None = None
    home_airport: str | None = None
    budget_max: float | None = None
    budget_currency: str | None = None
    max_stops: int | None = None
    departure_airports: list[str] | None = None
    countries_of_interest: list[str] | None = None
    destinations: list[str] | None = None
    preferred_airlines: list[str] | None = None
    loyalty_programs: list[str] | None = None


@app.post("/api/profile/details")
async def save_profile_details(body: ProfileDetailsRequest) -> dict:
    """Save structured profile fields submitted from the Profile screen's form.

    merge_lists=False: the form shows the user's full current list for each
    field, so "Save" replaces it outright — unlike the update_user_profile LLM
    tool, which only ever appends one newly-learned value at a time (see that
    function's docstring).
    """
    return await _update_user_profile(
        user_id=body.user_id,
        name=body.name,
        carry_on_only=body.carry_on_only,
        home_airport=body.home_airport,
        budget_max=body.budget_max,
        budget_currency=body.budget_currency,
        max_stops=body.max_stops,
        departure_airports=body.departure_airports,
        countries_of_interest=body.countries_of_interest,
        destinations=body.destinations,
        preferred_airlines=body.preferred_airlines,
        loyalty_programs=body.loyalty_programs,
        merge_lists=False,
    )


@app.get("/api/profile/attachments")
async def get_profile_attachments(user_id: str = "demo") -> list[dict]:
    """List attachment metadata (name, content type, size) for the profile screen."""
    return list_user_attachments(user_id)


@app.post("/api/profile/attachment")
async def upload_profile_attachment(
    user_id: str = Form("demo"),
    attachment_type: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """Upload a passport scan, bag photo, or preference sheet (PDF) as a RavenDB attachment."""
    if attachment_type not in ATTACHMENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"attachment_type must be one of {ATTACHMENT_TYPES}",
        )
    content = await file.read()
    return save_user_attachment(
        user_id=user_id,
        attachment_type=attachment_type,
        filename=file.filename or attachment_type,
        content=content,
        content_type=file.content_type or "application/octet-stream",
    )


@app.get("/api/profile/attachment/{attachment_type}")
async def download_profile_attachment(attachment_type: str, user_id: str = "demo") -> Response:
    """Stream back a previously uploaded attachment (e.g. to preview/download it)."""
    result = get_user_attachment(user_id, attachment_type)
    if result is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return Response(content=result["content"], media_type=result["content_type"])


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    try:
        prior_turns, preferences = _load_conversation_context(request.user_id, request.session_id)

        result: AgentResult = await run_agent(
            user_message=request.message,
            prior_turns=prior_turns,
            user_id=request.user_id,
            session_id=request.session_id,
            preferences=preferences,
        )
    except Exception as exc:
        # Same external-actor boundary as dispatch_tool (src/tools/dispatcher.py) --
        # a transient RavenDB or OpenAI failure here must not surface as a raw,
        # unexplained 500 to the chat UI.
        log.exception("chat request failed")
        raise HTTPException(status_code=502, detail=f"Agent request failed: {exc}") from exc

    if result.tool_token_warnings:
        log.warning("Token budget warnings: %s", result.tool_token_warnings)
    if result.grounding_warnings:
        log.warning("Grounding warnings: %s", result.grounding_warnings)

    await persist_turn(
        user_id=request.user_id,
        session_id=request.session_id,
        user_message=request.message,
        assistant_response=result.response,
    )

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

    async def generate():
        try:
            prior_turns, preferences = _load_conversation_context(
                request.user_id, request.session_id
            )
            async for chunk in stream_agent(
                user_message=request.message,
                prior_turns=prior_turns,
                user_id=request.user_id,
                session_id=request.session_id,
                preferences=preferences,
            ):
                yield chunk
                payload = json.loads(chunk[len("data: "):])
                if payload.get("type") == "done":
                    await persist_turn(
                        user_id=request.user_id,
                        session_id=request.session_id,
                        user_message=request.message,
                        assistant_response=payload["response"],
                    )
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
            "tool_results_tokens": 1800,
            "response_tokens": 400,
            "total_tokens": 2500,
        },
        "naive_baseline_tokens": "40000-100000",
    }