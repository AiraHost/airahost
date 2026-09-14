from __future__ import annotations

from datetime import date

from worker.scraper import price_estimator
from worker.scraper.target_extractor import ListingSpec


class _FakeClient:
    config = {"CDP_URL": "http://127.0.0.1:9222"}
    cdp_url = "http://127.0.0.1:9222"

    def close_browser(self) -> None:
        return


def test_fixed_pool_expands_geo_when_baseline_pool_is_short(monkeypatch):
    target = ListingSpec(
        url="https://www.airbnb.com/rooms/999999",
        title="Target",
        location="Belmont, CA",
        property_type="entire_home",
        accommodates=4,
        bedrooms=2,
        baths=1.5,
        lat=37.52,
        lng=-122.27,
    )
    radii = []
    target_filters = []

    def _spec(listing_id: int) -> ListingSpec:
        return ListingSpec(
            url=f"https://www.airbnb.com/rooms/{listing_id}",
            title=f"Comp {listing_id}",
            location="Belmont, CA",
            property_type="entire_home",
            accommodates=4,
            bedrooms=2,
            baths=1.5,
            nightly_price=200.0,
            lat=37.52,
            lng=-122.27,
        )

    def _collect_search_comps(*_args, **kwargs):
        radius = float(kwargs["map_radius_km"])
        radii.append(radius)
        target_filters.append(kwargs.get("target_accommodates"))
        count = 13 if radius == 4.0 else 15
        return [_spec(1000 + i) for i in range(count)], 1

    monkeypatch.setattr(price_estimator, "collect_search_comps", _collect_search_comps)
    monkeypatch.setattr(
        price_estimator,
        "filter_similar_candidates",
        lambda _target, comps: (list(comps), {"stage": "test"}),
    )
    monkeypatch.setattr(
        price_estimator,
        "similarity_score",
        lambda _target, _comp, debug=False: (0.9, {}) if debug else 0.9,
    )

    pool = price_estimator._build_fixed_comp_pool(
        _FakeClient(),
        target,
        "https://www.airbnb.com",
        date(2026, 6, 1),
        adults=4,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        max_radius_km=4.0,
        pool_size=15,
        page_count=2,
    )

    assert len(pool) == 15
    assert 4.0 in radii
    assert 5.0 in radii
    # target_accommodates is intentionally not passed to the search here —
    # filtering by exact capacity happens later via similarity scoring; the
    # unfiltered search keeps fixed-pool discovery fast.
    assert set(target_filters) == {None}
