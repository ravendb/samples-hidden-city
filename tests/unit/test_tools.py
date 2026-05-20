"""
Unit tests for individual tool implementations — RavenDB and HTTP clients mocked.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.save_conversation import save_conversation
from src.tools.search_routes import _is_stale, search_routes


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
        mock_query.all = MagicMock(side_effect=lambda: iter(routes))

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


class TestSaveConversation:
    @pytest.mark.asyncio
    async def test_creates_new_session(self):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.load.return_value = None  # no existing session
        stored_docs = {}
        mock_session.store = lambda doc, doc_id: stored_docs.update({doc_id: doc})
        mock_session.save_changes = MagicMock()

        mock_store = MagicMock()
        mock_store.open_session.return_value = mock_session

        with patch("src.tools.save_conversation.get_store", return_value=mock_store):
            result = await save_conversation(
                user_id="u1",
                session_id="1",
                user_message="Find flights",
                assistant_response="Here are flights.",
            )

        assert result["saved"] is True
        assert result["total_turns"] == 2  # user + assistant
        assert mock_session.save_changes.called
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
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.load.return_value = existing
        stored_docs = {}
        mock_session.store = lambda doc, doc_id: stored_docs.update({doc_id: doc})
        mock_session.save_changes = MagicMock()

        mock_store = MagicMock()
        mock_store.open_session.return_value = mock_session

        with patch("src.tools.save_conversation.get_store", return_value=mock_store):
            await save_conversation(
                user_id="u1",
                session_id="1",
                user_message="Only carry-on",
                assistant_response="Noted.",
                constraints={"carry_on_only": True},
            )

        assert mock_session.save_changes.called
        doc = stored_docs["sessions/u1-1"]
        assert doc["active_constraints"]["carry_on_only"] is True
        assert doc["active_constraints"]["max_stops"] == 2  # unchanged
