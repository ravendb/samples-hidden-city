"""
CronJob entrypoint — runs every 6h inside the cluster.
Pulls Travelpayouts bulk prices and bulk-writes to RavenDB.
Existing documents are updated in place; prices > 2h old trigger a re-fetch.

Origins are not a hardcoded country list — users depart from anywhere, so we
scrape wherever real saved profiles (Users.home_airport / departure_airports)
say they fly from. _FALLBACK_ORIGINS only covers the cold-start case where no
profile has a departure airport saved yet.

Hub (connecting airport) data does not come from the bulk endpoint — see
src/scraper/travelpayouts.py. For each origin, routes with transfers > 0 get a
real-time Flight Search lookup to resolve their actual hub, capped at
_MAX_HUB_LOOKUPS_PER_ORIGIN per run since each lookup is a search+poll call,
far pricier than the bulk fetch. Routes past the cap keep hubs=[] until a
later run reaches them.
"""
import asyncio
import logging
import os

from pyravendb.commands.commands_data import PutDocumentCommand

from src.db.client import doc_to_dict, get_store
from src.db.expiration import ensure_expiration_enabled, expires_at
from src.db.models import RouteDocument
from src.hidden_city.enricher import enrich_hidden_city
from src.scraper.travelpayouts import fetch_cheapest_from, fetch_hubs_for_route

log = logging.getLogger(__name__)

_FALLBACK_ORIGINS = ["WAW", "LHR", "JFK", "DXB", "SIN"]
_MAX_HUB_LOOKUPS_PER_ORIGIN = int(os.environ.get("MAX_HUB_LOOKUPS_PER_ORIGIN", "25"))


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


async def _resolve_hubs(
    origin: str, fetched: list[tuple[RouteDocument, int]], token: str, marker: str
) -> list[RouteDocument]:
    """Fills in real hubs for routes with transfers > 0, up to the per-origin cap."""
    routes: list[RouteDocument] = []
    lookups_done = 0
    capped = False

    for route, transfers in fetched:
        if transfers > 0 and route.depart_date:
            if lookups_done >= _MAX_HUB_LOOKUPS_PER_ORIGIN:
                capped = True
                routes.append(route)
                continue
            try:
                hubs = await fetch_hubs_for_route(origin, route.destination, route.depart_date, token, marker)
                route = route.model_copy(update={"hubs": hubs})
            except Exception:
                log.exception("Hub lookup failed for %s→%s — leaving hubs empty", origin, route.destination)
            lookups_done += 1
        routes.append(route)

    if capped:
        print(
            f"  Travelpayouts → {origin}: hub lookup capped at {_MAX_HUB_LOOKUPS_PER_ORIGIN}, "
            "remaining indirect routes keep hubs=[] until a later run",
            flush=True,
        )
    return routes


async def run() -> None:
    token = os.environ["TRAVELPAYOUTS_TOKEN"]
    marker = os.environ["TRAVELPAYOUTS_MARKER"]
    store = get_store()
    # The CronJob runs as its own process, independent of the agent's startup event —
    # don't assume the agent pod has already turned expiration on for this database.
    ensure_expiration_enabled(store)
    origins = get_origins(store)
    total_written = 0

    for origin in origins:
        print(f"  Travelpayouts → fetching {origin}...", flush=True)
        fetched = await fetch_cheapest_from(origin, token)
        log.info("Fetched %d routes from %s", len(fetched), origin)

        routes = await _resolve_hubs(origin, fetched, token, marker)
        enriched = enrich_hidden_city(routes)
        hidden = sum(1 for r in enriched if r.hidden_city_score > 0.5)

        executor = store.get_request_executor()
        for route in enriched:
            data = route.model_dump(mode="json")
            data["@metadata"] = {"@collection": "Routes", "@expires": expires_at()}
            executor.execute(PutDocumentCommand(key=route.route_id(), document=data))

        total_written += len(enriched)
        print(f"  Travelpayouts → {origin}: {len(enriched)} routes ({hidden} hidden city)", flush=True)
        log.info("Wrote %d routes for origin %s", len(enriched), origin)

    print(f"  Travelpayouts → done, {total_written} routes total", flush=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
