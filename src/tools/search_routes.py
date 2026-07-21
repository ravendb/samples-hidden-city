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

from src.db.client import doc_to_dict, get_store, load_airport_names
from src.db.geo import haversine_km
from src.hidden_city.scorer import HiddenCityCandidate, RiskFactor

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
_MAX_FETCH_BUFFER = 30  # cap on how many extra docs we pull when post-filtering

_NEARBY_MIN_SIMILARITY = 0.95  # ~2000km cutoff, see src/db/geo.py docstring
_NEARBY_CANDIDATES = 20
_NEARBY_MAX_RESULTS = 3

_HUB_JOIN_FETCH = 50
_HUB_JOIN_MAX_RESULTS = 3


def _adjusted_hidden_score(base_score: float, carry_on_only: bool) -> float:
    """Apply checked-baggage risk to a route's own precomputed best-hub score.

    Used only for the informational hidden-city annotation on a plain
    destination search — that stored score/via reflects a single hub (the best
    one for that route), which is fine as a "by the way" flag. The
    real_destination search path below scores every matching hub fresh instead,
    since relying on the stored field there would silently drop valid
    candidates whenever the requested hub isn't the route's single best one.
    """
    if not carry_on_only:
        return base_score
    return round(base_score * _CHECKED_BAGGAGE_MULTIPLIER, 3)


def _nearby_alternatives(store, iata: str) -> list[dict]:
    """Nearby airports for `iata`, for the caller to offer as a question — never to
    substitute automatically. See the "ask before nearby-airport substitution" rule
    in SYSTEM_PROMPT.

    Computed via RavenDB vector search over each airport's location_vector (a
    great-circle unit-vector projection of its coordinates, see src/db/geo.py) —
    this surfaces geographically close airports dynamically instead of relying
    on a hand-curated list, so it works for any airport, not just the handful
    that had a "nearby" entry manually filled in. With ~96 airports in this
    demo a brute-force scan would be equally instant — this exercises RavenDB's
    vector search feature for the demo, not solving a real scale problem.
    """
    with store.open_session() as session:
        origin_doc = session.load(f"airports/{iata}")
        if origin_doc is None:
            return []
        origin_vector = origin_doc.get("location_vector")
        origin_coords = origin_doc.get("coordinates") or {}
        if not origin_vector or "lat" not in origin_coords or "lng" not in origin_coords:
            return []

        candidates = [
            doc_to_dict(c)
            for c in session.query_collection("Airports")
            .vector_search(
                "location_vector",
                origin_vector,
                minimum_similarity=_NEARBY_MIN_SIMILARITY,
                number_of_candidates=_NEARBY_CANDIDATES,
            )
            .where_not_equals("iata", iata)
        ]

    scored = []
    for c in candidates:
        coords = c.get("coordinates") or {}
        if "lat" not in coords or "lng" not in coords:
            continue
        distance_km = round(
            haversine_km(origin_coords["lat"], origin_coords["lng"], coords["lat"], coords["lng"])
        )
        scored.append(
            {
                "airport": c.get("iata"),
                "city": c.get("city"),
                "country": c.get("country"),
                "distance_km": distance_km,
            }
        )

    scored.sort(key=lambda e: e["distance_km"])
    return scored[:_NEARBY_MAX_RESULTS]


