"""
CronJob entrypoint — runs every 6h inside the cluster.
Pulls Travelpayouts bulk prices and bulk-writes to RavenDB.
Existing documents are updated in place; prices > 2h old trigger a re-fetch.

Origins are not a hardcoded country list — users depart from anywhere, so we
scrape wherever real saved profiles (Users.home_airport / departure_airports)
say they fly from. _FALLBACK_ORIGINS only covers the cold-start case where no
profile has a departure airport saved yet.

Hub (connecting airport) data does not come from the bulk endpoint — see
src/scraper/travelpayouts.py. For each origin, routes with transfers > 0 get
their hub guessed offline via src/scraper/hub_inference.py, against airport
coordinates already in RavenDB — no external call, unlike the real-time
Flight Search API this project can't get approved for.
"""
import asyncio
import logging
import os

from src.db.client import doc_to_dict, get_store, load_all_airport_coords, put_document
from src.db.expiration import ensure_expiration_enabled, expires_at
from src.db.models import RouteDocument
from src.hidden_city.enricher import enrich_hidden_city
from src.scraper.hub_inference import infer_hub
from src.scraper.travelpayouts import fetch_cheapest_from

log = logging.getLogger(__name__)

# National-capital airports across Europe, Asia, and the US — widens cold-start
# coverage (see module docstring) well beyond the original 6-airport list, so a
# fresh cluster already has real cached routes for most countries a demo user
# might ask about, without waiting on their profile to be saved first. Where a
# capital city's own airport has negligible/no scheduled service, the nearest
# major hub actually served by Travelpayouts is used instead (e.g. ZRH for
# Bern, RGN for Naypyidaw, TLV for Jerusalem) — noted inline.
_EUROPE_CAPITALS = [
    "WAW",  # Warsaw, Poland
    "LHR",  # London, UK
    "CDG",  # Paris, France
    "BER",  # Berlin, Germany
    "MAD",  # Madrid, Spain
    "FCO",  # Rome, Italy
    "AMS",  # Amsterdam, Netherlands
    "BRU",  # Brussels, Belgium
    "VIE",  # Vienna, Austria
    "ZRH",  # Switzerland — proxy for Bern
    "ARN",  # Stockholm, Sweden
    "OSL",  # Oslo, Norway
    "CPH",  # Copenhagen, Denmark
    "HEL",  # Helsinki, Finland
    "DUB",  # Dublin, Ireland
    "LIS",  # Lisbon, Portugal
    "ATH",  # Athens, Greece
    "PRG",  # Prague, Czechia
    "BUD",  # Budapest, Hungary
    "OTP",  # Bucharest, Romania
    "SOF",  # Sofia, Bulgaria
    "ZAG",  # Zagreb, Croatia
    "BEG",  # Belgrade, Serbia
    "KBP",  # Kyiv, Ukraine
    "SVO",  # Moscow, Russia
    "MSQ",  # Minsk, Belarus
    "VNO",  # Vilnius, Lithuania
    "RIX",  # Riga, Latvia
    "TLL",  # Tallinn, Estonia
    "KEF",  # Reykjavik — Iceland
    "LJU",  # Ljubljana, Slovenia
    "BTS",  # Bratislava, Slovakia
    "SKP",  # Skopje, North Macedonia
    "SJJ",  # Sarajevo, Bosnia and Herzegovina
    "TGD",  # Podgorica, Montenegro
    "TIA",  # Tirana, Albania
    "KIV",  # Chisinau, Moldova
    "LUX",  # Luxembourg City
    "MLA",  # Valletta, Malta
    "LCA",  # Cyprus — proxy for Nicosia (no airport)
]

_ASIA_CAPITALS = [
    "ICN",  # Seoul, South Korea
    "DXB",  # UAE — proxy for Abu Dhabi
    "SIN",  # Singapore
    "NRT",  # Tokyo, Japan
    "PEK",  # Beijing, China
    "DEL",  # New Delhi, India
    "BKK",  # Bangkok, Thailand
    "CGK",  # Jakarta, Indonesia
    "MNL",  # Manila, Philippines
    "HAN",  # Hanoi, Vietnam
    "KUL",  # Kuala Lumpur, Malaysia
    "ISB",  # Islamabad, Pakistan
    "DAC",  # Dhaka, Bangladesh
    "CMB",  # Colombo — Sri Lanka
    "KTM",  # Kathmandu, Nepal
    "RGN",  # Myanmar — proxy for Naypyidaw
    "PNH",  # Phnom Penh, Cambodia
    "VTE",  # Vientiane, Laos
    "ULN",  # Ulaanbaatar, Mongolia
    "NQZ",  # Astana, Kazakhstan
    "TAS",  # Tashkent, Uzbekistan
    "FRU",  # Bishkek, Kyrgyzstan
    "DYU",  # Dushanbe, Tajikistan
    "ASB",  # Ashgabat, Turkmenistan
    "GYD",  # Baku, Azerbaijan
    "EVN",  # Yerevan, Armenia
    "TBS",  # Tbilisi, Georgia
    "IKA",  # Tehran, Iran
    "BGW",  # Baghdad, Iraq
    "DAM",  # Damascus, Syria
    "BEY",  # Beirut, Lebanon
    "AMM",  # Amman, Jordan
    "TLV",  # Israel — proxy for Jerusalem
    "RUH",  # Riyadh, Saudi Arabia
    "DOH",  # Doha, Qatar
    "BAH",  # Manama, Bahrain
    "KWI",  # Kuwait City
    "MCT",  # Muscat, Oman
    "SAH",  # Sanaa, Yemen
    "KBL",  # Kabul, Afghanistan
    "TPE",  # Taipei, Taiwan
    "FNJ",  # Pyongyang, North Korea
    "DIL",  # Dili, Timor-Leste
]

