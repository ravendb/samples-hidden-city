from dataclasses import dataclass, field
from enum import Enum


MIN_SAVINGS_PLN = 100.0
MIN_SCORE_TO_SURFACE = 0.5


class RiskFactor(Enum):
    CHECKED_BAGGAGE = "checked_baggage"
    RETURN_SAME_BOOKING = "return_same_booking"
    LOW_COST_CARRIER = "low_cost_carrier"
    SHORT_CONNECTION = "short_connection"


_RISK_MULTIPLIERS: dict[RiskFactor, float] = {
    RiskFactor.CHECKED_BAGGAGE: 0.2,
    RiskFactor.RETURN_SAME_BOOKING: 0.3,
    RiskFactor.LOW_COST_CARRIER: 0.7,
    RiskFactor.SHORT_CONNECTION: 0.8,
}


@dataclass
class HiddenCityCandidate:
    origin: str
    real_destination: str
    decoy_destination: str
    price_direct: float
    price_hidden: float
    risks: list[RiskFactor] = field(default_factory=list)

    @property
    def savings(self) -> float:
        return self.price_direct - self.price_hidden

    @property
    def savings_pct(self) -> float:
        if self.price_direct == 0:
            return 0.0
        return self.savings / self.price_direct

    @property
    def score(self) -> float:
        return score_candidate(self.price_direct, self.price_hidden, self.risks)

    @property
    def should_surface(self) -> bool:
        return self.score >= MIN_SCORE_TO_SURFACE and self.savings >= MIN_SAVINGS_PLN


def score_candidate(
    price_direct: float,
    price_hidden: float,
    risks: list[RiskFactor],
) -> float:
    if price_hidden >= price_direct:
        return 0.0

    savings_pct = (price_direct - price_hidden) / price_direct

    multiplier = 1.0
    for risk in risks:
        multiplier *= _RISK_MULTIPLIERS[risk]

    return min(savings_pct * multiplier, 1.0)


def find_candidates(
    origin: str,
    real_destination: str,
    price_direct: float,
    through_routes: list[dict],
    risks: list[RiskFactor],
) -> list[HiddenCityCandidate]:
    """
    through_routes: list of dicts with keys 'destination' and 'price',
    representing routes that pass through real_destination as a hub.
    """
    candidates = []
    for route in through_routes:
        candidate = HiddenCityCandidate(
            origin=origin,
            real_destination=real_destination,
            decoy_destination=route["destination"],
            price_direct=price_direct,
            price_hidden=route["price"],
            risks=risks,
        )
        if candidate.should_surface:
            candidates.append(candidate)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates
