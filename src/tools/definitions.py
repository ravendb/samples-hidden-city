"""
OpenAI tool schemas for the agent's tools.
Keep descriptions tight — they count against the token budget.
"""

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_routes",
            "description": (
                "Search RavenDB for flight routes. Supports direct and hidden city lookups. "
                "Always call this before get_live_prices. Returns stale=true if data is >2h old. "
                "budget_max, budget_currency, countries_of_interest, and carry_on_only are "
                "auto-filled from the user's saved preferences if you omit them — only pass "
                "them yourself to override for this one search. "
                "If no routes are found, may return nearby_alternatives with two separate "
                "lists — near_origin (alternative departure airports) and near_destination "
                "(alternative arrival airports), each with airport/city/distance_km/train. "
                "Neither has been searched — ask the user before searching one, never "
                "substitute silently, and never mix up which list an airport came from."
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
                            "For hidden city search: the city the user actually wants to reach. "
                            "Returns routes where this airport appears as a hub."
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
                "Fetch live price(s) from Travelpayouts. "
                "Use when search_routes returns stale=true, no results, or has_schedule=false. "
                "Pass destination for a single-route lookup. Omit destination for an "
                "'anywhere from origin' search — returns the cheapest destinations found, "
                "up to max_results. Returns price and departure date per route."
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
                "Save durable user preferences to RavenDB. Call when the user expresses "
                "a preference that should carry across sessions: their name, baggage style, "
                "home/departure airports, countries or destinations they're interested in, "
                "budget, preferred airlines, loyalty programs. List fields are merged with "
                "what's already saved — pass only the new values just learned, not the full list. "
                "Do NOT use for trip-specific constraints — use save_conversation for those."
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
