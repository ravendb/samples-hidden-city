import math

import pytest

from src.db.geo import haversine_km, to_unit_vector


class TestToUnitVector:
    def test_returns_unit_length_vector(self):
        v = to_unit_vector(52.1657, 20.9671)
        length = math.sqrt(sum(c * c for c in v))
        assert length == pytest.approx(1.0)

    def test_same_point_has_cosine_similarity_one(self):
        v1 = to_unit_vector(40.0799, 116.6031)
        v2 = to_unit_vector(40.0799, 116.6031)
        dot = sum(a * b for a, b in zip(v1, v2))
        assert dot == pytest.approx(1.0)


class TestHaversineKm:
    def test_same_point_is_zero(self):
        assert haversine_km(52.1657, 20.9671, 52.1657, 20.9671) == pytest.approx(0.0, abs=1e-6)

    def test_known_distance_waw_krk(self):
        # Warsaw <-> Krakow is ~250-260km real-world straight-line distance.
        distance = haversine_km(52.1657, 20.9671, 50.0777, 19.7848)
        assert 200 < distance < 300
