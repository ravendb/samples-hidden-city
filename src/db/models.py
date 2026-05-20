from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class Coordinates(BaseModel):
    lat: float
    lng: float


class NearbyAirport(BaseModel):
    iata: str
    distance_km: int
    train: bool


class TypicalPrice(BaseModel):
    min: float
    max: float
    currency: str = "USD"


class RouteDocument(BaseModel):
    origin: str
    destination: str
    hubs: list[str] = Field(default_factory=list)
    typical_price: TypicalPrice
    duration_avg_min: int = 0
    hidden_city_score: float = 0.0
    hidden_city_via: Optional[str] = None
    hidden_city_decoy: Optional[str] = None
    hidden_city_risks: list[str] = Field(default_factory=list)
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def route_id(self) -> str:
        return f"routes/{self.origin}-{self.destination}"

    def is_stale(self, max_age_hours: float = 2.0) -> bool:
        age = datetime.now(timezone.utc) - self.last_updated.replace(tzinfo=timezone.utc)
        return age.total_seconds() > max_age_hours * 3600


class AirportDocument(BaseModel):
    iata: str
    name: str
    city: str
    country: str
    coordinates: Coordinates
    nearby: list[NearbyAirport] = Field(default_factory=list)

    def airport_id(self) -> str:
        return f"airports/{self.iata}"


class ActiveConstraints(BaseModel):
    carry_on_only: bool = False
    max_stops: int = 2
    max_duration_min: Optional[int] = None
    preferred_airlines: list[str] = Field(default_factory=list)


class ConversationTurn(BaseModel):
    role: str  # "user" | "assistant"
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SessionDocument(BaseModel):
    user_id: str
    turns: list[ConversationTurn] = Field(default_factory=list)
    active_constraints: ActiveConstraints = Field(default_factory=ActiveConstraints)
    last_active: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def session_id(self, session_num: int = 1) -> str:
        return f"sessions/{self.user_id}-{session_num}"

    def last_n_turns(self, n: int = 10) -> list[ConversationTurn]:
        return self.turns[-n:]
