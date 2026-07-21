"""
Travelpayouts /v2/prices/latest — bulk cheapest prices from an origin.
Called by the CronJob scraper every 6h. On cache miss, get_live_prices tool
calls Travelpayouts /v1/prices/cheap instead (see src/tools/get_live_prices.py).

/v2/prices/latest exposes `number_of_changes` (a transfer count) but never the
actual connecting airport codes — Travelpayouts only reveals real itinerary
segments through the separate real-time Flight Search API
(/v1/flight_search + /v1/flight_search_results), which is search+poll and far
more expensive per call. fetch_cheapest_from() therefore returns the transfer
count alongside each route so the caller (src/scraper/run.py) can decide which
routes are worth a hub lookup via fetch_hubs_for_route() below.
"""
import asyncio
import hashlib
from datetime import datetime, timezone

import httpx

from src.db.models import RouteDocument, TypicalPrice

_BASE = "https://api.travelpayouts.com"
_PRICE_SPREAD = 0.15  # ±15% around the scraped price to fill min/max

_SEARCH_INIT_URL = f"{_BASE}/v1/flight_search"
_SEARCH_RESULTS_URL = f"{_BASE}/v1/flight_search_results"
_SEARCH_POLL_INTERVAL_S = 2.0
_SEARCH_POLL_TIMEOUT_S = 20.0


async def fetch_cheapest_from(
    origin: str,
    token: str,
    currency: str = "usd",
    limit: int = 1000,
) -> list[tuple[RouteDocument, int]]:
    """
    Returns up to `limit` cheapest routes from `origin`, paired with the
    `number_of_changes` (transfer count) Travelpayouts reports for that fare.
    `hubs` on the returned RouteDocument is always [] here — see module
    docstring; the caller fills it in via fetch_hubs_for_route() for routes
    with transfers > 0.
    """
    params = {
        "origin": origin,
        "token": token,
        "currency": currency,
        "limit": limit,
        "period_type": "month",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{_BASE}/v2/prices/latest", params=params)
        response.raise_for_status()

    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Travelpayouts error: {payload}")

    now = datetime.now(timezone.utc)
    routes: list[tuple[RouteDocument, int]] = []
    data = payload.get("data", [])

    # API returns either a list of objects or a dict keyed by destination
    if isinstance(data, dict):
        items = [{"destination": dest, **info} for dest, info in data.items()]
    else:
        items = data

    for item in items:
        dest = item.get("destination")
        price_raw = item.get("price") or item.get("value")
        if not dest or not price_raw:
            continue
        price = float(price_raw)
        depart_date = item.get("depart_date") or item.get("departure_at", "")[:10] or None
        transfers = int(item.get("number_of_changes") or item.get("transfers") or 0)
        routes.append(
            (
                RouteDocument(
                    origin=origin,
                    destination=dest,
                    hubs=[],
                    typical_price=TypicalPrice(
                        min=round(price * (1 - _PRICE_SPREAD), 2),
                        max=round(price * (1 + _PRICE_SPREAD), 2),
                        currency=currency.upper(),
                    ),
                    duration_avg_min=0,
                    depart_date=depart_date,
                    last_updated=now,
                ),
                transfers,
            )
        )

    return routes


def _signature(
    token: str, marker: str, host: str, locale: str, origin: str, destination: str, date: str
) -> str:
    """
    Per Travelpayouts spec: md5(token + ":" + sorted-param-values joined by ":").
    Param order: host, locale, marker, then per-segment (date, destination, origin),
    then passenger counts (adults, children, infants), then trip_class.
    """
    parts = [host, locale, marker, date, destination, origin, "1", "0", "0", "Y"]
    raw = token + ":" + ":".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _cheapest_proposal(proposals: list[dict]) -> dict | None:
    def price_of(proposal: dict) -> float:
        terms = proposal.get("terms", {})
        if not terms:
            return float("inf")
        return min(t.get("unified_price", t.get("price", float("inf"))) for t in terms.values())

    return min(proposals, key=price_of, default=None)


def _extract_hubs(proposal: dict, origin: str, destination: str) -> list[str]:
    """
    proposal["segment"] holds one entry per direction; each direction is a list
    of flight legs in order. Every leg's arrival airport except the last one is
    an intermediate hub.
    """
    hubs: list[str] = []
    for direction in proposal.get("segment", []):
        legs = direction if isinstance(direction, list) else direction.get("flight", [])
        for leg in legs[:-1]:
            arrival = leg.get("arrival")
            if arrival and arrival not in (origin, destination) and arrival not in hubs:
                hubs.append(arrival)
    return hubs


async def fetch_hubs_for_route(
    origin: str,
    destination: str,
    date: str,
    token: str,
    marker: str,
    host: str = "samples-hidden-city.local",
    locale: str = "en",
) -> list[str]:
    """
    Runs a real-time Flight Search (search + poll) to discover the actual
    connecting airports for the cheapest itinerary on this route. Returns []
    on no results, timeout, or if the cheapest proposal turns out direct.

    NOTE: response field names here (segment/flight/arrival, terms/unified_price)
    follow the documented Aviasales proposal shape but have not been verified
    against a live token — confirm against a real response and adjust
    _extract_hubs/_cheapest_proposal if the field names differ.
    """
    signature = _signature(token, marker, host, locale, origin, destination, date)
    body = {
        "signature": signature,
        "marker": marker,
        "host": host,
        "user_ip": "127.0.0.1",
        "locale": locale,
        "trip_class": "Y",
        "passengers": {"adults": 1, "children": 0, "infants": 0},
        "segments": [{"origin": origin, "destination": destination, "date": date}],
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        start = await client.post(_SEARCH_INIT_URL, json=body)
        start.raise_for_status()
        search_id = start.json().get("search_id")
        if not search_id:
            return []

        elapsed = 0.0
        proposals: list[dict] = []
        while elapsed < _SEARCH_POLL_TIMEOUT_S:
            await asyncio.sleep(_SEARCH_POLL_INTERVAL_S)
            elapsed += _SEARCH_POLL_INTERVAL_S
            poll = await client.get(_SEARCH_RESULTS_URL, params={"uuid": search_id})
            poll.raise_for_status()
            chunks = poll.json()
            if not chunks:
                continue
            for chunk in chunks:
                proposals.extend(chunk.get("proposals", []))
            if proposals or any(chunk.get("search_completed") for chunk in chunks):
                break

    if not proposals:
        return []

    cheapest = _cheapest_proposal(proposals)
    return _extract_hubs(cheapest, origin, destination) if cheapest else []
