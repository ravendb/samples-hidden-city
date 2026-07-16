from datetime import datetime, timedelta, timezone

from src.db.expiration import PRICE_TTL_MINUTES, expires_at


class TestExpiresAt:
    def test_returns_iso_timestamp_around_default_ttl(self):
        before = datetime.now(timezone.utc)
        result = datetime.fromisoformat(expires_at())

        delta = result - before
        assert timedelta(minutes=PRICE_TTL_MINUTES - 1) < delta < timedelta(minutes=PRICE_TTL_MINUTES + 1)

    def test_respects_custom_minutes(self):
        before = datetime.now(timezone.utc)
        result = datetime.fromisoformat(expires_at(minutes=5))

        delta = result - before
        assert timedelta(minutes=4) < delta < timedelta(minutes=6)
