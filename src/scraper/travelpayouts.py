"""
Travelpayouts /v2/prices/latest — bulk cheapest prices from an origin.
Called by the CronJob scraper every 6h. On cache miss, get_live_price tool
calls Travelpayouts /v1/prices/cheap instead (see src/tools/get_live_price.py).
"""
from datetime import datetime, timezone

import httpx

from src.db.models import RouteDocument, TypicalPrice

_BASE = "https://api.travelpayouts.com"
_PRICE_SPREAD = 0.15  # ±15% around the scraped price to fill min/max


async def fetch_cheapest_from(
    origin: str,
    token: str,
    currency: str = "usd",
    limit: int = 1000,
) -> list[RouteDocument]:
    """
    Returns up to `limit` cheapest routes from `origin`.
    Hubs are NOT populated here — Travelpayouts does not expose itinerary legs.
    Hub enrichment is not available via Travelpayouts — hubs remain empty until enriched manually.
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
    routes: list[RouteDocument] = []
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
        routes.append(
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
            )
        )

    return routes
