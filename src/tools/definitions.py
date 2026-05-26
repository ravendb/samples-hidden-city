"""
OpenAI tool schemas for the four agent tools.
Keep descriptions tight — they count against the token budget.
"""

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_routes",
            "description": (
                "Search RavenDB for flight routes. Supports direct and hidden city lookups. "
                "Always call this before get_live_price. Returns stale=true if data is >2h old."
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
            "name": "get_live_price",
            "description": (
                "Fetch a live price from Kiwi Tequila (hidden city routes) or Amadeus (direct routes). "
                "Use when search_routes returns stale=true, no results, or has_schedule=false. "
                "Returns departure date, departure/arrival times, and duration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Departure IATA code"},
                    "destination": {"type": "string", "description": "Endpoint IATA code"},
                    "date": {
                        "type": "string",
                        "description": "Departure date YYYY-MM-DD. Omit to use the nearest available.",
                    },
                    "route_type": {
                        "type": "string",
                        "enum": ["direct", "hidden_city"],
                        "description": "direct → Amadeus, hidden_city → Kiwi Tequila",
                    },
                },
                "required": ["origin", "destination", "route_type"],
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
                "a preference that should carry across sessions: always carry-on only, "
                "home airport, preferred airlines, loyalty programs. "
                "Do NOT use for trip-specific constraints — use save_conversation for those."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
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
                    "preferred_airlines": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Airlines the user prefers (e.g. ['LOT', 'Lufthansa']).",
                    },
                    "loyalty_programs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Frequent flyer programs the user holds (e.g. ['Miles & More']).",
                    },
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_conversation",
            "description": (
                "Persist the current turn to RavenDB. Call after your final response. "
                "Include any constraints the user expressed in this turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "session_id": {"type": "string"},
                    "user_message": {"type": "string"},
                    "assistant_response": {"type": "string"},
                    "constraints": {
                        "type": "object",
                        "description": (
                            "Constraint updates from this turn. "
                            "Keys: carry_on_only (bool), max_stops (int), max_duration_min (int)."
                        ),
                    },
                },
                "required": ["user_id", "session_id", "user_message", "assistant_response"],
            },
        },
    },
]
