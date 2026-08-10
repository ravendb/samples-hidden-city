"""
Auto-seeds the local database with airport and route fixtures.
Called on every app startup — safe to run multiple times (skips if data exists).
"""
import json
import logging
import os
import time
from pathlib import Path

from ravendb import CreateDatabaseOperation
from ravendb.exceptions.raven_exceptions import ConcurrencyException
from ravendb.serverwide.database_record import DatabaseRecord

from src.db.client import get_store, put_document
from src.db.expiration import ensure_expiration_enabled
from src.db.geo import to_unit_vector
from src.db.models import (
    AirportDocument,
    Coordinates,
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


def ensure_database(store, max_wait_seconds: float = 90) -> None:
    """Creates the database if it doesn't exist yet, retrying with backoff for
    up to max_wait_seconds while RavenDB itself isn't reachable yet.

    This is called independently by every process that touches the database
    (agent startup, worker, scraper CronJob) -- none of them may assume
    another one already ran first. In Kubernetes, a pod commonly starts
    before the RavenDB cluster has finished forming Raft quorum; a single,
    unretried attempt here used to fail with a bare connection error that the
    caller swallowed, leaving the database permanently missing and every
    later request failing with DatabaseDoesNotExistException (confirmed in
    practice, not hypothetical). ConcurrencyException -- another process won
    the race to create it first -- is treated as success, not an error: this
    function's contract is "the database exists when this returns", not
    "this process created it".
    """
    db_name = os.environ["RAVENDB_DATABASE"]
    deadline = time.monotonic() + max_wait_seconds
    delay = 1.0
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            store.maintenance.server.send(CreateDatabaseOperation(DatabaseRecord(db_name)))
            log.info("Created database '%s'", db_name)
            break
        except ConcurrencyException:
            break
        except Exception as e:
            last_error = e
            log.warning("ensure_database: RavenDB not ready yet (%s) -- retrying in %.0fs", e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 10)
    else:
        raise RuntimeError(
            f"RavenDB never became available to create database {db_name!r} within {max_wait_seconds:.0f}s"
        ) from last_error

    ensure_expiration_enabled(store)


def seed_airports(store) -> int:
    airports_file = _DATA_DIR / "airports.json"
    if not airports_file.exists():
        log.warning("airports.json not found at %s — skipping airport seed", airports_file)
        return 0

    airports = json.loads(airports_file.read_text())
    for raw in airports:
        coordinates = Coordinates(**raw["coordinates"])
        doc = AirportDocument(
            iata=raw["iata"],
            name=raw["name"],
            city=raw["city"],
            country=raw["country"],
            coordinates=coordinates,
            location_vector=to_unit_vector(coordinates.lat, coordinates.lng),
        )
        data = doc.model_dump(mode="json")
        data["@metadata"] = {"@collection": "Airports"}
        put_document(store, doc.airport_id(), data)

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

    for route in enriched:
        data = route.model_dump(mode="json")
        data["@metadata"] = {"@collection": "Routes"}
        put_document(store, route.route_id(), data)

    return len(enriched)


def seed_if_empty() -> None:
    """Seeds airports and fixture routes if their respective collections are empty."""
    store = get_store()
    ensure_database(store)

    with store.open_session() as session:
        existing_airports = list(session.query_collection("Airports").take(1))

    if not existing_airports:
        print("  DB: seeding airports...", flush=True)
        n_airports = seed_airports(store)
        print(f"  DB: {n_airports} airports written", flush=True)
    else:
        print("  DB: airports already seeded — skipping", flush=True)

    with store.open_session() as session:
        existing_routes = list(session.query_collection("Routes").take(1))

    if not existing_routes:
        print("  DB: seeding fixture routes...", flush=True)
        n_routes = seed_routes(store)
        hidden = _count_hidden(store)
        print(f"  DB: {n_routes} routes written ({hidden} hidden city candidates)", flush=True)
    else:
        n = _count_routes(store)
        print(f"  DB: {n} routes already in database — skipping seed", flush=True)


def _count_routes(store) -> int:
    with store.open_session() as session:
        return len(list(session.query_collection("Routes")))


def _count_hidden(store) -> int:
    with store.open_session() as session:
        return len(list(
            session.query_collection("Routes")
            .where_greater_than("hidden_city_score", 0.5)
        ))
