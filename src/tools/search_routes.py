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
    in SYSTEM_PROMPT: this only surfaces distance/train info, it does not check
    whether the alternative actually has route data.
    """
    with store.open_session() as session:
        doc = session.load(f"airports/{iata}")
    if doc is None:
        return []

    nearby = doc_to_dict(doc).get("nearby", [])
    if not nearby:
        return []

    names = load_airport_names(store, [n["iata"] for n in nearby])
    alternatives = []
    for n in nearby:
        entry = {
            "airport": n["iata"],
            "distance_km": n["distance_km"],
            "train": n["train"],
        }
        if n["iata"] in names:
            entry["city"] = names[n["iata"]]["city"]
        alternatives.append(entry)
    return alternatives


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
                direct_dict = doc_to_dict(direct_doc)
                price_direct = direct_dict.get("typical_price", {}).get("min")
                direct_price_stale = _is_stale(direct_dict.get("last_updated"))

        query = session.query(collection_name="Routes").where_equals("origin", origin)

        if destination:
            query = query.where_equals("destination", destination.upper())
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
        # Report nearby alternatives for BOTH sides explicitly, keyed by role, rather
        # than guessing which single side is "the disconnected one" — a route count
        # on either airport (e.g. leftover live-price cache entries from an unrelated
        # earlier search) is not a reliable signal for that, and picking the wrong
        # side produces a nonsensical answer (e.g. "try a nearby airport to Warsaw"
        # when the actual gap is on the origin side). Keying by role also stops the
        # model from attributing an alternative to the wrong side.
        other_side = destination or real_destination
        near_origin = _nearby_alternatives(store, origin)
        near_destination = _nearby_alternatives(store, other_side.upper()) if other_side else []
        if near_origin or near_destination:
            result["nearby_alternatives"] = {
                "near_origin": near_origin,
                "near_destination": near_destination,
            }
            result["note"] = (
                "No routes found. near_origin lists alternative DEPARTURE airports "
                "near the origin; near_destination lists alternative ARRIVAL airports "
                "near the requested destination. These are the ONLY valid "
                "alternatives — never mention any other airport. Ask the user before "
                "searching one of them; never substitute silently."
            )
    return result
