from src.scraper.hub_inference import infer_hub

# Rough real-world coordinates, reused across tests.
_COORDS = {
    "WAW": (52.1657, 20.9671),
    "JFK": (40.6413, -73.7781),
    "LHR": (51.4700, -0.4543),
    "SIN": (1.3644, 103.9915),
    "SYD": (-33.9399, 151.1753),
}


class TestInferHub:
    def test_picks_plausible_hub_on_the_way(self):
        # WAW -> JFK plausibly connects via LHR (small detour); other major
        # hubs aren't even in the coords dict here, so LHR should win.
        hubs = infer_hub("WAW", "JFK", _COORDS)
        assert hubs == ["LHR"]

    def test_no_hub_when_destination_coords_missing(self):
        assert infer_hub("WAW", "ZZZ", _COORDS) == []

    def test_no_hub_when_origin_coords_missing(self):
        assert infer_hub("ZZZ", "JFK", _COORDS) == []

    def test_no_hub_when_detour_too_large(self):
        # SIN -> SYD: LHR is a huge detour in the wrong direction entirely.
        coords = {"SIN": _COORDS["SIN"], "SYD": _COORDS["SYD"], "LHR": _COORDS["LHR"]}
        assert infer_hub("SIN", "SYD", coords) == []

    def test_hub_never_equals_origin_or_destination(self):
        coords = {**_COORDS, "LHR": _COORDS["LHR"]}
        hubs = infer_hub("WAW", "LHR", coords)
        assert "LHR" not in hubs

    def test_same_origin_and_destination_returns_empty(self):
        assert infer_hub("WAW", "WAW", _COORDS) == []