def _connecting_hub_candidates(store, origin: str, destination: str) -> list[dict]:
    """Deterministic origin->X->destination search for when no direct route
    exists: X is any airport reachable from origin AND that itself reaches
    destination, per real cached route documents — not a geography guess.

    Distinct from the hidden-city `hubs` field, which represents a single
    ticket priced end-to-end where a stop happens to be scheduled. This
    stitches together two independently-priced route documents with no
    guaranteed single booking, so results are returned as `connecting_hubs`,
    never mixed into `hidden_city`.
    """
    with store.open_session() as session:
        from_origin = {
            doc_to_dict(r).get("destination")
            for r in session.query_collection("Routes").where_equals("origin", origin).take(_HUB_JOIN_FETCH)
        }
        to_destination = {
            doc_to_dict(r).get("origin")
            for r in session.query_collection("Routes").where_equals("destination", destination).take(_HUB_JOIN_FETCH)
        }
        hub_codes = (from_origin & to_destination) - {origin, destination, None}
        if not hub_codes:
            return []

        candidates = []
        for hub in hub_codes:
            leg1 = session.load(f"routes/{origin}-{hub}")
            leg2 = session.load(f"routes/{hub}-{destination}")
            if leg1 is None or leg2 is None:
                continue
            leg1_price = leg1.get("typical_price", {})
            leg2_price = leg2.get("typical_price", {})
            if leg1_price.get("min") is None or leg2_price.get("min") is None:
                continue
            candidates.append(
                {
                    "via": hub,
                    "leg1_price_usd": leg1_price,
                    "leg2_price_usd": leg2_price,
                    "total_price_usd_min": round(leg1_price["min"] + leg2_price["min"], 2),
                    "stale": _is_stale(leg1.get("last_updated")) or _is_stale(leg2.get("last_updated")),
                }
            )

    names = load_airport_names(store, [c["via"] for c in candidates])
    for c in candidates:
        if c["via"] in names:
            c["city"] = names[c["via"]]["city"]

    candidates.sort(key=lambda c: c["total_price_usd_min"])
    return candidates[:_HUB_JOIN_MAX_RESULTS]


def _within_budget(price: dict, budget_max: float, budget_currency: Optional[str]) -> bool:
    """Exclude routes priced above budget_max. Only compares when currencies match —
    there's no FX conversion here, so a mismatched currency is left unfiltered rather
    than guessed at."""
    price_max = price.get("max")
    if price_max is None:
        return True
    price_currency = (price.get("currency") or "USD").strip().lower()
    wanted_currency = (budget_currency or "USD").strip().lower()
    if price_currency != wanted_currency:
        return True
    return price_max <= budget_max


