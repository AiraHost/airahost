from __future__ import annotations

from datetime import date

from worker.scraper.day_query import (
    _revalidate_top_comp_prices_with_pdp,
    estimate_base_price_for_date,
)
from worker.scraper.target_extractor import ListingSpec


def test_revalidate_top_comp_prices_with_pdp_updates_search_price(monkeypatch):
    comp = ListingSpec(
        url="https://www.airbnb.com/rooms/12034936",
        title="Pismo Beach, Steps to the Beach (STR 20928)",
        location="Pismo Beach, California",
        accommodates=8,
        bedrooms=1,
        baths=1.0,
        property_type="entire_home",
        nightly_price=678.0,
        scrape_nights=1,
        price_kind="nightly_from_search",
    )

    class _FakeClient:
        def get_listing_details(self, listing_id, checkin=None, checkout=None, adults=None):
            return {"ok": True, "listing_id": listing_id, "checkin": checkin, "checkout": checkout, "adults": adults}

    monkeypatch.setattr(
        "worker.scraper.day_query.parse_pdp_response",
        lambda *_args, **_kwargs: {
            "nightly_price": 683.0,
            "total_price": 683.0,
            "currency": "USD",
        },
    )

    stats = _revalidate_top_comp_prices_with_pdp(
        _FakeClient(),
        [(comp, 0.88)],
        date_i=date(2026, 6, 5),
        adults=8,
        top_n=1,
    )

    assert stats["attempted"] == 1
    assert stats["updated"] == 1
    assert comp.nightly_price == 683.0
    assert comp.price_kind == "nightly_from_pdp"
    assert comp.currency == "USD"


def test_estimate_base_price_uses_revalidated_comp_price_for_output(monkeypatch):
    comp = ListingSpec(
        url="https://www.airbnb.com/rooms/12034936",
        title="Pismo Beach, Steps to the Beach (STR 20928)",
        location="Pismo Beach, California",
        accommodates=8,
        bedrooms=1,
        baths=1.0,
        property_type="entire_home",
        nightly_price=678.0,
        scrape_nights=1,
        price_kind="nightly_from_search",
    )
    target = ListingSpec(
        url="https://www.airbnb.com/rooms/50302420",
        title="Target",
        location="Oceano, California",
        accommodates=8,
        bedrooms=1,
        baths=1.0,
        property_type="entire_home",
        nightly_price=451.0,
    )

    class _FakeClient:
        def __init__(self):
            self.detail_calls = []
            self._locked_search_location = "Oceano, California"

        def get_listing_details(self, listing_id, checkin=None, checkout=None, adults=None):
            self.detail_calls.append(
                {
                    "listing_id": str(listing_id),
                    "checkin": checkin,
                    "checkout": checkout,
                    "adults": adults,
                }
            )
            return {"ok": True}

    fake_client = _FakeClient()

    def _fake_collect_search_comps(*_args, **kwargs):
        if kwargs.get("prefer_one_night"):
            return [comp], 1
        return [], 2

    class _SanityRow:
        def __init__(self, c, s):
            self.comp = c
            self.sim_score = s
            self.weight = 1.0

    monkeypatch.setattr("worker.scraper.day_query.collect_search_comps", _fake_collect_search_comps)
    monkeypatch.setattr("worker.scraper.day_query._resolve_query_center", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(
        "worker.scraper.day_query.filter_similar_candidates",
        lambda _target, comps: (comps, {"stage": "strict"}),
    )
    monkeypatch.setattr("worker.scraper.day_query.similarity_score", lambda _target, _comp: 0.87)
    monkeypatch.setattr(
        "worker.scraper.day_query.apply_price_sanity",
        lambda above_floor: ([_SanityRow(c, s) for c, s in above_floor], 0, 0),
    )
    monkeypatch.setattr(
        "worker.scraper.day_query.build_price_sanity_weights",
        lambda _rows: {},
    )
    monkeypatch.setattr(
        "worker.scraper.day_query.recommend_price",
        lambda _target, comps, **_kwargs: (
            float(comps[0].nightly_price) if comps else None,
            {"picked_n": len(comps), "below_floor": 0, "low_comp_confidence": False},
        ),
    )
    monkeypatch.setattr(
        "worker.scraper.day_query.parse_pdp_response",
        lambda *_args, **_kwargs: {
            "nightly_price": 683.0,
            "total_price": 683.0,
            "currency": "USD",
        },
    )
    monkeypatch.setattr("worker.scraper.day_query.DAY_QUERY_PDP_REVALIDATE_TOP_N", 1)

    result = estimate_base_price_for_date(
        fake_client,
        target,
        "https://www.airbnb.com",
        date(2026, 6, 5),
        8,
        max_cards=25,
    )

    assert result.comp_prices["12034936"] == 683.0
    assert result.top_comps[0]["id"] == "12034936"
    assert result.top_comps[0]["nightlyPrice"] == 683.0
    assert result.median_price == 683.0
    assert len(fake_client.detail_calls) == 1
    assert fake_client.detail_calls[0]["checkin"] == "2026-06-05"
    assert fake_client.detail_calls[0]["checkout"] == "2026-06-06"
    assert fake_client.detail_calls[0]["adults"] == 8
