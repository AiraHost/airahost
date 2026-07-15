from __future__ import annotations

from datetime import date

import pytest

from worker.core import benchmark as benchmark_core
from worker.scraper.target_extractor import ListingSpec


def test_benchmark_search_caps_map_radius_to_five_miles(monkeypatch):
    captured: dict = {}

    def _fake_collect_search_comps(*args, **kwargs):
        captured.update(kwargs)
        return [], 1

    monkeypatch.setattr(benchmark_core, "collect_search_comps", _fake_collect_search_comps)
    monkeypatch.setattr(
        benchmark_core,
        "_extract_benchmark_price_with_min_stay_fallback",
        lambda *args, **kwargs: (None, "failed", None),
    )
    monkeypatch.setattr(benchmark_core.time, "sleep", lambda *_: None)

    target = ListingSpec(
        url="https://www.airbnb.com/rooms/123456789",
        location="Seattle, WA",
        lat=47.6062,
        lng=-122.3321,
        accommodates=6,
    )

    _ = benchmark_core.estimate_benchmark_price_for_date(
        client=object(),
        target=target,
        benchmark_url="https://www.airbnb.com/rooms/987654321",
        base_origin="https://www.airbnb.com",
        date_i=date(2026, 6, 1),
        adults=6,
        max_radius_km=30.0,
    )

    assert captured.get("center_lat") == pytest.approx(target.lat or 0.0)
    assert captured.get("center_lng") == pytest.approx(target.lng or 0.0)
    assert captured.get("map_radius_km") == pytest.approx(5.0 * 1.609344)
