"""
CronJob entrypoint — runs every 6h inside the cluster.
Pulls Travelpayouts bulk prices and bulk-writes to RavenDB.
Existing documents are updated in place; prices > 2h old trigger a re-fetch.

Origins are not a hardcoded country list — users depart from anywhere, so we
scrape wherever real saved profiles (Users.home_airport / departure_airports)
say they fly from. _FALLBACK_ORIGINS only covers the cold-start case where no
profile has a departure airport saved yet.

Hub (connecting airport) data does not come from the bulk endpoint — see
src/scraper/travelpayouts.py. For each origin, routes with transfers > 0 get
their hub guessed offline via src/scraper/hub_inference.py, against airport
coordinates already in RavenDB — no external call, unlike the real-time
Flight Search API this project can't get approved for.
"""
import asyncio
import logging
import os

from src.db.client import doc_to_dict, get_store, load_all_airport_coords, put_document
from src.db.expiration import ensure_expiration_enabled, expires_at
from src.db.models import RouteDocument
from src.hidden_city.enricher import enrich_hidden_city
from src.scraper.hub_inference import infer_hub
from src.scraper.travelpayouts import fetch_cheapest_from

log = logging.getLogger(__name__)

_FALLBACK_ORIGINS = ["WAW", "LHR", "JFK", "DXB", "SIN"]


def get_origins(store) -> list[str]:
    """Distinct departure airports across all saved user profiles, or the fallback list if none exist yet."""
    origins: set[str] = set()
    with store.open_session() as session:
        users = [doc_to_dict(u) for u in session.query_collection("Users").take(10_000)]

    for user in users:
        home_airport = user.get("home_airport")
        if home_airport:
            origins.add(home_airport.upper())
        for airport in user.get("departure_airports", []):
            origins.add(airport.upper())

    return sorted(origins) if origins else list(_FALLBACK_ORIGINS)


def _resolve_hubs(
    origin: str,
    fetched: list[tuple[RouteDocument, int]],
    airport_coords: dict[str, tuple[float, float]],
) -> list[RouteDocument]:
    """Guesses a connecting hub for routes with transfers > 0. Routes whose
    origin/destination/candidate-hub coordinates aren't in RavenDB keep
    hubs=[] — see src/scraper/hub_inference.py."""
    routes: list[RouteDocument] = []
    for route, transfers in fetched:
        if transfers > 0:
            hubs = infer_hub(origin, route.destination, airport_coords)
            route = route.model_copy(update={"hubs": hubs})
        routes.append(route)
    return routes


async def run() -> None:
    token = os.environ["TRAVELPAYOUTS_TOKEN"]
    store = get_store()
    # The CronJob runs as its own process, independent of the agent's startup event —
    # don't assume the agent pod has already turned expiration on for this database.
    ensure_expiration_enabled(store)
    origins = get_origins(store)
    airport_coords = load_all_airport_coords(store)
    total_written = 0

    for origin in origins:
        print(f"  Travelpayouts → fetching {origin}...", flush=True)
        fetched = await fetch_cheapest_from(origin, token)
        log.info("Fetched %d routes from %s", len(fetched), origin)

        routes = _resolve_hubs(origin, fetched, airport_coords)
        enriched = enrich_hidden_city(routes)
        hidden = sum(1 for r in enriched if r.hidden_city_score > 0.5)

        for route in enriched:
            data = route.model_dump(mode="json")
            data["@metadata"] = {"@collection": "Routes", "@expires": expires_at()}
            put_document(store, route.route_id(), data)

        total_written += len(enriched)
        print(f"  Travelpayouts → {origin}: {len(enriched)} routes ({hidden} hidden city)", flush=True)
        log.info("Wrote %d routes for origin %s", len(enriched), origin)

    print(f"  Travelpayouts → done, {total_written} routes total", flush=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
