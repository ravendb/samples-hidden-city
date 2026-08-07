from datetime import datetime, timezone

import pytest

from src.db.models import (
    ActiveConstraints,
    AirportDocument,
    Coordinates,
    ConversationTurn,
    RouteDocument,
    SessionDocument,
    TypicalPrice,
)


class TestRouteDocument:
    def _route(self, **kwargs) -> RouteDocument:
        defaults = dict(
            origin="WAW",
            destination="JFK",
            hubs=["LHR"],
            typical_price=TypicalPrice(min=185.0, max=280.0),
        )
        return RouteDocument(**(defaults | kwargs))

    def test_route_id_format(self):
        assert self._route().route_id() == "routes/WAW-JFK"

    def test_is_stale_fresh(self):
        route = self._route()
        assert route.is_stale(max_age_hours=2.0) is False

    def test_is_stale_old(self):
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        route = self._route(last_updated=old_time)
        assert route.is_stale(max_age_hours=2.0) is True

    def test_default_hidden_city_score(self):
        assert self._route().hidden_city_score == 0.0

    def test_hubs_default_empty(self):
        route = RouteDocument(
            origin="WAW",
            destination="LHR",
            typical_price=TypicalPrice(min=310.0, max=420.0),
        )
        assert route.hubs == []


class TestAirportDocument:
    def test_airport_id_format(self):
        airport = AirportDocument(
            iata="WAW",
            name="Warsaw Chopin",
            city="Warsaw",
            country="PL",
            coordinates=Coordinates(lat=52.1657, lng=20.9671),
        )
        assert airport.airport_id() == "airports/WAW"

    def test_location_vector_defaults_empty(self):
        airport = AirportDocument(
            iata="DOH",
            name="Hamad International",
            city="Doha",
            country="QA",
            coordinates=Coordinates(lat=25.27, lng=51.61),
        )
        assert airport.location_vector == []


class TestSessionDocument:
    def test_last_n_turns_returns_slice(self):
        turns = [
            ConversationTurn(role="user", content=f"msg {i}")
            for i in range(15)
        ]
        session = SessionDocument(user_id="u1", turns=turns)
        last = session.last_n_turns(5)
        assert len(last) == 5
        assert last[-1].content == "msg 14"

    def test_last_n_turns_fewer_than_n(self):
        turns = [ConversationTurn(role="user", content="hi")]
        session = SessionDocument(user_id="u1", turns=turns)
        assert len(session.last_n_turns(10)) == 1

    def test_session_id_format(self):
        session = SessionDocument(user_id="user-42")
        assert session.session_id(3) == "sessions/user-42-3"

    def test_active_constraints_defaults(self):
        session = SessionDocument(user_id="u1")
        assert session.active_constraints.carry_on_only is False
        assert session.active_constraints.max_stops == 2
