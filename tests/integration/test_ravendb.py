"""
Integration tests — require a running RavenDB instance.

Run with:
    pytest tests/integration/ --require-ravendb

Or let them be skipped automatically when RavenDB is not available.
"""
import pytest

from src.db.models import AirportDocument, Coordinates, RouteDocument, TypicalPrice


@pytest.mark.integration
class TestRouteStoreAndLoad:
    def test_store_and_load_route(self, ravendb_session):
        route = RouteDocument(
            origin="WAW",
            destination="JFK",
            hubs=["LHR"],
            typical_price=TypicalPrice(min=185.0, max=280.0),
            hidden_city_score=0.72,
            hidden_city_via="LHR",
        )
        ravendb_session.store(route.model_dump(), route.route_id())
        ravendb_session.save_changes()

        loaded = ravendb_session.load(route.route_id())
        assert loaded["origin"] == "WAW"
        assert loaded["destination"] == "JFK"
        assert loaded["hidden_city_score"] == pytest.approx(0.72)
        assert loaded["hidden_city_via"] == "LHR"

    def test_store_and_load_airport(self, ravendb_session):
        airport = AirportDocument(
            iata="WAW",
            name="Warsaw Chopin",
            city="Warsaw",
            country="PL",
            coordinates=Coordinates(lat=52.1657, lng=20.9671),
        )
        ravendb_session.store(airport.model_dump(), airport.airport_id())
        ravendb_session.save_changes()

        loaded = ravendb_session.load(airport.airport_id())
        assert loaded["iata"] == "WAW"
        assert loaded["coordinates"]["lat"] == pytest.approx(52.1657)

    def test_query_routes_by_origin(self, ravendb_session):
        for destination in ["LHR", "FRA"]:
            route = RouteDocument(
                origin="WAW",
                destination=destination,
                typical_price=TypicalPrice(min=100.0, max=200.0),
            )
            ravendb_session.store(route.model_dump(), route.route_id())
        ravendb_session.save_changes()

        results = list(
            ravendb_session.query(collection="Routes")
            .where_equals("origin", "WAW")
            .all()
        )
        origins = {r["origin"] for r in results}
        assert origins == {"WAW"}

    def test_query_hidden_city_candidates(self, ravendb_session):
        routes = [
            RouteDocument(
                origin="WAW",
                destination="JFK",
                hubs=["LHR"],
                typical_price=TypicalPrice(min=185.0, max=280.0),
                hidden_city_score=0.72,
            ),
            RouteDocument(
                origin="WAW",
                destination="LHR",
                typical_price=TypicalPrice(min=310.0, max=420.0),
                hidden_city_score=0.0,
            ),
        ]
        for route in routes:
            ravendb_session.store(route.model_dump(), route.route_id())
        ravendb_session.save_changes()

        candidates = list(
            ravendb_session.query(collection="Routes")
            .where_greater_than("hidden_city_score", 0.5)
            .all()
        )
        assert all(r["hidden_city_score"] > 0.5 for r in candidates)
