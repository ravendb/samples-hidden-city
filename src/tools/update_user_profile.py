"""
update_user_profile tool — persists durable user preferences to RavenDB.

Call this when the user expresses a preference that should carry across sessions:
  "I always travel carry-on only"  → carry_on_only=True
  "I'm based in Warsaw"            → home_airport="WAW"
  "I have a Miles & More account"  → loyalty_programs=["Miles & More"]

Do NOT call this for trip-specific constraints (e.g. "max 1 stop for this trip").
Use save_conversation constraints for those.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from pyravendb.commands.commands_data import PutDocumentCommand

from src.db.client import doc_to_dict, get_store

log = logging.getLogger(__name__)


async def update_user_profile(
    user_id: str,
    carry_on_only: Optional[bool] = None,
    max_stops: Optional[int] = None,
    home_airport: Optional[str] = None,
    preferred_airlines: Optional[list] = None,
    loyalty_programs: Optional[list] = None,
) -> dict:
    store = get_store()

    with store.open_session() as session:
        raw = session.load(f"users/{user_id}")
        doc = doc_to_dict(raw) if raw is not None else {}

    updates: dict = {}
    if carry_on_only is not None:
        updates["carry_on_only"] = carry_on_only
    if max_stops is not None:
        updates["max_stops"] = max_stops
    if home_airport is not None:
        updates["home_airport"] = home_airport.upper()
    if preferred_airlines is not None:
        updates["preferred_airlines"] = preferred_airlines
    if loyalty_programs is not None:
        updates["loyalty_programs"] = loyalty_programs

    if not updates:
        return {"saved": False, "reason": "no fields to update"}

    doc.update(updates)
    doc["last_updated"] = datetime.now(timezone.utc).isoformat()
    doc.pop("@metadata", None)
    doc["@metadata"] = {"@collection": "Users"}

    store.get_request_executor().execute(
        PutDocumentCommand(key=f"users/{user_id}", document=doc)
    )

    log.info("Updated profile for %s: %s", user_id, list(updates.keys()))
    return {"saved": True, "user_id": user_id, "updated_fields": list(updates.keys())}
