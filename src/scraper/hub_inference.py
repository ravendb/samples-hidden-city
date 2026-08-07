"""
Offline geographic heuristic for guessing the likely connecting hub on an
indirect route, used in place of a live Travelpayouts Flight Search lookup.

That real-time API (see git history of src/scraper/travelpayouts.py) requires
a Travelpayouts partnership approval plus 50,000 MAU on the querying resource —
not something this project qualifies for — and 403s regardless of that, since
`fetch_hubs_for_route` was also sending a forbidden loopback `user_ip`. Rather
than depend on an external product this project can't access, hub inference
runs entirely against airport documents already sitting in RavenDB: no
external call, no egress, consistent with the in-cluster-only design of the
rest of this demo.

Method: a route with transfers > 0 likely connects through one of a small set
of major hub airports. For each candidate hub we know coordinates for, compute
the extra great-circle distance flying via that hub adds over the direct
route. The hub with the smallest detour — provided the detour stays under
_MAX_DETOUR_RATIO of the direct distance — is the best guess. This is a
heuristic, not ground truth: it will occasionally pick a plausible-but-wrong
hub, and the hidden_city_score derived from it should be read as indicative,
not authoritative.
"""
from src.db.geo import haversine_km

# Airports handling enough international connecting traffic to plausibly be
# the layover on an indirect itinerary. Not exhaustive — curated to match the
# routes this demo's scraper and airport fixture data actually touch.
MAJOR_HUBS = {
    "LHR", "CDG", "FRA", "AMS", "IST", "DXB", "DOH", "MAD", "FCO",
    "JFK", "ORD", "ICN", "HKG", "SIN", "BKK", "NRT", "HND",
}

_MAX_DETOUR_RATIO = 0.3  # hub may add at most 30% extra distance vs flying direct


def infer_hub(
    origin: str,
    destination: str,
    airport_coords: dict[str, tuple[float, float]],
) -> list[str]:
    """
    Best-guess connecting hub for an origin→destination route with a known
    transfer, or [] if coordinates are missing or no major hub is plausibly
    on the way.
    """
    origin_coords = airport_coords.get(origin)
    dest_coords = airport_coords.get(destination)
    if origin_coords is None or dest_coords is None:
        return []

    direct_km = haversine_km(*origin_coords, *dest_coords)
    if direct_km == 0:
        return []

    best_hub: str | None = None
    best_detour = float("inf")

    for hub in MAJOR_HUBS:
        if hub in (origin, destination):
            continue
        hub_coords = airport_coords.get(hub)
        if hub_coords is None:
            continue

        detour_km = (
            haversine_km(*origin_coords, *hub_coords)
            + haversine_km(*hub_coords, *dest_coords)
            - direct_km
        )
        if detour_km < best_detour:
            best_detour = detour_km
            best_hub = hub

    if best_hub is not None and best_detour <= direct_km * _MAX_DETOUR_RATIO:
        return [best_hub]
    return []
