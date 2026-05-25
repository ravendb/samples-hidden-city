"""
save_conversation tool — persists the current turn to RavenDB.

The model calls this after generating its final response.
Appends both turns and updates active_constraints if the user expressed any.
Conversation lives in sessions/{user_id}-{session_id} as a queryable document.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from pyravendb.commands.commands_data import PutDocumentCommand

from src.db.client import doc_to_dict, get_store
from src.db.models import ActiveConstraints, ConversationTurn, SessionDocument

log = logging.getLogger(__name__)


def _session_doc_id(user_id: str, session_id: str) -> str:
    return f"sessions/{user_id}-{session_id}"


async def save_conversation(
    user_id: str,
    session_id: str,
    user_message: str,
    assistant_response: str,
    constraints: Optional[dict] = None,
) -> dict:
    doc_id = _session_doc_id(user_id, session_id)
    store = get_store()
    now = datetime.now(timezone.utc)

    with store.open_session() as session:
        raw = session.load(doc_id)

        if raw is None:
            doc = SessionDocument(user_id=user_id)
        else:
            doc = SessionDocument(**doc_to_dict(raw))

        doc.turns.append(ConversationTurn(role="user", content=user_message, timestamp=now))
        doc.turns.append(
            ConversationTurn(role="assistant", content=assistant_response, timestamp=now)
        )
        doc.last_active = now

        if constraints:
            current = doc.active_constraints.model_dump()
            current.update({k: v for k, v in constraints.items() if v is not None})
            doc.active_constraints = ActiveConstraints(**current)

        data = doc.model_dump(mode="json")
        data["@metadata"] = {"@collection": "Sessions"}
        store.get_request_executor().execute(PutDocumentCommand(key=doc_id, document=data))

    log.info("Saved turn for %s session %s (%d total turns)", user_id, session_id, len(doc.turns))
    return {"saved": True, "session_id": doc_id, "total_turns": len(doc.turns)}
