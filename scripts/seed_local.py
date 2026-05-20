"""
Seeds local RavenDB (docker-compose) with airports and fixture routes.
Run once after `docker-compose up -d ravendb`.

Fixture routes include known hidden city patterns so the demo works
without real API keys.

Usage:
    python -m scripts.seed_local
"""
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.db.client import get_store
from src.db.models import (
    AirportDocument,
    Coordinates,
    NearbyAirport,
    RouteDocument,
    TypicalPrice,
)
from src.hidden_city.enricher import enrich_hidden_city

log = logging.getLogger(__name__)

# Routes with known hubs — prices in USD
# Hidden city patterns embedded here:
#   WAW→JFK (via LHR) cheaper than WAW→LHR direct  → LHR hidden city
#   WAW→BOS (via LHR) cheaper than WAW→LHR direct  → LHR hidden city (second decoy)
#   WAW→ORD (via FRA) cheaper than WAW→FRA direct  → FRA hidden city
#   WAW→DOH (via IST) cheaper than WAW→IST direct  → IST hidden city
FIXTURE_ROUTES = [
    # Direct routes (expensive, no hubs)
    {"origin": "WAW", "destination": "LHR", "hubs": [], "price_min": 310, "price_max": 420},
    {"origin": "WAW", "destination": "FRA", "hubs": [], "price_min": 85, "price_max": 130},
    {"origin": "WAW", "destination": "AMS", "hubs": [], "price_min": 75, "price_max": 110},
    {"origin": "WAW", "destination": "IST", "hubs": [], "price_min": 140, "price_max": 200},
    {"origin": "WAW", "destination": "CDG", "hubs": [], "price_min": 95, "price_max": 140},
    # Connecting routes (cheap, with hubs → hidden city candidates)
    {"origin": "WAW", "destination": "JFK", "hubs": ["LHR"], "price_min": 185, "price_max": 280},
    {"origin": "WAW", "destination": "BOS", "hubs": ["LHR", "FRA"], "price_min": 195, "price_max": 290},
    {"origin": "WAW", "destination": "ORD", "hubs": ["FRA", "AMS"], "price_min": 200, "price_max": 300},
    {"origin": "WAW", "destination": "DOH", "hubs": ["IST"], "price_min": 115, "price_max": 165},
    {"origin": "WAW", "destination": "DXB", "hubs": ["IST", "DOH"], "price_min": 120, "price_max": 175},
    {"origin": "WAW", "destination": "EWR", "hubs": ["LHR", "AMS"], "price_min": 190, "price_max": 285},
    # Katowice routes (nearby alternative origin)
    {"origin": "KTW", "destination": "LHR", "hubs": [], "price_min": 290, "price_max": 400},
    {"origin": "KTW", "destination": "JFK", "hubs": ["FRA", "AMS"], "price_min": 195, "price_max": 295},
    {"origin": "KTW", "destination": "FRA", "hubs": [], "price_min": 70, "price_max": 110},
]


def seed_airports(store) -> int:
    data_path = Path(__file__).parent.parent / "data" / "airports.json"
    airports = json.loads(data_path.read_text())

    with store.open_session() as session:
        for raw in airports:
            doc = AirportDocument(
                iata=raw["iata"],
                name=raw["name"],
                city=raw["city"],
                country=raw["country"],
                coordinates=Coordinates(**raw["coordinates"]),
                nearby=[NearbyAirport(**n) for n in raw.get("nearby", [])],
            )
            session.store(doc.model_dump(), doc.airport_id())
        session.save_changes()

    log.info("Seeded %d airports", len(airports))
    return len(airports)


def seed_routes(store) -> int:
    routes = [
        RouteDocument(
            origin=r["origin"],
            destination=r["destination"],
            hubs=r["hubs"],
            typical_price=TypicalPrice(
                min=float(r["price_min"]),
                max=float(r["price_max"]),
                currency="USD",
            ),
        )
        for r in FIXTURE_ROUTES
    ]

    enriched = enrich_hidden_city(routes)

    hidden_count = sum(1 for r in enriched if r.hidden_city_score > 0.5)
    log.info("Hidden city candidates found: %d", hidden_count)

    with store.bulk_insert() as bulk:
        for route in enriched:
            bulk.store(route.model_dump(), route.route_id())

    log.info("Seeded %d routes", len(enriched))
    return len(enriched)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = get_store()
    seed_airports(store)
    seed_routes(store)
    log.info("Done — open http://localhost:8080 to inspect")


if __name__ == "__main__":
    main()
