import logging

from src.tools.get_live_price import get_live_price
from src.tools.get_user_profile import get_user_profile
from src.tools.save_conversation import save_conversation
from src.tools.search_routes import search_routes
from src.tools.update_user_profile import update_user_profile

log = logging.getLogger(__name__)

_TOOLS = {
    "search_routes": search_routes,
    "get_live_price": get_live_price,
    "get_user_profile": get_user_profile,
    "save_conversation": save_conversation,
    "update_user_profile": update_user_profile,
}


_SEARCH_PREFERENCE_DEFAULTS = ("carry_on_only", "budget_max", "budget_currency", "countries_of_interest")


def _apply_search_preferences(tool_input: dict, preferences: dict) -> dict:
    """Fill search_routes filters from saved preferences when the model didn't pass
    them itself — enforces preference filtering deterministically instead of relying
    on the model to remember and apply it consistently on every call."""
    merged = dict(tool_input)
    for key in _SEARCH_PREFERENCE_DEFAULTS:
        merged.setdefault(key, preferences.get(key))
    return merged


async def dispatch_tool(name: str, tool_input: dict, preferences: dict | None = None) -> dict:
    fn = _TOOLS.get(name)
    if fn is None:
        raise ValueError(f"Unknown tool: {name!r}")
    if name == "search_routes" and preferences:
        tool_input = _apply_search_preferences(tool_input, preferences)
    log.info("Dispatching tool %s with input %s", name, tool_input)
    return await fn(**tool_input)
