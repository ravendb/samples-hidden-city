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
                "Use only when search_routes returns stale=true or no results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Departure IATA code"},
                    "destination": {"type": "string", "description": "Endpoint IATA code"},
                    "date": {
                        "type": "string",
                        "description": "Departure date YYYY-MM-DD",
                    },
                    "route_type": {
                        "type": "string",
                        "enum": ["direct", "hidden_city"],
                        "description": "direct → Amadeus, hidden_city → Kiwi Tequila",
                    },
                },
                "required": ["origin", "destination", "date", "route_type"],
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
