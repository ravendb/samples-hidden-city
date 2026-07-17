"""
get_user_profile tool — loads user preferences from RavenDB.

Profile document lives at users/{user_id}.
Binary attachments (passport scan, bag photo) are not returned here —
they are fetched separately only when explicitly needed.

`build_preferences`/`DEFAULT_PREFERENCES` are also used by app.py to preload
preferences into the system prompt on every turn (see _load_conversation_context
in src/agent/app.py) — kept here so the shape stays in one place.
"""
import logging

from src.db.client import doc_to_dict, get_store

log = logging.getLogger(__name__)

DEFAULT_PREFERENCES = {
    "name": None,
    "carry_on_only": False,
    "max_stops": 2,
    "home_airport": None,
    "departure_airports": [],
    "countries_of_interest": [],
    "destinations": [],
    "preferred_airlines": [],
    "loyalty_programs": [],
    "budget_max": None,
    "budget_currency": None,
}


def build_preferences(d: dict) -> dict:
    return {
        "name": d.get("name"),
        "carry_on_only": d.get("carry_on_only", False),
        "max_stops": d.get("max_stops", 2),
        "home_airport": d.get("home_airport"),
        "departure_airports": d.get("departure_airports", []),
        "countries_of_interest": d.get("countries_of_interest", []),
        "destinations": d.get("destinations", []),
        "preferred_airlines": d.get("preferred_airlines", []),
        "loyalty_programs": d.get("loyalty_programs", []),
        "budget_max": d.get("budget_max"),
        "budget_currency": d.get("budget_currency"),
    }


async def get_user_profile(user_id: str) -> dict:
    store = get_store()

    with store.open_session() as session:
        doc = session.load(f"users/{user_id}")

    if doc is None:
        log.info("No profile found for user %s, returning defaults", user_id)
        return {"found": False, "preferences": dict(DEFAULT_PREFERENCES)}

    d = doc_to_dict(doc)
    return {"found": True, "preferences": build_preferences(d)}
