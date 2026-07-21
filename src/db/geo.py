"""
Geographic helpers backing the vector-search "nearby airport" lookup in
src/tools/search_routes.py.

Raw (lat, lng) cosine similarity is the wrong metric for great-circle distance —
longitude wraps at +/-180 and latitude sign flips don't mean "far apart" the way
the raw components suggest. Projecting to a 3D unit vector fixes this: cosine
similarity between two such vectors equals cos(angular separation), which is
exactly proportional to great-circle distance, with no antimeridian/pole
discontinuity.
"""
import math

_EARTH_RADIUS_KM = 6371.0


def to_unit_vector(lat: float, lng: float) -> list[float]:
    lat_r, lng_r = math.radians(lat), math.radians(lng)
    return [
        math.cos(lat_r) * math.cos(lng_r),
        math.cos(lat_r) * math.sin(lng_r),
        math.sin(lat_r),
    ]


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    lat1_r, lng1_r, lat2_r, lng2_r = map(math.radians, (lat1, lng1, lat2, lng2))
    dlat = lat2_r - lat1_r
    dlng = lng2_r - lng1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlng / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))
