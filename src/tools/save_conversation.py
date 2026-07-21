"""
Conversation persistence for sessions/{user_id}-{session_id}.

persist_turn() is called directly by app.py after the agent produces its final
response, not through the LLM — the text is already known server-side, so
routing it through a tool call would force the model to restate the full
response as a tool argument before saying it again as the final message,
paying for both the duplicated output tokens and an extra round trip.

update_constraints() stays an LLM-facing tool: trip-specific constraints
(carry-on only for this trip, max stops) can only come from what the user just
said, so the model still has to report them explicitly when they come up —
but only then, not on every turn.
"""
import logging
from datetime import datetime, timezone

from pyravendb.commands.commands_data import PutDocumentCommand

from src.db.client import doc_to_dict, get_store
from src.db.models import ActiveConstraints, ConversationTurn, SessionDocument

log = logging.getLogger(__name__)


def _session_doc_id(user_id: str, session_id: str) -> str:
    return f"sessions/{user_id}-{session_id}"


def _load_session(store, doc_id: str, user_id: str) -> SessionDocument:
    with store.open_session() as session:
        raw = session.load(doc_id)
    return SessionDocument(**doc_to_dict(raw)) if raw is not None else SessionDocument(user_id=user_id)


def _write_session(store, doc_id: str, doc: SessionDocument) -> None:
    data = doc.model_dump(mode="json")
    data["@metadata"] = {"@collection": "Sessions"}
    store.get_request_executor().execute(PutDocumentCommand(key=doc_id, document=data))


async def persist_turn(user_id: str, session_id: str, user_message: str, assistant_response: str) -> dict:
    doc_id = _session_doc_id(user_id, session_id)
    store = get_store()
    now = datetime.now(timezone.utc)

    doc = _load_session(store, doc_id, user_id)
    doc.turns.append(ConversationTurn(role="user", content=user_message, timestamp=now))
    doc.turns.append(ConversationTurn(role="assistant", content=assistant_response, timestamp=now))
    doc.last_active = now
    _write_session(store, doc_id, doc)

    log.info("Persisted turn for %s session %s (%d total turns)", user_id, session_id, len(doc.turns))
    return {"saved": True, "session_id": doc_id, "total_turns": len(doc.turns)}


async def update_constraints(user_id: str, session_id: str, constraints: dict) -> dict:
    """LLM tool — call only when the user states a trip-specific constraint this
    turn (e.g. carry-on only for this trip, max 1 stop). Merges into
    active_constraints; does not touch conversation turns."""
    doc_id = _session_doc_id(user_id, session_id)
    store = get_store()

    doc = _load_session(store, doc_id, user_id)
    current = doc.active_constraints.model_dump()
    current.update({k: v for k, v in constraints.items() if v is not None})
    doc.active_constraints = ActiveConstraints(**current)
    _write_session(store, doc_id, doc)

    log.info("Updated constraints for %s session %s: %s", user_id, session_id, constraints)
    return {"saved": True, "active_constraints": doc.active_constraints.model_dump()}
