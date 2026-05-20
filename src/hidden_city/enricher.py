"""
Computes hidden_city_score for route documents by cross-referencing
direct routes vs routes where the real destination appears as a hub.

Risks are NOT applied here — they depend on user context (checked baggage,
return ticket) which is only known at query time in the agent tool.
The stored score is the "base" score using savings percentage only.
"""
from src.db.models import RouteDocument
from src.hidden_city.scorer import HiddenCityCandidate


def enrich_hidden_city(routes: list[RouteDocument]) -> list[RouteDocument]:
    """
    For each route, check if any hub in its itinerary is cheaper to reach
    via this route than by flying there directly. Updates hidden_city_* fields.
    Routes without hub information are returned unchanged.
    """
    by_dest: dict[tuple[str, str], RouteDocument] = {
        (r.origin, r.destination): r for r in routes
    }

    result: list[RouteDocument] = []
    for route in routes:
        best_score = 0.0
        best_via: str | None = None
        best_decoy: str | None = None

        for hub in route.hubs:
            direct = by_dest.get((route.origin, hub))
            if direct is None:
                continue

            candidate = HiddenCityCandidate(
                origin=route.origin,
                real_destination=hub,
                decoy_destination=route.destination,
                price_direct=direct.typical_price.min,
                price_hidden=route.typical_price.min,
                risks=[],
            )

            if candidate.score > best_score:
                best_score = candidate.score
                best_via = hub
                best_decoy = route.destination

        result.append(
            route.model_copy(
                update={
                    "hidden_city_score": best_score,
                    "hidden_city_via": best_via,
                    "hidden_city_decoy": best_decoy,
                }
            )
        )

    return result
