"""
CronJob entrypoint — runs every 6h inside the cluster.
Pulls Travelpayouts bulk prices and bulk-writes to RavenDB.
Existing documents are updated in place; prices > 2h old trigger a re-fetch.

Origins are not a hardcoded country list — users depart from anywhere, so we
scrape wherever real saved profiles (Users.home_airport / departure_airports)
say they fly from. _FALLBACK_ORIGINS only covers the cold-start case where no
profile has a departure airport saved yet.
"""
import asyncio
import logging
import os

from pyravendb.commands.commands_data import PutDocumentCommand

from src.db.client import doc_to_dict, get_store
from src.db.models import RouteDocument
from src.hidden_city.enricher import enrich_hidden_city
from src.scraper.travelpayouts import fetch_cheapest_from

log = logging.getLogger(__name__)

_FALLBACK_ORIGINS = ["WAW", "LHR", "JFK", "DXB", "SIN"]


def get_origins(store) -> list[str]:
    """Distinct departure airports across all saved user profiles, or the fallback list if none exist yet."""
    origins: set[str] = set()
    with store.open_session() as session:
        users = [doc_to_dict(u) for u in session.query(collection_name="Users").take(10_000)]

    for user in users:
        home_airport = user.get("home_airport")
        if home_airport:
            origins.add(home_airport.upper())
        for airport in user.get("departure_airports", []):
            origins.add(airport.upper())

    return sorted(origins) if origins else list(_FALLBACK_ORIGINS)


async def run() -> None:
    token = os.environ["TRAVELPAYOUTS_TOKEN"]
    store = get_store()
    origins = get_origins(store)
    total_written = 0

    for origin in origins:
        print(f"  Travelpayouts → fetching {origin}...", flush=True)
        routes = await fetch_cheapest_from(origin, token)
        log.info("Fetched %d routes from %s", len(routes), origin)

        enriched = enrich_hidden_city(routes)
        hidden = sum(1 for r in enriched if r.hidden_city_score > 0.5)

        executor = store.get_request_executor()
        for route in enriched:
            data = route.model_dump(mode="json")
            data["@metadata"] = {"@collection": "Routes"}
            executor.execute(PutDocumentCommand(key=route.route_id(), document=data))

        total_written += len(enriched)
        print(f"  Travelpayouts → {origin}: {len(enriched)} routes ({hidden} hidden city)", flush=True)
        log.info("Wrote %d routes for origin %s", len(enriched), origin)

    print(f"  Travelpayouts → done, {total_written} routes total", flush=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
