"""
Fast-path origin/destination resolution for a plain "from X to Y" message.

Deliberate, narrow exception to the "model decides what to fetch" rule in
CLAUDE.md: for an unambiguous request, resolving the airports ourselves and
pre-fetching search_routes lets the whole turn finish in a single OpenAI call
instead of two (the model never has to spend a round trip deciding to call
search_routes — it just sees the result). Anything ambiguous or not matching
the "from X to Y" shape returns None, and the caller (src/agent/loop.py) falls
back to the normal multi-turn tool-calling flow unchanged.

City/name resolution goes through RavenDB's full-text search (.search() on
the Airports collection) rather than a hardcoded city->IATA table — the first
call against a given field with no existing static index makes RavenDB create
an Auto-Index for it. See README's "RavenDB Features Used" table.
"""
import re
from typing import Optional

from ravendb import DocumentStore

from src.db.client import doc_to_dict

_IATA_RE = re.compile(r"^[A-Za-z]{3}$")

# Deliberately simple (English "from X to Y" / Polish "z X do Y") — anything
# more complex (typos, multi-leg, relative references like "back home") just
# fails to match and falls through to the model, which handles it fine at the
# cost of the normal two-call flow. A false negative here only costs tokens,
# never correctness.
_FROM_TO_RE = re.compile(
    r"\bfrom\s+(?P<origin>.+?)\s+to\s+(?P<destination>.+?)(?:[.,!?]|$)",
    re.IGNORECASE,
)
_FROM_TO_PL_RE = re.compile(
    r"\bz\s+(?P<origin>.+?)\s+do\s+(?P<destination>.+?)(?:[.,!?]|$)",
    re.IGNORECASE,
)


def _resolve_one(store: DocumentStore, phrase: str) -> Optional[str]:
    """Resolve a single city/airport phrase to exactly one IATA code, or None
    if it's empty, not found, or ambiguous (2+ matches) -- ambiguity is left
    for the model to ask about, not guessed at."""
    phrase = phrase.strip().strip(".,!?")
    if not phrase:
        return None

    if _IATA_RE.match(phrase):
        with store.open_session() as session:
            doc = session.load(f"airports/{phrase.upper()}")
        return phrase.upper() if doc else None

    with store.open_session() as session:
        matches = [
            doc_to_dict(a)
            for a in session.query_collection("Airports").search("city", phrase).take(3)
        ]
    if len(matches) == 1:
        return matches[0].get("iata")
    return None


def resolve_origin_destination(store: DocumentStore, user_message: str) -> Optional[dict]:
    """{"origin": IATA, "destination": IATA} for an unambiguous "from X to Y"
    (or "z X do Y") message, or None otherwise -- including when the shape
    doesn't match, or either side doesn't resolve to exactly one airport."""
    match = _FROM_TO_RE.search(user_message) or _FROM_TO_PL_RE.search(user_message)
    if not match:
        return None

    origin = _resolve_one(store, match.group("origin"))
    if not origin:
        return None
    destination = _resolve_one(store, match.group("destination"))
    if not destination:
        return None
    return {"origin": origin, "destination": destination}
