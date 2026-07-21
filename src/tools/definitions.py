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
                "Search RavenDB for flight routes (direct or hidden city). Call before "
                "get_live_prices. Returns stale=true if data is >2h old. budget_max, "
                "budget_currency, countries_of_interest, carry_on_only auto-fill from saved "
                "preferences unless overridden. If no direct route is found, returns EITHER "
                "connecting_hubs (up to 3 via-hub suggestions, each with leg1_price_usd, "
                "leg2_price_usd, total_price_usd_min — two separately cached routes, NOT a "
                "single fare and NOT a hidden city opportunity) OR nearby_alternatives "
                "(unsearched near_origin/near_destination lists, each with airport/city/"
                "country/distance_km, found via geographic proximity search) — never both. "
                "Ask before searching a nearby_alternatives airport, never mix the two lists."
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
                "Fetch live price(s) from Travelpayouts when search_routes returns "
                "stale=true, no results, or has_schedule=false. Pass destination for a "
                "single-route lookup, or omit it for an 'anywhere from origin' search "
                "(cheapest destinations, up to max_results). Returns price and departure "
                "date per route."
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
