"""
CronJob entrypoint — runs every 6h inside the cluster.
Pulls Travelpayouts bulk prices and bulk-writes to RavenDB.
Existing documents are updated in place; prices > 2h old trigger a re-fetch.
"""
import asyncio
import logging
import os

from src.db.client import get_store
from src.db.models import RouteDocument
from src.hidden_city.enricher import enrich_hidden_city
from src.scraper.travelpayouts import fetch_cheapest_from

log = logging.getLogger(__name__)

ORIGINS = ["WAW", "KTW", "KRK", "WRO"]


async def run() -> None:
    token = os.environ["TRAVELPAYOUTS_TOKEN"]
    store = get_store()

    for origin in ORIGINS:
        log.info("Scraping %s", origin)
        routes = await fetch_cheapest_from(origin, token)
        log.info("Fetched %d routes from %s", len(routes), origin)

        enriched = enrich_hidden_city(routes)
        hidden = sum(1 for r in enriched if r.hidden_city_score > 0.5)
        log.info("Hidden city candidates: %d", hidden)

        with store.bulk_insert() as bulk:
            for route in enriched:
                bulk.store(route.model_dump(), route.route_id())

        log.info("Wrote %d routes for origin %s", len(enriched), origin)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
