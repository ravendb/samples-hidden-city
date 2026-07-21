"""
update_user_profile tool — persists durable user preferences to RavenDB.

Call this when the user expresses a preference that should carry across sessions:
  "Call me Alex"                         → name="Alex"
  "I always travel carry-on only"        → carry_on_only=True
  "I'm based in Warsaw"                  → home_airport="WAW", departure_airports=["WAW"]
  "I sometimes fly from Krakow too"      → departure_airports=["KRK"]
  "I'm interested in China"              → countries_of_interest=["China"]
  "Thinking about Chongqing or Beijing"  → destinations=["Chongqing", "Beijing"]
  "My budget is around 3000 PLN"         → budget_max=3000, budget_currency="PLN"
  "I have a Miles & More account"        → loyalty_programs=["Miles & More"]

List fields (departure_airports, countries_of_interest, destinations,
preferred_airlines, loyalty_programs) are merged with whatever is already saved —
pass only the new values just learned, not the full list. Earlier values are
never dropped by a later call.

Do NOT call this for trip-specific constraints (e.g. "max 1 stop for this trip").
Use save_conversation constraints for those.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from src.db.client import doc_to_dict, get_store, put_document

log = logging.getLogger(__name__)


def _merge_list(existing: list, new: list) -> list:
    seen = {str(v).lower() for v in existing}
    merged = list(existing)
    for v in new:
        if str(v).lower() not in seen:
            merged.append(v)
            seen.add(str(v).lower())
    return merged


async def update_user_profile(
    user_id: str,
    name: Optional[str] = None,
    carry_on_only: Optional[bool] = None,
    max_stops: Optional[int] = None,
    home_airport: Optional[str] = None,
    departure_airports: Optional[list] = None,
    countries_of_interest: Optional[list] = None,
    destinations: Optional[list] = None,
    preferred_airlines: Optional[list] = None,
    loyalty_programs: Optional[list] = None,
    budget_max: Optional[float] = None,
    budget_currency: Optional[str] = None,
) -> dict:
    store = get_store()

    with store.open_session() as session:
        raw = session.load(f"users/{user_id}")
        doc = doc_to_dict(raw) if raw is not None else {}

    updates: dict = {}
    if name is not None:
        updates["name"] = name
    if carry_on_only is not None:
        updates["carry_on_only"] = carry_on_only
    if max_stops is not None:
        updates["max_stops"] = max_stops
    if home_airport is not None:
        updates["home_airport"] = home_airport.upper()
    if departure_airports is not None:
        updates["departure_airports"] = _merge_list(
            doc.get("departure_airports", []), [a.upper() for a in departure_airports]
        )
    if countries_of_interest is not None:
        updates["countries_of_interest"] = _merge_list(
            doc.get("countries_of_interest", []), countries_of_interest
        )
    if destinations is not None:
        updates["destinations"] = _merge_list(doc.get("destinations", []), destinations)
    if preferred_airlines is not None:
        updates["preferred_airlines"] = _merge_list(
            doc.get("preferred_airlines", []), preferred_airlines
        )
    if loyalty_programs is not None:
        updates["loyalty_programs"] = _merge_list(
            doc.get("loyalty_programs", []), loyalty_programs
        )
    if budget_max is not None:
        updates["budget_max"] = budget_max
    if budget_currency is not None:
        updates["budget_currency"] = budget_currency.upper()

    if not updates:
        return {"saved": False, "reason": "no fields to update"}

    doc.update(updates)
    doc["last_updated"] = datetime.now(timezone.utc).isoformat()
    doc.pop("@metadata", None)
    doc["@metadata"] = {"@collection": "Users"}

    put_document(store, f"users/{user_id}", doc)

    log.info("Updated profile for %s: %s", user_id, list(updates.keys()))
    return {"saved": True, "user_id": user_id, "updated_fields": list(updates.keys())}
