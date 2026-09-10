"""
OpenAI tool schemas for the agent's tools.
Keep descriptions tight — they count against the token budget.
"""
import re

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_routes",
            "description": (
                "Search RavenDB for flight routes (direct or hidden city), auto-refreshing "
                "stale/missing data via Travelpayouts for a single destination or hidden-city "
                "fare. budget_max, budget_currency, countries_of_interest, carry_on_only "
                "auto-fill from saved preferences unless overridden. No direct route → returns "
                "connecting_hubs or nearby_alternatives (never both, see system rules for how "
                "to present each)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {
                        "type": "string",
                        "description": "Departure airport IATA code (e.g. WAW)",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Endpoint destination IATA code. Omit to search all routes from origin.",
                    },
                    "real_destination": {
                        "type": "string",
                        "description": (
                            "For hidden city search: IATA code of the city the user actually "
                            "wants to reach (e.g. LHR). Returns routes where this airport "
                            "appears as a hub."
                        ),
                    },
                    "carry_on_only": {
                        "type": "boolean",
                        "description": "If true, apply checked-baggage risk penalty to hidden city candidates.",
                    },
                    "budget_max": {
                        "type": "number",
                        "description": "Exclude routes priced above this amount.",
                    },
                    "budget_currency": {
                        "type": "string",
                        "description": "Currency for budget_max (e.g. 'PLN'). Only filters when it matches the route's price currency.",
                    },
                    "countries_of_interest": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Only return routes whose destination country is in this list.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of routes to return (default 5).",
                    },
                },
                "required": ["origin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_live_prices",
            "description": (
                "Fetch live price(s) from Travelpayouts directly. search_routes already "
                "does this itself for a single destination or a hidden-city direct fare — "
                "call this tool yourself only for an 'anywhere from origin' search (omit "
                "destination; returns cheapest destinations, up to max_results) or to force "
                "a refresh search_routes didn't trigger."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Departure IATA code"},
                    "destination": {
                        "type": "string",
                        "description": "Endpoint IATA code. Omit for an 'anywhere from origin' search.",
                    },
                    "date": {
                        "type": "string",
                        "description": "Departure date YYYY-MM-DD. Omit to use the nearest available.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max destinations to return when destination is omitted (default 5).",
                    },
                },
                "required": ["origin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_profile",
            "description": "Load user preferences and saved constraints from RavenDB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_user_profile",
            "description": (
                "Save durable user preferences to RavenDB: name, baggage style, home/"
                "departure airports, countries/destinations of interest, budget, preferred "
                "airlines, loyalty programs. List fields merge with what's already saved — "
                "pass only the new values just learned, not the full list. Not for "
                "trip-specific constraints — use update_constraints for those."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "name": {
                        "type": "string",
                        "description": "What the user wants to be called (e.g. 'Alex').",
                    },
                    "carry_on_only": {
                        "type": "boolean",
                        "description": "User always travels without checked baggage.",
                    },
                    "max_stops": {
                        "type": "integer",
                        "description": "Preferred maximum number of stops across all trips.",
                    },
                    "home_airport": {
                        "type": "string",
                        "description": "User's default departure airport IATA code (e.g. WAW).",
                    },
                    "departure_airports": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New departure airport IATA codes to add (e.g. ['WAW', 'KRK']).",
                    },
                    "countries_of_interest": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New countries the user is interested in flying to (e.g. ['China']).",
                    },
                    "destinations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New specific destination cities/airports to add (e.g. ['Chongqing', 'Beijing']).",
                    },
                    "budget_max": {
                        "type": "number",
                        "description": "User's maximum budget for a trip.",
                    },
                    "budget_currency": {
                        "type": "string",
                        "description": "Currency for budget_max (e.g. 'PLN', 'USD').",
                    },
                    "preferred_airlines": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New airlines the user prefers to add (e.g. ['LOT', 'Lufthansa']).",
                    },
                    "loyalty_programs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New frequent flyer programs to add (e.g. ['Miles & More']).",
                    },
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_constraints",
            "description": (
                "Save a trip-specific constraint for this session only (not a durable "
                "profile preference) — e.g. carry-on only for this trip, max 1 stop. "
                "Call only when the user actually states one this turn; skip otherwise. "
                "The turn itself is saved automatically — you don't need a tool call for that."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "session_id": {"type": "string"},
                    "constraints": {
                        "type": "object",
                        "description": "Keys: carry_on_only (bool), max_stops (int), max_duration_min (int).",
                    },
                },
                "required": ["user_id", "session_id", "constraints"],
            },
        },
    },
]

# search_routes/get_live_prices are needed on essentially every turn.
# get_user_profile, update_user_profile, and update_constraints together are
# ~900 of the ~1400 fixed per-call tokens above, but only fire when the user
# is actually stating or re-reading a preference/constraint — most turns are
# a plain search and never touch them. Excluding their schemas on those turns
# is the single biggest "fewer tools per call" lever available here.
_CORE_TOOL_NAMES = ("search_routes", "get_live_prices")
_PREFERENCE_TOOL_NAMES = ("get_user_profile", "update_user_profile", "update_constraints")

# Deliberately broad/bilingual (this project's users write English and Polish) —
# a false positive just costs some extra tokens; a false negative silently
# drops a preference the user asked to be remembered, which is worse. Widen
# this list rather than narrow it if a real preference statement gets missed.
_PREFERENCE_HINT_RE = re.compile(
    r"\b("
    r"remember|prefer|preference|budget|carry.?on|checked.?bag|luggage|baggage|"
    r"stops?|airline|loyalty|miles|frequent.?flyer|my name|call me|"
    r"home airport|departure airport|countr(y|ies)|destinations?|"
    r"zapami[eę]taj|prefer(uj|encj)\w*|bud[zż]et|baga[zż]|przesiad\w*|lini\w*|"
    r"program\w*|mil[ea]\w*|nazywam|m[oó]wi[eć] do mnie|lotnisko|kraj\w*"
    r")\b",
    re.IGNORECASE,
)


def select_tools(user_message: str) -> list[dict]:
    """Tool schemas to send for this turn's user_message. Always includes the
    search tools; includes the preference-write tools only when the message
    hints at a durable preference or trip constraint. See module docstring
    above _PREFERENCE_HINT_RE for the false-positive/false-negative trade-off."""
    selected = [t for t in TOOL_DEFINITIONS if t["function"]["name"] in _CORE_TOOL_NAMES]
    if _PREFERENCE_HINT_RE.search(user_message):
        selected += [t for t in TOOL_DEFINITIONS if t["function"]["name"] in _PREFERENCE_TOOL_NAMES]
    return selected
