import pytest
from src.hidden_city.scorer import (
    HiddenCityCandidate,
    RiskFactor,
    find_candidates,
    score_candidate,
    MIN_SAVINGS_USD,
    MIN_SCORE_TO_SURFACE,
)


class TestScoreCandidate:
    def test_no_savings_returns_zero(self):
        assert score_candidate(1000.0, 1000.0, []) == 0.0

    def test_hidden_more_expensive_returns_zero(self):
        assert score_candidate(1000.0, 1200.0, []) == 0.0

    def test_50_pct_savings_no_risk(self):
        score = score_candidate(2000.0, 1000.0, [])
        assert score == pytest.approx(0.5)

    def test_checked_baggage_kills_score(self):
        # 50% savings but checked baggage multiplier = 0.2
        score = score_candidate(2000.0, 1000.0, [RiskFactor.CHECKED_BAGGAGE])
        assert score == pytest.approx(0.5 * 0.2)

    def test_multiple_risks_multiply(self):
        # checked_baggage (0.2) * return_same_booking (0.3) = 0.06
        score = score_candidate(
            2000.0, 1000.0,
            [RiskFactor.CHECKED_BAGGAGE, RiskFactor.RETURN_SAME_BOOKING],
        )
        assert score == pytest.approx(0.5 * 0.2 * 0.3)

    def test_score_capped_at_one(self):
        # even 99% savings can't exceed 1.0
        score = score_candidate(10000.0, 1.0, [])
        assert score >= 0.999

    def test_score_is_float(self):
        assert isinstance(score_candidate(2000.0, 1500.0, []), float)


class TestHiddenCityCandidate:
    def _make(self, price_direct=2800.0, price_hidden=1650.0, risks=None):
        return HiddenCityCandidate(
            origin="WAW",
            real_destination="LHR",
            decoy_destination="JFK",
            price_direct=price_direct,
            price_hidden=price_hidden,
            risks=risks or [],
        )

    def test_savings_calculation(self):
        c = self._make(2800.0, 1650.0)
        assert c.savings == pytest.approx(1150.0)

    def test_savings_pct(self):
        c = self._make(2000.0, 1000.0)
        assert c.savings_pct == pytest.approx(0.5)

    def test_should_surface_true(self):
        c = self._make(2800.0, 1300.0)  # 53.6% savings → score 0.536 > 0.5
        assert c.should_surface is True

    def test_should_surface_false_below_min_savings(self):
        # savings = 1 USD below MIN_SAVINGS_USD
        c = self._make(1000.0, 1000.0 - MIN_SAVINGS_USD + 1)
        assert c.should_surface is False

    def test_should_surface_false_below_min_score(self):
        # big risk kills the score below threshold
        c = self._make(
            2000.0, 1800.0,
            risks=[RiskFactor.CHECKED_BAGGAGE, RiskFactor.RETURN_SAME_BOOKING],
        )
        assert c.score < MIN_SCORE_TO_SURFACE
        assert c.should_surface is False


class TestFindCandidates:
    def test_returns_only_surfaceable(self):
        through_routes = [
            {"destination": "JFK", "price": 1200.0},   # 57% savings → score 0.571 > 0.5
            {"destination": "BOS", "price": 2900.0},   # hidden more expensive
            {"destination": "ORD", "price": 2750.0},   # savings small, score well below MIN_SCORE_TO_SURFACE
        ]
        candidates = find_candidates("WAW", "LHR", 2800.0, through_routes, [])
        assert len(candidates) == 1
        assert candidates[0].decoy_destination == "JFK"

    def test_sorted_by_score_descending(self):
        through_routes = [
            {"destination": "JFK", "price": 1650.0},   # ~41% savings
            {"destination": "ORD", "price": 1000.0},   # ~64% savings
        ]
        candidates = find_candidates("WAW", "LHR", 2800.0, through_routes, [])
        assert candidates[0].decoy_destination == "ORD"

    def test_empty_when_no_candidates(self):
        candidates = find_candidates("WAW", "LHR", 1000.0, [], [])
        assert candidates == []

    def test_risks_applied_to_all_candidates(self):
        # 90% savings → score 0.9; with SHORT_CONNECTION (×0.8) → 0.72 — both surface
        through_routes = [{"destination": "JFK", "price": 280.0}]
        no_risk = find_candidates("WAW", "LHR", 2800.0, through_routes, [])
        with_risk = find_candidates(
            "WAW", "LHR", 2800.0, through_routes, [RiskFactor.SHORT_CONNECTION]
        )
        assert with_risk[0].score < no_risk[0].score
