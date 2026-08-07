"""
Travelpayouts /v2/prices/latest — bulk cheapest prices from an origin.
Called by the CronJob scraper every 6h. On cache miss, get_live_prices tool
calls Travelpayouts /v1/prices/cheap instead (see src/tools/get_live_prices.py).

/v2/prices/latest exposes `number_of_changes` (a transfer count) but never the
actual connecting airport codes. Travelpayouts only reveals real itinerary
segments through the separate real-time Flight Search API, which requires a
partnership approval + 50k MAU this project doesn't have and 403s regardless.
fetch_cheapest_from() therefore returns the transfer count alongside each
route so the caller (src/scraper/run.py) can decide which routes need a hub
guess — resolved offline via src/scraper/hub_inference.py, no live API call.
"""
from datetime import datetime, timezone

import httpx

from src.db.models import RouteDocument, TypicalPrice

_BASE = "https://api.travelpayouts.com"
_PRICE_SPREAD = 0.15  # ±15% around the scraped price to fill min/max


async def validate_token(token: str) -> bool:
    """One minimal request instead of a full 85-origin scrape (see
    src/scraper/run.py) — lets a bad token fail once, with one clear message,
    instead of once per origin. Returns False only on a confirmed 401
    (invalid/expired token); any other outcome (including network errors) is
    treated as "can't tell, don't block" and returns True."""
    params = {"origin": "WAW", "token": token, "currency": "usd", "limit": 1, "period_type": "month"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{_BASE}/v2/prices/latest", params=params)
        if response.status_code == 401:
            return False
    except Exception:
        pass
    return True


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
    docstring; the caller fills it in via src/scraper/hub_inference.py for
    routes with transfers > 0.
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
