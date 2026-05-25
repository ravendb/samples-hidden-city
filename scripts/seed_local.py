"""
Seeds local RavenDB (docker-compose) with airports and fixture routes.
Delegates to src.db.seed — same logic as the automatic startup seed.

Usage:
    python -m scripts.seed_local
"""
import logging

from dotenv import load_dotenv

load_dotenv()

from src.db.seed import ensure_database, get_store, seed_airports, seed_routes


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = get_store()
    ensure_database(store)
    n_airports = seed_airports(store)
    n_routes = seed_routes(store)
    print(f"Done — {n_airports} airports, {n_routes} routes")
    print("Open http://localhost:8080 to inspect")


if __name__ == "__main__":
    main()
