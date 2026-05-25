"""
Auto-seeds the local database with airport and route fixtures.
Called on every app startup — safe to run multiple times (skips if data exists).
"""
import json
import logging
import os
from pathlib import Path

from pyravendb.commands.commands_data import PutDocumentCommand
from pyravendb.raven_operations.server_operations import CreateDatabaseOperation

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

_DATA_DIR = Path(__file__).parent.parent.parent / "data"

FIXTURE_ROUTES = [
    {"origin": "WAW", "destination": "LHR", "hubs": [], "price_min": 580, "price_max": 720},
    {"origin": "WAW", "destination": "FRA", "hubs": [], "price_min": 380, "price_max": 480},
    {"origin": "WAW", "destination": "AMS", "hubs": [], "price_min": 320, "price_max": 420},
    {"origin": "WAW", "destination": "IST", "hubs": [], "price_min": 260, "price_max": 340},
    {"origin": "WAW", "destination": "CDG", "hubs": [], "price_min": 340, "price_max": 440},
    {"origin": "WAW", "destination": "JFK", "hubs": ["LHR"], "price_min": 220, "price_max": 320},
    {"origin": "WAW", "destination": "BOS", "hubs": ["LHR", "FRA"], "price_min": 240, "price_max": 340},
    {"origin": "WAW", "destination": "ORD", "hubs": ["FRA", "AMS"], "price_min": 155, "price_max": 240},
    {"origin": "WAW", "destination": "DOH", "hubs": ["IST"], "price_min": 120, "price_max": 185},
    {"origin": "WAW", "destination": "DXB", "hubs": ["IST", "DOH"], "price_min": 130, "price_max": 200},
    {"origin": "WAW", "destination": "EWR", "hubs": ["LHR", "AMS"], "price_min": 230, "price_max": 330},
    {"origin": "WAW", "destination": "CKG", "hubs": ["IST", "DOH"], "price_min": 110, "price_max": 175},
    {"origin": "WAW", "destination": "PVG", "hubs": ["IST", "DOH"], "price_min": 115, "price_max": 180},
    {"origin": "WAW", "destination": "PEK", "hubs": ["IST"], "price_min": 108, "price_max": 170},
    {"origin": "KTW", "destination": "LHR", "hubs": [], "price_min": 560, "price_max": 700},
    {"origin": "KTW", "destination": "JFK", "hubs": ["FRA", "AMS"], "price_min": 165, "price_max": 260},
    {"origin": "KTW", "destination": "FRA", "hubs": [], "price_min": 360, "price_max": 460},
]


def ensure_database(store) -> None:
    db_name = os.environ["RAVENDB_DATABASE"]
    try:
        store.maintenance.server.send(CreateDatabaseOperation(db_name))
        log.info("Created database '%s'", db_name)
    except Exception as e:
        msg = str(e).lower()
        if "already exist" in msg or "concurrency" in msg:
            pass
        else:
            raise


def seed_airports(store) -> int:
    airports_file = _DATA_DIR / "airports.json"
    if not airports_file.exists():
        log.warning("airports.json not found at %s — skipping airport seed", airports_file)
        return 0

    airports = json.loads(airports_file.read_text())
    executor = store.get_request_executor()
    for raw in airports:
        doc = AirportDocument(
            iata=raw["iata"],
            name=raw["name"],
            city=raw["city"],
            country=raw["country"],
            coordinates=Coordinates(**raw["coordinates"]),
            nearby=[NearbyAirport(**n) for n in raw.get("nearby", [])],
        )
        data = doc.model_dump()
        data["@metadata"] = {"@collection": "Airports"}
        executor.execute(PutDocumentCommand(key=doc.airport_id(), document=data))

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

    executor = store.get_request_executor()
    for route in enriched:
        data = route.model_dump()
        data["@metadata"] = {"@collection": "Routes"}
        executor.execute(PutDocumentCommand(key=route.route_id(), document=data))

    return len(enriched)


def seed_if_empty() -> None:
    """Seeds airports and fixture routes if the Routes collection is empty."""
    store = get_store()
    ensure_database(store)

    with store.open_session() as session:
        existing = list(session.query(collection_name="Routes").take(1))

    if existing:
        n = _count_routes(store)
        print(f"  DB: {n} routes already in database — skipping seed", flush=True)
        return

    print("  DB: seeding airports...", flush=True)
    n_airports = seed_airports(store)
    print(f"  DB: {n_airports} airports written", flush=True)

    print("  DB: seeding fixture routes...", flush=True)
    n_routes = seed_routes(store)
    hidden = _count_hidden(store)
    print(f"  DB: {n_routes} routes written ({hidden} hidden city candidates)", flush=True)


def _count_routes(store) -> int:
    with store.open_session() as session:
        return len(list(session.query(collection_name="Routes")))


def _count_hidden(store) -> int:
    with store.open_session() as session:
        return len(list(
            session.query(collection_name="Routes")
            .where_greater_than("hidden_city_score", 0.5)
        ))
