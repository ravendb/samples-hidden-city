"""
get_user_profile tool — loads user preferences from RavenDB.

Profile document lives at users/{user_id}.
Binary attachments (passport scan, bag photo) are not returned here —
they are fetched separately only when explicitly needed.
"""
import logging

from src.db.client import get_store

log = logging.getLogger(__name__)

_DEFAULT_PROFILE = {
    "found": False,
    "preferences": {
        "carry_on_only": False,
        "max_stops": 2,
        "preferred_airlines": [],
    },
}


async def get_user_profile(user_id: str) -> dict:
    store = get_store()

    with store.open_session() as session:
        doc = session.load(f"users/{user_id}")

    if doc is None:
        log.info("No profile found for user %s, returning defaults", user_id)
        return _DEFAULT_PROFILE

    return {
        "found": True,
        "preferences": {
            "carry_on_only": doc.get("carry_on_only", False),
            "max_stops": doc.get("max_stops", 2),
            "preferred_airlines": doc.get("preferred_airlines", []),
            "home_airport": doc.get("home_airport"),
            "loyalty_programs": doc.get("loyalty_programs", []),
        },
    }
