"""
get_live_price tool — live API calls on cache miss.

  hidden_city → Kiwi Tequila (specialises in non-obvious routing)
  direct      → Amadeus (400+ airlines, accurate direct prices)

Results are written back to RavenDB so the next call is a cache hit.
"""
import logging
import os
import time
from datetime import datetime, timezone

import httpx
from pyravendb.commands.commands_data import PutDocumentCommand

from src.db.client import get_store
from src.db.models import RouteDocument, TypicalPrice

log = logging.getLogger(__name__)

_KIWI_BASE = "https://api.tequila.kiwi.com/v2"
_AMADEUS_BASE = "https://test.api.amadeus.com"  # swap to production URL for prod

_amadeus_token_cache: dict = {}


async def _kiwi_search(origin: str, destination: str, date: str) -> dict:
    api_key = os.environ["KIWI_API_KEY"]
    params = {
        "fly_from": origin,
        "fly_to": destination,
        "date_from": date,
        "date_to": date,
        "curr": "USD",
        "limit": 3,
        "sort": "price",
        "max_stopovers": 2,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{_KIWI_BASE}/search",
            params=params,
            headers={"apikey": api_key},
        )
        response.raise_for_status()

    data = response.json()
    itineraries = data.get("data", [])
    if not itineraries:
        return {"found": False}

    best = itineraries[0]
    hubs = [r["flyTo"] for r in best.get("route", [])[:-1]]
    depart_dt = best.get("local_departure", "")
    arrive_dt = best.get("local_arrival", "")
    return {
        "found": True,
        "price_usd": best["price"],
        "hubs": hubs,
        "airline": best.get("airlines", []),
        "depart_date": depart_dt[:10] if depart_dt else None,
        "depart_time": depart_dt[11:16] if len(depart_dt) >= 16 else None,
        "arrive_time": arrive_dt[11:16] if len(arrive_dt) >= 16 else None,
        "duration_min": _parse_kiwi_duration(best.get("fly_duration", "")),
    }


async def _get_amadeus_token() -> str:
    cached = _amadeus_token_cache
    if cached.get("access_token") and cached.get("expires_at", 0) > time.time():
        return cached["access_token"]

    client_id = os.environ["AMADEUS_CLIENT_ID"]
    client_secret = os.environ["AMADEUS_CLIENT_SECRET"]
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{_AMADEUS_BASE}/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        response.raise_for_status()

    token_data = response.json()
    _amadeus_token_cache.update(
        {
            "access_token": token_data["access_token"],
            "expires_at": time.time() + token_data["expires_in"] - 60,
        }
    )
    return token_data["access_token"]


async def _amadeus_search(origin: str, destination: str, date: str) -> dict:
    token = await _get_amadeus_token()
    params = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": date,
        "adults": 1,
        "currencyCode": "USD",
        "max": 3,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{_AMADEUS_BASE}/v2/shopping/flight-offers",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()

    offers = response.json().get("data", [])
    if not offers:
        return {"found": False}

    best = offers[0]
    price = float(best["price"]["total"])
    itinerary = best["itineraries"][0]
    segments = itinerary["segments"]
    hubs = [s["departure"]["iataCode"] for s in segments[:-1]]
    depart_dt = segments[0]["departure"].get("at", "")
    arrive_dt = segments[-1]["arrival"].get("at", "")
    return {
        "found": True,
        "price_usd": price,
        "hubs": hubs,
        "duration_min": _parse_duration(itinerary.get("duration", "")),
        "depart_date": depart_dt[:10] if depart_dt else None,
        "depart_time": depart_dt[11:16] if len(depart_dt) >= 16 else None,
        "arrive_time": arrive_dt[11:16] if len(arrive_dt) >= 16 else None,
    }


def _parse_duration(iso_duration: str) -> int:
    """Parse PT10H30M → 630 minutes."""
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", iso_duration)
    if not m:
        return 0
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    return hours * 60 + minutes


def _parse_kiwi_duration(s: str) -> int:
    """Parse '10h 30m' or '10h' or '30m' → minutes."""
    import re
    m = re.search(r"(?:(\d+)h)?\s*(?:(\d+)m)?", s)
    if not m:
        return 0
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def _write_to_ravendb(
    origin: str, destination: str, result: dict, hubs: list[str], duration_min: int
) -> None:
    price = result["price_usd"]
    route = RouteDocument(
        origin=origin,
        destination=destination,
        hubs=hubs,
        typical_price=TypicalPrice(
            min=round(price * 0.9, 2),
            max=round(price * 1.1, 2),
            currency="USD",
        ),
        duration_avg_min=duration_min,
        depart_date=result.get("depart_date"),
        depart_time=result.get("depart_time"),
        arrive_time=result.get("arrive_time"),
        last_updated=datetime.now(timezone.utc),
    )
    try:
        store = get_store()
        data = route.model_dump(mode="json")
        data["@metadata"] = {"@collection": "Routes"}
        store.get_request_executor().execute(PutDocumentCommand(key=route.route_id(), document=data))
        log.info("Cached live price for %s→%s: $%s", origin, destination, price)
    except Exception:
        log.exception("Failed to cache price for %s→%s — continuing", origin, destination)


async def get_live_price(
    origin: str,
    destination: str,
    route_type: str,
    date: str | None = None,
) -> dict:
    origin = origin.upper()
    destination = destination.upper()
    if not date:
        from datetime import timedelta
        date = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")

    if route_type == "hidden_city":
        result = await _kiwi_search(origin, destination, date)
        hubs = result.get("hubs", [])
        duration_min = result.get("duration_min", 0)
    else:
        result = await _amadeus_search(origin, destination, date)
        hubs = result.get("hubs", [])
        duration_min = result.get("duration_min", 0)

    if result.get("found"):
        _write_to_ravendb(origin, destination, result, hubs, duration_min)

    return result
