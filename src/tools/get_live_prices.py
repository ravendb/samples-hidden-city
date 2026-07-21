"""
get_live_prices tool — refreshes price(s) from Travelpayouts on cache miss.

Travelpayouts /v1/prices/cheap returns: price, departure date/time, stops count.
It does NOT return arrival time or intermediate hub airports.

Two modes, both going through the same endpoint:
- destination given -> single-route lookup (as before).
- destination omitted -> "anywhere from origin": Travelpayouts returns cheapest
  prices to several destinations from origin in one call, used for the
  "anywhere from home" quick search.

Strategy per destination resolved:
- Load the existing RavenDB route doc to preserve hubs and duration_avg_min.
- Fetch fresh price + depart_time from Travelpayouts.
- Compute arrive_time = depart_time + duration_avg_min (if both known).
- Write back only the price/timing fields; structural data (hubs, duration) unchanged.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from src.db.client import doc_to_dict, get_store, load_airport_names, put_document
from src.db.expiration import expires_at
from src.db.models import RouteDocument, TypicalPrice

log = logging.getLogger(__name__)

_BASE = "https://api.travelpayouts.com"
_PRICE_SPREAD = 0.15
_DEFAULT_MAX_RESULTS = 5


async def _travelpayouts_search(
    origin: str, destination: Optional[str], date: str, max_results: int
) -> list[dict]:
    token = os.environ["TRAVELPAYOUTS_TOKEN"]
    params = {
        "origin": origin,
        "depart_date": date[:7],  # API expects YYYY-MM
        "token": token,
        "currency": "usd",
    }
    if destination:
        params["destination"] = destination

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{_BASE}/v1/prices/cheap", params=params)
        response.raise_for_status()

    payload = response.json()
    if not payload.get("success"):
        return []

    # /v1/prices/cheap nests each destination by number-of-stops:
    # {"AER": {"0": {price, airline, departure_at, ...}, "1": {...}}, ...}
    data = payload.get("data", {})
    if destination:
        by_stops = data.get(destination) or (list(data.values())[0] if data else None)
        entries = [(destination, by_stops)] if by_stops else []
    else:
        entries = list(data.items())[:max_results]

    results = []
    for dest, by_stops in entries:
        if not by_stops:
            continue
        stops_key, info = min(
            by_stops.items(), key=lambda kv: kv[1].get("price", float("inf"))
        )
        depart_at = info.get("departure_at", "")
        results.append(
            {
                "destination": dest,
                "price_usd": float(info.get("price", 0)),
                "depart_date": depart_at[:10] if depart_at else date,
                "depart_time": depart_at[11:16] if len(depart_at) >= 16 else None,
                "stops": int(stops_key) if str(stops_key).isdigit() else 0,
            }
        )
    return results


def _compute_arrive_time(depart_time: str | None, duration_min: int) -> str | None:
    if not depart_time or not duration_min:
        return None
    try:
        base = datetime.strptime(depart_time, "%H:%M")
        arrival = base + timedelta(minutes=duration_min)
        return arrival.strftime("%H:%M")
    except ValueError:
        return None


def _load_existing_route(origin: str, destination: str) -> dict:
    try:
        store = get_store()
        with store.open_session() as session:
            raw = session.load(f"routes/{origin}-{destination}")
        return doc_to_dict(raw) if raw is not None else {}
    except Exception:
        return {}


def _write_to_ravendb(
    origin: str,
    destination: str,
    price: float,
    depart_date: str | None,
    depart_time: str | None,
    arrive_time: str | None,
    existing: dict,
) -> None:
    route = RouteDocument(
        origin=origin,
        destination=destination,
        hubs=existing.get("hubs", []),
        typical_price=TypicalPrice(
            min=round(price * (1 - _PRICE_SPREAD), 2),
            max=round(price * (1 + _PRICE_SPREAD), 2),
            currency="USD",
        ),
        duration_avg_min=existing.get("duration_avg_min", 0),
        depart_date=depart_date,
        depart_time=depart_time,
        arrive_time=arrive_time,
        hidden_city_score=existing.get("hidden_city_score", 0.0),
        hidden_city_via=existing.get("hidden_city_via"),
        hidden_city_decoy=existing.get("hidden_city_decoy"),
        hidden_city_risks=existing.get("hidden_city_risks", []),
        last_updated=datetime.now(timezone.utc),
    )
    try:
        store = get_store()
        data = route.model_dump(mode="json")
        data["@metadata"] = {"@collection": "Routes", "@expires": expires_at()}
        store.get_request_executor().execute(
            PutDocumentCommand(key=route.route_id(), document=data)
        )
        log.info("Cached live price for %s→%s: $%s", origin, destination, price)
    except Exception:
        log.exception("Failed to cache price for %s→%s — continuing", origin, destination)


async def get_live_prices(
    origin: str,
    destination: Optional[str] = None,
    date: Optional[str] = None,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> dict:
    origin = origin.upper()
    destination = destination.upper() if destination else None
    if not date:
        date = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")

    found = await _travelpayouts_search(origin, destination, date, max_results)
    if not found:
        return {"routes": [], "count": 0}

    codes = {origin} | {item["destination"] for item in found}
    names = load_airport_names(get_store(), list(codes))

    routes = []
    for item in found:
        dest = item["destination"]
        existing = _load_existing_route(origin, dest)
        arrive_time = _compute_arrive_time(
            item.get("depart_time"), existing.get("duration_avg_min", 0)
        )
        _write_to_ravendb(
            origin=origin,
            destination=dest,
            price=item["price_usd"],
            depart_date=item.get("depart_date"),
            depart_time=item.get("depart_time"),
            arrive_time=arrive_time,
            existing=existing,
        )

        entry: dict = {
            "to": dest,
            "price_usd": item["price_usd"],
            "depart_date": item.get("depart_date"),
            "depart_time": item.get("depart_time"),
            "arrive_time": arrive_time,
            "hubs": existing.get("hubs", []),
            "duration_min": existing.get("duration_avg_min", 0),
            "stops": item.get("stops", 0),
        }
        if dest in names:
            entry["to_city"] = names[dest]["city"]
        routes.append(entry)

    result: dict = {"routes": routes, "count": len(routes), "from": origin}
    if origin in names:
        result["from_city"] = names[origin]["city"]
    return result
