"""
Unit tests for individual tool implementations — RavenDB and HTTP clients mocked.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.save_conversation import persist_turn, update_constraints
from src.tools.search_routes import _is_stale, search_routes
from src.tools.update_user_profile import update_user_profile

WARN_TOOL_TOKENS = 800  # mirrors src/agent/loop.py


class TestIsStale:
    def test_none_is_stale(self):
        assert _is_stale(None) is True

    def test_fresh_timestamp(self):
        ts = datetime.now(timezone.utc).isoformat()
        assert _is_stale(ts) is False

    def test_old_timestamp(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        assert _is_stale(old) is True

    def test_exactly_at_boundary(self):
        boundary = (datetime.now(timezone.utc) - timedelta(hours=2, seconds=1)).isoformat()
        assert _is_stale(boundary) is True

    def test_invalid_string_is_stale(self):
        assert _is_stale("not-a-date") is True


class TestSearchRoutes:
    def _mock_session_with_routes(self, routes: list[dict]):
        mock_query = MagicMock()
        mock_query.where_equals.return_value = mock_query
        mock_query.where_greater_than.return_value = mock_query
        mock_query.take.return_value = mock_query
        mock_query.__iter__ = MagicMock(side_effect=lambda: iter(routes))

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value = mock_query

        mock_store = MagicMock()
        mock_store.open_session.return_value = mock_session
        return mock_store

    @pytest.mark.asyncio
    async def test_returns_routes(self):
        routes = [
            {
                "origin": "WAW",
                "destination": "JFK",
                "hubs": ["LHR"],
                "typical_price": {"min": 185, "max": 280},
                "hidden_city_score": 0.72,
                "hidden_city_via": "LHR",
                "hidden_city_decoy": "JFK",
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
        ]
        mock_store = self._mock_session_with_routes(routes)

        with patch("src.tools.search_routes.get_store", return_value=mock_store):
            result = await search_routes(origin="WAW")

        assert result["count"] == 1
        assert result["routes"][0]["from"] == "WAW"
        assert "hidden_city" in result["routes"][0]

    @pytest.mark.asyncio
    async def test_empty_results(self):
        mock_store = self._mock_session_with_routes([])

        with patch("src.tools.search_routes.get_store", return_value=mock_store):
            result = await search_routes(origin="WAW", destination="ZZZ")

        assert result["count"] == 0
        assert result["routes"] == []
        assert "nearby_alternatives" not in result

    @pytest.mark.asyncio
    async def test_empty_results_surfaces_nearby_alternative(self):
        """No KRK routes are seeded, but KRK's airport doc lists WAW as nearby (290km,
        train available) — the tool should surface it under near_origin (KRK is the
        origin here) for the agent to ask about, never auto-substitute it into the
        results. LHR (the destination) has no nearby doc configured in this test, so
        near_destination stays empty — confirming the two sides are kept separate."""
        mock_store = self._mock_session_with_routes([])
        mock_store.open_session.return_value.load.side_effect = lambda key: (
            {"nearby": [{"iata": "WAW", "distance_km": 290, "train": True}]}
            if key == "airports/KRK"
            else None
        )

        with (
            patch("src.tools.search_routes.get_store", return_value=mock_store),
            patch(
                "src.tools.search_routes.load_airport_names",
                return_value={"WAW": {"city": "Warsaw"}},
            ),
        ):
            result = await search_routes(origin="KRK", destination="LHR")

        assert result["count"] == 0
        assert result["nearby_alternatives"]["near_origin"] == [
            {"airport": "WAW", "distance_km": 290, "train": True, "city": "Warsaw"}
        ]
        assert result["nearby_alternatives"]["near_destination"] == []

    @pytest.mark.asyncio
    async def test_carry_on_lowers_hidden_city_score(self):
        routes = [
            {
                "origin": "WAW",
                "destination": "JFK",
                "hubs": ["LHR"],
                "typical_price": {"min": 185, "max": 280},
                "hidden_city_score": 0.72,
                "hidden_city_via": "LHR",
                "hidden_city_decoy": "JFK",
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
        ]
        mock_store = self._mock_session_with_routes(routes)

        with patch("src.tools.search_routes.get_store", return_value=mock_store):
            no_risk = await search_routes(origin="WAW", carry_on_only=False)
            with_risk = await search_routes(origin="WAW", carry_on_only=True)

        no_risk_score = no_risk["routes"][0]["hidden_city"]["score"]
        with_risk_score = with_risk["routes"][0]["hidden_city"]["score"]
        assert with_risk_score < no_risk_score
        assert "checked_baggage" in with_risk["routes"][0]["hidden_city"]["risks"]

    @pytest.mark.asyncio
    async def test_stale_flag_propagated(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        routes = [
            {
                "origin": "WAW",
                "destination": "LHR",
                "hubs": [],
                "typical_price": {"min": 310, "max": 420},
                "hidden_city_score": 0.0,
                "last_updated": old_ts,
            }
        ]
        mock_store = self._mock_session_with_routes(routes)

        with patch("src.tools.search_routes.get_store", return_value=mock_store):
            result = await search_routes(origin="WAW")

        assert result["routes"][0]["stale"] is True

    @pytest.mark.asyncio
    async def test_budget_max_excludes_pricier_routes(self):
        routes = [
            {
                "origin": "WAW",
                "destination": "JFK",
                "hubs": ["LHR"],
                "typical_price": {"min": 185, "max": 280, "currency": "USD"},
                "hidden_city_score": 0.0,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            },
            {
                "origin": "WAW",
                "destination": "LHR",
                "hubs": [],
                "typical_price": {"min": 90, "max": 150, "currency": "USD"},
                "hidden_city_score": 0.0,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            },
        ]
        mock_store = self._mock_session_with_routes(routes)

        with patch("src.tools.search_routes.get_store", return_value=mock_store):
            result = await search_routes(origin="WAW", budget_max=200, budget_currency="USD")

        assert result["count"] == 1
        assert result["routes"][0]["to"] == "LHR"

    @pytest.mark.asyncio
    async def test_budget_max_ignored_on_currency_mismatch(self):
        """No FX conversion — a route priced in a different currency is left unfiltered."""
        routes = [
            {
                "origin": "WAW",
                "destination": "JFK",
                "hubs": ["LHR"],
                "typical_price": {"min": 1000, "max": 1500, "currency": "PLN"},
                "hidden_city_score": 0.0,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
        ]
        mock_store = self._mock_session_with_routes(routes)

        with patch("src.tools.search_routes.get_store", return_value=mock_store):
            result = await search_routes(origin="WAW", budget_max=200, budget_currency="USD")

        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_countries_of_interest_filters_by_destination_country(self):
        routes = [
            {
                "origin": "WAW",
                "destination": "JFK",
                "hubs": ["LHR"],
                "typical_price": {"min": 185, "max": 280},
                "hidden_city_score": 0.0,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            },
            {
                "origin": "WAW",
                "destination": "LHR",
                "hubs": [],
                "typical_price": {"min": 90, "max": 150},
                "hidden_city_score": 0.0,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            },
        ]
        mock_store = self._mock_session_with_routes(routes)
        airport_countries = {
            "WAW": {"city": "Warsaw", "country": "Poland"},
            "JFK": {"city": "New York", "country": "United States"},
            "LHR": {"city": "London", "country": "United Kingdom"},
        }

        with (
            patch("src.tools.search_routes.get_store", return_value=mock_store),
            patch("src.tools.search_routes.load_airport_names", return_value=airport_countries),
        ):
            result = await search_routes(origin="WAW", countries_of_interest=["United Kingdom"])

        assert result["count"] == 1
        assert result["routes"][0]["to"] == "LHR"

    @pytest.mark.asyncio
    async def test_result_within_800_token_budget(self):
        """Tool result must stay inside the 800-token budget enforced by the agent loop."""
        now = datetime.now(timezone.utc).isoformat()
        routes = [
            {
                "origin": "WAW",
                "destination": f"DST{i}",
                "hubs": ["LHR"],
                "typical_price": {"min": 200.0 + i * 10, "max": 300.0 + i * 10},
                "hidden_city_score": 0.62,
                "hidden_city_via": "LHR",
                "hidden_city_decoy": f"DST{i}",
                "last_updated": now,
            }
            for i in range(5)
        ]
        mock_store = self._mock_session_with_routes(routes)

        with patch("src.tools.search_routes.get_store", return_value=mock_store):
            result = await search_routes(origin="WAW")

        result_json = json.dumps(result)
        estimated_tokens = len(result_json) // 4
        assert estimated_tokens <= WARN_TOOL_TOKENS, (
            f"search_routes result is ~{estimated_tokens} tokens — exceeds {WARN_TOOL_TOKENS}-token budget"
        )


class TestSearchRoutesHiddenCity:
    def _mock_store(self, candidate_routes: list[dict], direct_route_doc: dict | None):
        mock_query = MagicMock()
        mock_query.where_equals.return_value = mock_query
        mock_query.take.return_value = mock_query
        mock_query.__iter__ = MagicMock(side_effect=lambda: iter(candidate_routes))

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value = mock_query
        mock_session.load.return_value = direct_route_doc

        mock_store = MagicMock()
        mock_store.open_session.return_value = mock_session
        return mock_store

    @pytest.mark.asyncio
    async def test_surfaces_candidate_via_non_best_hub(self):
        """WAW->ORD (hubs=[FRA, AMS]) only got hidden_city_via="FRA" baked in at seed
        time because FRA scored higher than AMS for that specific route. A search for
        real_destination=AMS must still surface ORD as a valid decoy — the query
        should check every hub on the route, not just whichever one happened to win
        at enrichment time. WAW->EWR (hubs=[LHR, AMS]) is included too and must be
        excluded, since its savings via AMS alone fall below the surface threshold."""
        now = datetime.now(timezone.utc).isoformat()
        direct_route = {
            "origin": "WAW",
            "destination": "AMS",
            "typical_price": {"min": 320, "max": 420},
            "last_updated": now,
        }
        candidates = [
            {
                "origin": "WAW",
                "destination": "ORD",
                "hubs": ["FRA", "AMS"],
                "typical_price": {"min": 155, "max": 240},
                "hidden_city_score": 0.592,
                "hidden_city_via": "FRA",
                "hidden_city_decoy": "ORD",
                "last_updated": now,
            },
            {
                "origin": "WAW",
                "destination": "EWR",
                "hubs": ["LHR", "AMS"],
                "typical_price": {"min": 230, "max": 330},
                "hidden_city_score": 0.603,
                "hidden_city_via": "LHR",
                "hidden_city_decoy": "EWR",
                "last_updated": now,
            },
        ]
        mock_store = self._mock_store(candidates, direct_route)

        with (
            patch("src.tools.search_routes.get_store", return_value=mock_store),
            patch("src.tools.search_routes.load_airport_names", return_value={}),
        ):
            result = await search_routes(origin="WAW", real_destination="AMS")

        assert result["count"] == 1
        assert result["routes"][0]["to"] == "ORD"
        assert result["routes"][0]["hidden_city"]["via"] == "AMS"
        assert result["routes"][0]["hidden_city"]["decoy_to"] == "ORD"
        assert result["routes"][0]["hidden_city"]["score"] > 0.5

    @pytest.mark.asyncio
    async def test_no_direct_price_returns_note_instead_of_guessing(self):
        mock_store = self._mock_store([], direct_route_doc=None)

        with patch("src.tools.search_routes.get_store", return_value=mock_store):
            result = await search_routes(origin="WAW", real_destination="AMS")

        assert result["count"] == 0
        assert result["routes"] == []
        assert "note" in result

    @pytest.mark.asyncio
    async def test_stale_direct_price_returns_note_instead_of_scoring(self):
        """A >2h-old direct price is treated the same as a missing one — scoring
        against a stale baseline would produce an unreliable hidden-city score."""
        stale_ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        direct_route = {
            "origin": "WAW",
            "destination": "AMS",
            "typical_price": {"min": 320, "max": 420},
            "last_updated": stale_ts,
        }
        mock_store = self._mock_store([], direct_route)

        with patch("src.tools.search_routes.get_store", return_value=mock_store):
            result = await search_routes(origin="WAW", real_destination="AMS")

        assert result["count"] == 0
        assert "note" in result

    @pytest.mark.asyncio
    async def test_carry_on_only_can_drop_candidate_below_threshold(self):
        now = datetime.now(timezone.utc).isoformat()
        direct_route = {
            "origin": "WAW",
            "destination": "AMS",
            "typical_price": {"min": 320, "max": 420},
            "last_updated": now,
        }
        candidates = [
            {
                "origin": "WAW",
                "destination": "ORD",
                "hubs": ["FRA", "AMS"],
                "typical_price": {"min": 155, "max": 240},
                "hidden_city_score": 0.592,
                "hidden_city_via": "FRA",
                "hidden_city_decoy": "ORD",
                "last_updated": now,
            },
        ]
        mock_store = self._mock_store(candidates, direct_route)

        with (
            patch("src.tools.search_routes.get_store", return_value=mock_store),
            patch("src.tools.search_routes.load_airport_names", return_value={}),
        ):
            result = await search_routes(
                origin="WAW", real_destination="AMS", carry_on_only=True
            )

        assert result["count"] == 0


class TestSaveConversation:
    def _make_mock_store(self, existing_doc=None):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.load.return_value = existing_doc

        stored_docs = {}
        mock_executor = MagicMock()
        mock_executor.execute = MagicMock(
            side_effect=lambda cmd: stored_docs.update({cmd.key: cmd.document})
        )

        mock_store = MagicMock()
        mock_store.open_session.return_value = mock_session
        mock_store.get_request_executor.return_value = mock_executor
        return mock_store, stored_docs

    @pytest.mark.asyncio
    async def test_creates_new_session(self):
        mock_store, stored_docs = self._make_mock_store(existing_doc=None)

        with patch("src.tools.save_conversation.get_store", return_value=mock_store):
            result = await persist_turn(
                user_id="u1",
                session_id="1",
                user_message="Find flights",
                assistant_response="Here are flights.",
            )

        assert result["saved"] is True
        assert result["total_turns"] == 2  # user + assistant
        assert mock_store.get_request_executor().execute.called
        doc = stored_docs["sessions/u1-1"]
        assert len(doc["turns"]) == 2

    @pytest.mark.asyncio
    async def test_updates_constraints(self):
        existing = {
            "user_id": "u1",
            "turns": [],
            "active_constraints": {
                "carry_on_only": False,
                "max_stops": 2,
                "max_duration_min": None,
                "preferred_airlines": [],
            },
            "last_active": datetime.now(timezone.utc).isoformat(),
        }
        mock_store, stored_docs = self._make_mock_store(existing_doc=existing)

        with patch("src.tools.save_conversation.get_store", return_value=mock_store):
            await update_constraints(
                user_id="u1",
                session_id="1",
                constraints={"carry_on_only": True},
            )

        doc = stored_docs["sessions/u1-1"]
        assert doc["active_constraints"]["carry_on_only"] is True
        assert doc["active_constraints"]["max_stops"] == 2  # unchanged


class TestUpdateUserProfile:
    def _make_mock_store(self, existing_doc=None):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.load.return_value = existing_doc

        stored_docs = {}
        mock_executor = MagicMock()
        mock_executor.execute = MagicMock(
            side_effect=lambda cmd: stored_docs.update({cmd.key: cmd.document})
        )

        mock_store = MagicMock()
        mock_store.open_session.return_value = mock_session
        mock_store.get_request_executor.return_value = mock_executor
        return mock_store, stored_docs

    @pytest.mark.asyncio
    async def test_creates_user_doc_when_none_exists(self):
        mock_store, stored_docs = self._make_mock_store(existing_doc=None)

        with patch("src.tools.update_user_profile.get_store", return_value=mock_store):
            result = await update_user_profile(user_id="u1", carry_on_only=True)

        assert result["saved"] is True
        assert "carry_on_only" in result["updated_fields"]
        assert stored_docs["users/u1"]["carry_on_only"] is True

    @pytest.mark.asyncio
    async def test_merges_with_existing_doc(self):
        existing = {"carry_on_only": False, "home_airport": "WAW", "preferred_airlines": []}
        mock_store, stored_docs = self._make_mock_store(existing_doc=existing)

        with patch("src.tools.update_user_profile.get_store", return_value=mock_store):
            result = await update_user_profile(
                user_id="u1",
                carry_on_only=True,
                loyalty_programs=["Miles & More"],
            )

        doc = stored_docs["users/u1"]
        assert doc["carry_on_only"] is True
        assert doc["home_airport"] == "WAW"  # preserved
        assert doc["loyalty_programs"] == ["Miles & More"]
        assert result["saved"] is True

    @pytest.mark.asyncio
    async def test_home_airport_uppercased(self):
        mock_store, stored_docs = self._make_mock_store(existing_doc=None)

        with patch("src.tools.update_user_profile.get_store", return_value=mock_store):
            await update_user_profile(user_id="u1", home_airport="waw")

        assert stored_docs["users/u1"]["home_airport"] == "WAW"

    @pytest.mark.asyncio
    async def test_no_fields_returns_not_saved(self):
        mock_store, stored_docs = self._make_mock_store(existing_doc=None)

        with patch("src.tools.update_user_profile.get_store", return_value=mock_store):
            result = await update_user_profile(user_id="u1")

        assert result["saved"] is False
        assert stored_docs == {}  # nothing written to RavenDB
