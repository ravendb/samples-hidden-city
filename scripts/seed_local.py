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
    # Direct routes — realistic prices, used as baseline for hidden city comparison
    {"origin": "WAW", "destination": "LHR", "hubs": [], "price_min": 580, "price_max": 720},
    {"origin": "WAW", "destination": "FRA", "hubs": [], "price_min": 380, "price_max": 480},
    {"origin": "WAW", "destination": "AMS", "hubs": [], "price_min": 320, "price_max": 420},
    {"origin": "WAW", "destination": "IST", "hubs": [], "price_min": 260, "price_max": 340},
    {"origin": "WAW", "destination": "CDG", "hubs": [], "price_min": 340, "price_max": 440},
    # Connecting routes — hidden city candidates (score > 0.5 after enrichment)
    # WAW→JFK via LHR: 220 vs 580 direct to LHR → 62% savings → score ≈ 0.62
    {"origin": "WAW", "destination": "JFK", "hubs": ["LHR"], "price_min": 220, "price_max": 320},
    # WAW→BOS via LHR: 240 vs 580 direct to LHR → 59% savings → score ≈ 0.59
    {"origin": "WAW", "destination": "BOS", "hubs": ["LHR", "FRA"], "price_min": 240, "price_max": 340},
    # WAW→ORD via FRA: 155 vs 380 direct to FRA → 59% savings → score ≈ 0.59
    {"origin": "WAW", "destination": "ORD", "hubs": ["FRA", "AMS"], "price_min": 155, "price_max": 240},
    # WAW→DOH via IST: 120 vs 260 direct to IST → 54% savings → score ≈ 0.54
    {"origin": "WAW", "destination": "DOH", "hubs": ["IST"], "price_min": 120, "price_max": 185},
    # WAW→DXB via IST: 130 vs 260 direct to IST → 50% savings → score ≈ 0.50
    {"origin": "WAW", "destination": "DXB", "hubs": ["IST", "DOH"], "price_min": 130, "price_max": 200},
    {"origin": "WAW", "destination": "EWR", "hubs": ["LHR", "AMS"], "price_min": 230, "price_max": 330},
    # Asia routes via Gulf/IST hubs — hidden city candidates for IST
    # WAW→IST direct: 260.  WAW→CKG via IST: 110 → 58% savings → score ≈ 0.58 ✓
    # WAW→IST direct: 260.  WAW→PVG via IST: 115 → 56% savings → score ≈ 0.56 ✓
    # WAW→IST direct: 260.  WAW→PEK via IST: 108 → 58% savings → score ≈ 0.58 ✓
    # (DOH is a second hub leg but IST gives the best savings — enricher picks highest score)
    {"origin": "WAW", "destination": "CKG", "hubs": ["IST", "DOH"], "price_min": 110, "price_max": 175},
    {"origin": "WAW", "destination": "PVG", "hubs": ["IST", "DOH"], "price_min": 115, "price_max": 180},
    {"origin": "WAW", "destination": "PEK", "hubs": ["IST"], "price_min": 108, "price_max": 170},
    # Katowice routes
    {"origin": "KTW", "destination": "LHR", "hubs": [], "price_min": 560, "price_max": 700},
    {"origin": "KTW", "destination": "JFK", "hubs": ["FRA", "AMS"], "price_min": 165, "price_max": 260},
    {"origin": "KTW", "destination": "FRA", "hubs": [], "price_min": 360, "price_max": 460},
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
