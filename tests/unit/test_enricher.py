import pytest

from src.db.models import RouteDocument, TypicalPrice
from src.hidden_city.enricher import enrich_hidden_city


def make_route(origin: str, destination: str, price_min: float, hubs: list[str] = None) -> RouteDocument:
    return RouteDocument(
        origin=origin,
        destination=destination,
        hubs=hubs or [],
        typical_price=TypicalPrice(min=price_min, max=price_min * 1.2),
    )


class TestEnrichHiddenCity:
    def test_detects_hidden_city_candidate(self):
        # LHR direct=600, JFK-via-LHR=220 → 63% savings → score=0.63 > 0.5
        routes = [
            make_route("WAW", "LHR", price_min=600.0),
            make_route("WAW", "JFK", price_min=220.0, hubs=["LHR"]),
        ]
        enriched = enrich_hidden_city(routes)

        jfk = next(r for r in enriched if r.destination == "JFK")
        assert jfk.hidden_city_score > 0.5
        assert jfk.hidden_city_via == "LHR"
        assert jfk.hidden_city_decoy == "JFK"

    def test_direct_route_not_flagged(self):
        routes = [
            make_route("WAW", "LHR", price_min=600.0),
            make_route("WAW", "JFK", price_min=220.0, hubs=["LHR"]),
        ]
        enriched = enrich_hidden_city(routes)

        lhr = next(r for r in enriched if r.destination == "LHR")
        assert lhr.hidden_city_score == 0.0
        assert lhr.hidden_city_via is None

    def test_no_hidden_city_when_hub_route_not_cheaper(self):
        routes = [
            make_route("WAW", "LHR", price_min=200.0),
            make_route("WAW", "JFK", price_min=350.0, hubs=["LHR"]),  # more expensive
        ]
        enriched = enrich_hidden_city(routes)

        jfk = next(r for r in enriched if r.destination == "JFK")
        assert jfk.hidden_city_score == 0.0

    def test_no_hub_info_skipped(self):
        routes = [make_route("WAW", "JFK", price_min=185.0, hubs=[])]
        enriched = enrich_hidden_city(routes)
        assert enriched[0].hidden_city_score == 0.0

    def test_best_candidate_wins_when_multiple_hubs(self):
        # BOS-via-LHR saves 63% vs LHR direct (600→220)
        # BOS-via-FRA saves 0% vs FRA direct (90→220, actually more expensive — skipped)
        # LHR hub gives better score → should win
        routes = [
            make_route("WAW", "FRA", price_min=90.0),
            make_route("WAW", "LHR", price_min=600.0),
            make_route("WAW", "BOS", price_min=220.0, hubs=["LHR", "FRA"]),
        ]
        enriched = enrich_hidden_city(routes)
        bos = next(r for r in enriched if r.destination == "BOS")

        # LHR: savings = 380, score ≈ 0.63. FRA: hidden (220) > direct (90) → score = 0
        assert bos.hidden_city_via == "LHR"

    def test_empty_input(self):
        assert enrich_hidden_city([]) == []

    def test_output_length_matches_input(self):
        routes = [
            make_route("WAW", "LHR", price_min=600.0),
            make_route("WAW", "JFK", price_min=220.0, hubs=["LHR"]),
        ]
        assert len(enrich_hidden_city(routes)) == len(routes)