_US_CAPITALS = [
    "JFK",  # New York (major hub, kept from the original list)
    "IAD",  # Washington, D.C. — the actual US capital
]

_FALLBACK_ORIGINS = sorted(set(_EUROPE_CAPITALS + _ASIA_CAPITALS + _US_CAPITALS))


def get_origins(store) -> list[str]:
    """Distinct departure airports across all saved user profiles, ALWAYS unioned with
    _FALLBACK_ORIGINS — not just used as a cold-start-only fallback. The capitals list
    exists to guarantee broad demo coverage regardless of what's been searched before;
    if it only applied "when no profile exists yet", a single saved profile (even a
    stray test/debug one) would silently shrink every future scrape back down to just
    that one airport, undoing the whole point of a wide baseline."""
    origins: set[str] = set(_FALLBACK_ORIGINS)
    with store.open_session() as session:
        users = [doc_to_dict(u) for u in session.query_collection("Users").take(10_000)]

    for user in users:
        home_airport = user.get("home_airport")
        if home_airport:
            origins.add(home_airport.upper())
        for airport in user.get("departure_airports", []):
            origins.add(airport.upper())

    return sorted(origins)


def _resolve_hubs(
    origin: str,
    fetched: list[tuple[RouteDocument, int]],
    airport_coords: dict[str, tuple[float, float]],
) -> list[RouteDocument]:
    """Guesses a connecting hub for routes with transfers > 0. Routes whose
    origin/destination/candidate-hub coordinates aren't in RavenDB keep
    hubs=[] — see src/scraper/hub_inference.py."""
    routes: list[RouteDocument] = []
    for route, transfers in fetched:
        if transfers > 0:
            hubs = infer_hub(origin, route.destination, airport_coords)
            route = route.model_copy(update={"hubs": hubs})
        routes.append(route)
    return routes


async def run() -> None:
    token = os.environ["TRAVELPAYOUTS_TOKEN"]
    store = get_store()
    # The CronJob runs as its own process, independent of the agent's startup event —
    # don't assume the agent pod has already turned expiration on for this database.
    ensure_expiration_enabled(store)
    origins = get_origins(store)
    airport_coords = load_all_airport_coords(store)
    total_written = 0

    for origin in origins:
        print(f"  Travelpayouts → fetching {origin}...", flush=True)
        try:
            fetched = await fetch_cheapest_from(origin, token)
        except Exception:
            # One origin rejected by Travelpayouts (bad request, rate limit, timeout)
            # must not lose every remaining origin in the batch -- with ~85 origins
            # now in _FALLBACK_ORIGINS, a single bad one (e.g. FRU 400s on this
            # endpoint) used to abort the whole run before this try/except existed,
            # silently skipping everything alphabetically after it.
            log.exception("Fetch failed for origin %s — skipping, continuing with the rest", origin)
            print(f"  Travelpayouts → {origin}: failed, skipping", flush=True)
            continue
        log.info("Fetched %d routes from %s", len(fetched), origin)

        routes = _resolve_hubs(origin, fetched, airport_coords)
        enriched = enrich_hidden_city(routes)
        hidden = sum(1 for r in enriched if r.hidden_city_score > 0.5)

        for route in enriched:
            data = route.model_dump(mode="json")
            data["@metadata"] = {"@collection": "Routes", "@expires": expires_at()}
            put_document(store, route.route_id(), data)

        total_written += len(enriched)
        print(f"  Travelpayouts → {origin}: {len(enriched)} routes ({hidden} hidden city)", flush=True)
        log.info("Wrote %d routes for origin %s", len(enriched), origin)

    print(f"  Travelpayouts → done, {total_written} routes total", flush=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
