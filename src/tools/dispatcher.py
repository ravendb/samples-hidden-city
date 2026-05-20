import logging

from src.tools.get_live_price import get_live_price
from src.tools.get_user_profile import get_user_profile
from src.tools.save_conversation import save_conversation
from src.tools.search_routes import search_routes

log = logging.getLogger(__name__)

_TOOLS = {
    "search_routes": search_routes,
    "get_live_price": get_live_price,
    "get_user_profile": get_user_profile,
    "save_conversation": save_conversation,
}


async def dispatch_tool(name: str, tool_input: dict) -> dict:
    fn = _TOOLS.get(name)
    if fn is None:
        raise ValueError(f"Unknown tool: {name!r}")
    log.info("Dispatching tool %s with input %s", name, tool_input)
    return await fn(**tool_input)
