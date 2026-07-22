import inspect
import logging

from src.tools.get_live_prices import get_live_prices
from src.tools.get_user_profile import get_user_profile
from src.tools.save_conversation import update_constraints
from src.tools.search_routes import search_routes
from src.tools.update_user_profile import update_user_profile

log = logging.getLogger(__name__)

_TOOLS = {
    "search_routes": search_routes,
    "get_live_prices": get_live_prices,
    "get_user_profile": get_user_profile,
    "update_constraints": update_constraints,
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
    try:
        return await fn(**tool_input)
    except TypeError as exc:
        # The model is an external actor from this boundary's point of view — a
        # malformed/unsupported argument (e.g. a param that only exists on a
        # different tool) must not crash the whole turn. Surface it as a tool
        # result instead, so the model can see the accepted parameters and retry.
        accepted = [p for p in inspect.signature(fn).parameters if p != "self"]
        log.warning("Tool %s rejected input %s: %s", name, tool_input, exc)
        return {"error": str(exc), "accepted_parameters": accepted}
    except Exception as exc:
        # Same external-actor boundary as above, but for runtime failures rather than
        # bad arguments — missing config (e.g. TRAVELPAYOUTS_TOKEN), an upstream API
        # error, a RavenDB hiccup. search_routes already guards its *internal*
        # get_live_prices calls this way (see its try/except Exception blocks); this
        # is the same guard for a tool dispatched directly from the model, which had
        # no such safety net and would otherwise 500 the whole /chat request.
        log.exception("Tool %s failed with input %s", name, tool_input)
        return {"error": f"{name} failed: {exc}"}
