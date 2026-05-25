"""
search_routes tool — queries RavenDB for flight routes.

Two search modes:
  destination      → find routes from origin to that specific endpoint
  real_destination → find hidden city candidates where that city is a hub

Results are serialised compactly to stay within the 800-token tool budget.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from src.db.client import doc_to_dict, get_store
from src.hidden_city.scorer import RiskFactor, score_candidate

log = logging.getLogger(__name__)

_STALE_HOURS = 2.0
_DEFAULT_MAX_RESULTS = 5


def _is_stale(last_updated_str: str | None) -> bool:
    if not last_updated_str:
        return True
    try:
        ts = datetime.fromisoformat(last_updated_str.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - ts
        return age.total_seconds() > _STALE_HOURS * 3600
    except ValueError:
        return True


_CHECKED_BAGGAGE_MULTIPLIER = 0.2  # mirrors RiskFactor.CHECKED_BAGGAGE in scorer.py


def _adjusted_hidden_score(base_score: float, carry_on_only: bool) -> float:
    """Apply checked-baggage risk to the stored base score (which has no risks baked in)."""
    if not carry_on_only:
        return base_score
    return round(base_score * _CHECKED_BAGGAGE_MULTIPLIER, 3)


async def search_routes(
    origin: str,
    destination: Optional[str] = None,
    real_destination: Optional[str] = None,
    carry_on_only: bool = False,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict:
    store = get_store()

    with store.open_session() as session:
        query = session.query(collection_name="Routes").where_equals("origin", origin.upper())

        if destination:
            query = query.where_equals("destination", destination.upper())
        elif real_destination:
            # Hidden city search: routes where real_destination is a layover hub
            query = query.where_equals("hidden_city_via", real_destination.upper())
            query = query.where_greater_than("hidden_city_score", 0.5)

        raw_results = [doc_to_dict(r) for r in query.take(max_results)]

    routes = []
    for r in raw_results:
        stale = _is_stale(r.get("last_updated"))
        route_entry: dict = {
            "from": r.get("origin"),
            "to": r.get("destination"),
            "via": r.get("hubs", []),
            "price_usd": r.get("typical_price", {}),
            "stale": stale,
        }

        base_score = r.get("hidden_city_score", 0.0)
        if base_score > 0.5:
            adj_score = _adjusted_hidden_score(base_score, carry_on_only)
            risks = []
            if carry_on_only:
                risks.append("checked_baggage")
            route_entry["hidden_city"] = {
                "via": r.get("hidden_city_via"),
                "decoy_to": r.get("hidden_city_decoy"),
                "score": round(adj_score, 2),
                "risks": risks,
            }

        routes.append(route_entry)

    return {"routes": routes, "count": len(routes)}