async def search_routes(
    origin: str,
    destination: Optional[str] = None,
    real_destination: Optional[str] = None,
    carry_on_only: bool = False,
    budget_max: Optional[float] = None,
    budget_currency: Optional[str] = None,
    countries_of_interest: Optional[list[str]] = None,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict:
    store = get_store()
    origin = origin.upper()
    destination = destination.upper() if destination else None
    real_destination = real_destination.upper() if real_destination else None
    hidden_city_mode = bool(real_destination) and not destination

    post_filtering = budget_max is not None or bool(countries_of_interest) or hidden_city_mode
    fetch_count = min(max_results * 4, _MAX_FETCH_BUFFER) if post_filtering else max_results

    price_direct: Optional[float] = None
    direct_price_stale = False

    with store.open_session() as session:
        if hidden_city_mode:
            direct_doc = session.load(f"routes/{origin}-{real_destination}")
            if direct_doc is not None:
                price_direct = direct_doc.get("typical_price", {}).get("min")
                direct_price_stale = _is_stale(direct_doc.get("last_updated"))

        query = session.query_collection("Routes").where_equals("origin", origin)

        if destination:
            query = query.where_equals("destination", destination)
        elif hidden_city_mode:
            # Match ANY route where real_destination is one of the hubs — not just
            # the route whose stored hidden_city_via happens to be its single best
            # hub, which would silently drop valid candidates via other hubs on the
            # same route (see the module-level docstring on _adjusted_hidden_score).
            query = query.where_equals("hubs", real_destination)

        raw_results = [doc_to_dict(r) for r in query.take(fetch_count)]

    if hidden_city_mode and (price_direct is None or direct_price_stale):
        return {
            "routes": [],
            "count": 0,
            "note": (
                f"No fresh direct price for {origin}->{real_destination} — call "
                "get_live_prices for that route first, then retry the hidden city search."
            ),
        }

    codes: set[str] = set()
    for r in raw_results:
        codes.add(r.get("origin"))
        codes.add(r.get("destination"))
        codes.update(r.get("hubs", []))
    airport_names = load_airport_names(store, list(codes))

    wanted_countries = {c.strip().lower() for c in (countries_of_interest or [])}

    routes = []
    for r in raw_results:
        if len(routes) >= max_results:
            break

        from_code = r.get("origin")
        to_code = r.get("destination")

        if wanted_countries:
            to_country = airport_names.get(to_code, {}).get("country")
            if to_country and to_country.strip().lower() not in wanted_countries:
                continue

        if budget_max is not None and not _within_budget(
            r.get("typical_price", {}), budget_max, budget_currency
        ):
            continue

        hidden_city_info = None
        if hidden_city_mode:
            price_hidden = r.get("typical_price", {}).get("min")
            if price_hidden is None:
                continue
            risks = [RiskFactor.CHECKED_BAGGAGE] if carry_on_only else []
            candidate = HiddenCityCandidate(
                origin=from_code,
                real_destination=real_destination,
                decoy_destination=to_code,
                price_direct=price_direct,
                price_hidden=price_hidden,
                risks=risks,
            )
            if not candidate.should_surface:
                continue
            hidden_city_info = {
                "via": real_destination,
                "decoy_to": to_code,
                "score": round(candidate.score, 2),
                "risks": [risk.value for risk in risks],
            }

        stale = _is_stale(r.get("last_updated"))
        route_entry: dict = {
            "from": from_code,
            "to": to_code,
            "via": r.get("hubs", []),
            "price_usd": r.get("typical_price", {}),
            "stale": stale,
        }
        if from_code in airport_names:
            route_entry["from_city"] = airport_names[from_code]["city"]
        if to_code in airport_names:
            route_entry["to_city"] = airport_names[to_code]["city"]
        if r.get("depart_date"):
            route_entry["depart_date"] = r["depart_date"]
        if r.get("depart_time"):
            route_entry["depart_time"] = r["depart_time"]
        if r.get("arrive_time"):
            route_entry["arrive_time"] = r["arrive_time"]
        if r.get("duration_avg_min"):
            h, m = divmod(r["duration_avg_min"], 60)
            route_entry["duration"] = f"{h}h {m:02d}m" if h else f"{m}m"
        route_entry["has_schedule"] = bool(r.get("depart_time"))

        if hidden_city_info is not None:
            route_entry["hidden_city"] = hidden_city_info
        else:
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

    result: dict = {"routes": routes, "count": len(routes)}
    if not routes:
        connecting_hubs = []
        if destination and not hidden_city_mode:
            connecting_hubs = _connecting_hub_candidates(store, origin, destination)

        if connecting_hubs:
            result["connecting_hubs"] = connecting_hubs
            result["note"] = (
                "No direct route found. connecting_hubs lists up to 3 via-hub "
                "suggestions, each backed by two separately cached routes "
                "(origin->hub, hub->destination) — NOT a single fare and NOT a "
                "hidden city opportunity. Present each as an informational "
                "'you could fly via X' suggestion, stating both legs' prices."
            )
        else:
            # Report nearby alternatives for BOTH sides explicitly, keyed by role, rather
            # than guessing which single side is "the disconnected one" — a route count
            # on either airport (e.g. leftover live-price cache entries from an unrelated
            # earlier search) is not a reliable signal for that, and picking the wrong
            # side produces a nonsensical answer (e.g. "try a nearby airport to Warsaw"
            # when the actual gap is on the origin side). Keying by role also stops the
            # model from attributing an alternative to the wrong side.
            other_side = destination or real_destination
            near_origin = _nearby_alternatives(store, origin)
            near_destination = _nearby_alternatives(store, other_side) if other_side else []
            if near_origin or near_destination:
                result["nearby_alternatives"] = {
                    "near_origin": near_origin,
                    "near_destination": near_destination,
                }
                result["note"] = (
                    "No routes found. near_origin lists alternative DEPARTURE airports "
                    "near the origin; near_destination lists alternative ARRIVAL airports "
                    "near the requested destination — both computed by geographic "
                    "proximity search. These are the ONLY valid alternatives — never "
                    "mention any other airport. Ask the user before searching one of "
                    "them; never substitute silently."
                )
    return result
