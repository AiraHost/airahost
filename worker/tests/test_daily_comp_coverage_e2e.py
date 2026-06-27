from __future__ import annotations

import threading
from types import SimpleNamespace
from datetime import date

from worker.scraper import price_estimator
from worker.scraper.day_query import DayResult
from worker.scraper.target_extractor import ListingSpec


class _FakeClient:
    config = {"CDP_URL": "http://127.0.0.1:9222"}
    cdp_url = "http://127.0.0.1:9222"

    def close_browser(self) -> None:
        return


def _fresh_day_result(date_i: date, *, prefix: str = "8", count: int = 4) -> DayResult:
    top_comps = []
    comp_prices = {}
    date_suffix = date_i.strftime("%d")
    for i in range(count):
        cid = f"{prefix}{date_suffix}{i}"
        top_comps.append({
            "id": cid,
            "title": f"Fresh comp {cid}",
            "propertyType": "entire_home",
            "nightlyPrice": 120 + i,
            "similarity": 0.9 - (i * 0.01),
            "url": f"https://www.airbnb.com/rooms/{cid}",
        })
        comp_prices[cid] = 120 + i
    return DayResult(
        date=date_i.isoformat(),
        median_price=160.0,
        comps_collected=count,
        comps_used=count,
        filter_stage="strict",
        flags=[],
        is_sampled=True,
        is_weekend=False,
        top_comps=top_comps,
        comp_prices=comp_prices,
        selection_mode="strict",
        pricing_confidence="high",
    )


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
    monkeypatch.setattr(price_estimator, "similarity_score", lambda _target, _comp: 0.9)

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
    assert set(target_filters) == {4}


def test_run_scrape_uses_dynamic_day_query_when_fixed_yield_is_under_ten(monkeypatch):
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
    fixed_pool = {
        str(7000 + i): {
            "similarity": 0.88 - (i * 0.001),
            "url": f"https://www.airbnb.com/rooms/{7000 + i}",
            "title": f"Reusable comp {i}",
            "property_type": "entire_home",
            "accommodates": 4,
            "nightly_price": 150 + i,
            "seenCount": 2,
        }
        for i in range(15)
    }

    monkeypatch.setattr(
        price_estimator,
        "extract_target_spec",
        lambda *_args, **_kwargs: (target, []),
    )
    monkeypatch.setattr(
        price_estimator,
        "_build_fixed_comp_pool_by_stride",
        lambda *_args, **_kwargs: (fixed_pool, ["2026-06-01"], len(fixed_pool), len(fixed_pool)),
    )
    monkeypatch.setattr(
        price_estimator,
        "_preflight_listing_exists",
        lambda *_args, **_kwargs: (False, {"checked": False}),
    )
    monkeypatch.setattr(
        price_estimator,
        "_build_browser_pool",
        lambda **_kwargs: (
            [_FakeClient(), _FakeClient()],
            [threading.BoundedSemaphore(4), threading.BoundedSemaphore(4)],
        ),
    )
    monkeypatch.setattr(
        price_estimator,
        "close_browser_client_pool",
        lambda _pool: None,
    )
    calls = []

    def _estimate(_client, _target, _base_origin, date_i, _adults, **kwargs):
        calls.append(kwargs.get("known_comp_pool") is not None)
        if kwargs.get("known_comp_pool") is not None:
            return _fresh_day_result(date_i, prefix="8", count=8)
        return _fresh_day_result(date_i, prefix="9", count=3)

    monkeypatch.setattr(price_estimator, "estimate_base_price_for_date", _estimate)
    monkeypatch.setattr(
        price_estimator,
        "extract_nightly_price_from_listing_page",
        lambda *_args, **_kwargs: (None, "scrape_failed"),
    )
    monkeypatch.setattr(
        price_estimator,
        "_repair_incomplete_comparable_specs",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        price_estimator,
        "_repair_suspicious_comparable_titles",
        lambda *_args, **_kwargs: None,
    )

    def _execute(query_func, args_list, **_kwargs):
        return (
            [query_func(**dict(item)) for item in args_list],
            SimpleNamespace(
                completed_indices=list(range(len(args_list))),
                consecutive_empty_peak=0,
                early_stop_triggered=False,
            ),
        )

    monkeypatch.setattr(price_estimator, "execute_day_queries_concurrently", _execute)

    _daily, transparent = price_estimator.run_scrape(
        listing_url="https://www.airbnb.com/rooms/999999",
        checkin="2026-06-01",
        checkout="2026-06-04",
        adults=4,
        rate_limit_seconds=0.0,
    )

    for date_str in ("2026-06-01", "2026-06-02", "2026-06-03"):
        count_for_date = sum(
            1
            for comp in transparent["comparableListings"]
            if date_str in (comp.get("priceByDate") or {})
        )
        assert count_for_date >= 10

    assert calls.count(True) == 3
    assert calls.count(False) == 3
    assert transparent["queryCriteria"]["dailyCompCoverageTarget"] == 10
    assert transparent["queryCriteria"]["fixedCompBaselineTarget"] == 15
    assert transparent["queryCriteria"]["fixedCompPoolCoverageMode"] == "hybrid_fixed_then_dynamic"
