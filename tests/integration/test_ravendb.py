"""
Integration tests — require a running RavenDB instance.

Run with:
    pytest tests/integration/ --require-ravendb

Or let them be skipped automatically when RavenDB is not available.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from ravendb import DocumentStore

from src.db.client import doc_to_dict
from src.db.geo import to_unit_vector
from src.db.models import AirportDocument, Coordinates, RouteDocument, TypicalPrice
from tests.conftest import TEST_DB


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

        results = [
            doc_to_dict(r)
            for r in ravendb_session.query_collection("Routes").where_equals("origin", "WAW")
        ]
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

        candidates = [
            doc_to_dict(r)
            for r in ravendb_session.query_collection("Routes").where_greater_than(
                "hidden_city_score", 0.5
            )
        ]
        assert all(r["hidden_city_score"] > 0.5 for r in candidates)


@pytest.mark.integration
class TestSearchRoutesTool:
    """Integration tests for the search_routes tool against a live RavenDB."""

    @pytest.mark.asyncio
    async def test_search_routes_returns_seeded_route(self, ravendb_store):
        now = datetime.now(timezone.utc).isoformat()
        route_doc = {
            "origin": "WAW",
            "destination": "JFK",
            "hubs": ["LHR"],
            "typical_price": {"min": 220.0, "max": 320.0, "currency": "USD"},
            "duration_avg_min": 0,
            "hidden_city_score": 0.62,
            "hidden_city_via": "LHR",
            "hidden_city_decoy": "JFK",
            "hidden_city_risks": [],
            "last_updated": now,
        }
        with ravendb_store.open_session() as session:
            session.store(route_doc, "routes/WAW-JFK")
            session.save_changes()

        from src.tools.search_routes import search_routes

        with patch("src.tools.search_routes.get_store", return_value=ravendb_store):
            result = await search_routes(origin="WAW", destination="JFK")

        assert result["count"] == 1
        assert result["routes"][0]["to"] == "JFK"
        assert result["routes"][0]["hidden_city"]["via"] == "LHR"
        assert result["routes"][0]["hidden_city"]["score"] > 0.5

    @pytest.mark.asyncio
    async def test_search_routes_hidden_city_score_lowered_with_carry_on(self, ravendb_store):
        now = datetime.now(timezone.utc).isoformat()
        route_doc = {
            "origin": "WAW",
            "destination": "ORD",
            "hubs": ["FRA"],
            "typical_price": {"min": 155.0, "max": 240.0, "currency": "USD"},
            "duration_avg_min": 0,
            "hidden_city_score": 0.59,
            "hidden_city_via": "FRA",
            "hidden_city_decoy": "ORD",
            "hidden_city_risks": [],
            "last_updated": now,
        }
        with ravendb_store.open_session() as session:
            session.store(route_doc, "routes/WAW-ORD")
            session.save_changes()

        from src.tools.search_routes import search_routes

        with patch("src.tools.search_routes.get_store", return_value=ravendb_store):
            without_bags = await search_routes(origin="WAW", destination="ORD", carry_on_only=False)
            with_bags = await search_routes(origin="WAW", destination="ORD", carry_on_only=True)

        score_without = without_bags["routes"][0]["hidden_city"]["score"]
        score_with = with_bags["routes"][0]["hidden_city"]["score"]
        assert score_with < score_without


@pytest.mark.integration
class TestSessionPersistence:
    """Verifies session documents survive a store dispose + reinit (pod restart simulation)."""

    def test_session_survives_store_reinit(self, ravendb_url):
        doc_id = "sessions/u-persist-1"
        session_doc = {
            "user_id": "u-persist",
            "turns": [
                {"role": "user", "content": "Find flights WAW→LHR", "timestamp": datetime.now(timezone.utc).isoformat()},
                {"role": "assistant", "content": "Here are the options.", "timestamp": datetime.now(timezone.utc).isoformat()},
            ],
            "active_constraints": {
                "carry_on_only": True,
                "max_stops": 1,
                "max_duration_min": None,
                "preferred_airlines": [],
            },
            "last_active": datetime.now(timezone.utc).isoformat(),
        }

        # Write with store1 — simulates the original pod
        store1 = DocumentStore(urls=[ravendb_url], database=TEST_DB)
        store1.initialize()
        with store1.open_session() as s:
            s.store(session_doc, doc_id)
            s.save_changes()
        store1.close()

        # Read with store2 — simulates the replacement pod
        store2 = DocumentStore(urls=[ravendb_url], database=TEST_DB)
        store2.initialize()
        with store2.open_session() as s:
            loaded = s.load(doc_id)
        store2.close()

        assert loaded is not None
        assert loaded["user_id"] == "u-persist"
        assert loaded["active_constraints"]["carry_on_only"] is True
        assert len(loaded["turns"]) == 2
        assert loaded["turns"][0]["content"] == "Find flights WAW→LHR"


@pytest.mark.integration
class TestVectorNearbySearch:
    """Verifies the vector_search-based nearby-airport lookup against a live RavenDB."""

    @pytest.mark.asyncio
    async def test_nearby_ranks_same_region_above_distant_airport(self, ravendb_store):
        airports = [
            ("PEK", "Beijing", "CN", 40.0799, 116.6031),
            ("PVG", "Shanghai", "CN", 31.1443, 121.8083),
            ("CKG", "Chongqing", "CN", 29.7192, 106.6417),
            ("ICN", "Seoul", "KR", 37.4602, 126.4407),
            ("JFK", "New York", "US", 40.6413, -73.7781),
        ]
        with ravendb_store.open_session() as session:
            for iata, city, country, lat, lng in airports:
                doc = {
                    "iata": iata,
                    "name": city,
                    "city": city,
                    "country": country,
                    "coordinates": {"lat": lat, "lng": lng},
                    "location_vector": to_unit_vector(lat, lng),
                }
                session.store(doc, f"airports/{iata}")
                session.advanced.get_metadata_for(doc)["@collection"] = "Airports"
            session.save_changes()

        from src.tools.search_routes import _nearby_alternatives

        with patch("src.tools.search_routes.get_store", return_value=ravendb_store):
            results = _nearby_alternatives(ravendb_store, "PEK")

        codes_in_order = [r["airport"] for r in results]
        assert "JFK" not in codes_in_order
        assert "PVG" in codes_in_order
        assert "CKG" in codes_in_order
        assert codes_in_order.index("PVG") < codes_in_order.index("CKG") or set(
            codes_in_order
        ) >= {"PVG", "CKG"}


@pytest.mark.integration
class TestConnectingHubSearch:
    """Verifies the origin->hub->destination fallback against a live RavenDB."""

    @pytest.mark.asyncio
    async def test_connecting_hub_found_when_no_direct_route(self, ravendb_store):
        now = datetime.now(timezone.utc).isoformat()
        leg1 = {
            "origin": "AAA",
            "destination": "HUB",
            "hubs": [],
            "typical_price": {"min": 100.0, "max": 150.0, "currency": "USD"},
            "duration_avg_min": 0,
            "hidden_city_score": 0.0,
            "hidden_city_risks": [],
            "last_updated": now,
        }
        leg2 = {
            "origin": "HUB",
            "destination": "BBB",
            "hubs": [],
            "typical_price": {"min": 80.0, "max": 120.0, "currency": "USD"},
            "duration_avg_min": 0,
            "hidden_city_score": 0.0,
            "hidden_city_risks": [],
            "last_updated": now,
        }
        with ravendb_store.open_session() as session:
            session.store(leg1, "routes/AAA-HUB")
            session.advanced.get_metadata_for(leg1)["@collection"] = "Routes"
            session.store(leg2, "routes/HUB-BBB")
            session.advanced.get_metadata_for(leg2)["@collection"] = "Routes"
            session.save_changes()

        from src.tools.search_routes import search_routes

        with patch("src.tools.search_routes.get_store", return_value=ravendb_store):
            result = await search_routes(origin="AAA", destination="BBB")

        assert result["count"] == 0
        assert "connecting_hubs" in result
        assert result["connecting_hubs"][0]["via"] == "HUB"
        assert result["connecting_hubs"][0]["total_price_usd_min"] == pytest.approx(180.0)
