"""
get_live_price tool — refreshes price from Travelpayouts on cache miss.

Travelpayouts /v1/prices/cheap returns: price, departure date/time, stops count.
It does NOT return arrival time or intermediate hub airports.

Strategy:
- Load the existing RavenDB route doc to preserve hubs and duration_avg_min.
- Fetch fresh price + depart_time from Travelpayouts.
- Compute arrive_time = depart_time + duration_avg_min (if both known).
- Write back only the price/timing fields; structural data (hubs, duration) unchanged.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from pyravendb.commands.commands_data import PutDocumentCommand

from src.db.client import doc_to_dict, get_store, load_airport_names
from src.db.expiration import expires_at
from src.db.models import RouteDocument, TypicalPrice

log = logging.getLogger(__name__)

_BASE = "https://api.travelpayouts.com"
_PRICE_SPREAD = 0.15


async def _travelpayouts_search(origin: str, destination: str, date: str) -> dict:
    token = os.environ["TRAVELPAYOUTS_TOKEN"]
    params = {
        "origin": origin,
        "destination": destination,
        "depart_date": date[:7],  # API expects YYYY-MM
        "token": token,
        "currency": "usd",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{_BASE}/v1/prices/cheap", params=params)
        response.raise_for_status()

    payload = response.json()
    if not payload.get("success"):
        return {"found": False}

    data = payload.get("data", {})
    dest_data = data.get(destination) or (list(data.values())[0] if data else None)
    if not dest_data:
        return {"found": False}

    depart_at = dest_data.get("departure_at", "")
    return {
        "found": True,
        "price_usd": float(dest_data.get("price", 0)),
        "depart_date": depart_at[:10] if depart_at else date,
        "depart_time": depart_at[11:16] if len(depart_at) >= 16 else None,
        "stops": dest_data.get("number_of_changes", 0),
    }


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


async def get_live_price(
    origin: str,
    destination: str,
    date: str | None = None,
) -> dict:
    origin = origin.upper()
    destination = destination.upper()
    if not date:
        date = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")

    existing = _load_existing_route(origin, destination)
    result = await _travelpayouts_search(origin, destination, date)

    if not result.get("found"):
        return result

    arrive_time = _compute_arrive_time(
        result.get("depart_time"),
        existing.get("duration_avg_min", 0),
    )

    _write_to_ravendb(
        origin=origin,
        destination=destination,
        price=result["price_usd"],
        depart_date=result.get("depart_date"),
        depart_time=result.get("depart_time"),
        arrive_time=arrive_time,
        existing=existing,
    )

    names = load_airport_names(get_store(), [origin, destination])
    response: dict = {
        "found": True,
        "price_usd": result["price_usd"],
        "depart_date": result.get("depart_date"),
        "depart_time": result.get("depart_time"),
        "arrive_time": arrive_time,
        "hubs": existing.get("hubs", []),
        "duration_min": existing.get("duration_avg_min", 0),
        "stops": result.get("stops", 0),
    }
    if origin in names:
        response["from_city"] = names[origin]["city"]
    if destination in names:
        response["to_city"] = names[destination]["city"]
    return response
