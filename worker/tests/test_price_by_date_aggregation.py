from worker.scraper.price_estimator import _build_daily_transparent_result
from worker.scraper.target_extractor import ListingSpec


def test_price_by_date_backfills_early_day_from_full_comp_prices():
    target = ListingSpec(
        url="https://www.airbnb.com/rooms/999",
        title="Target listing",
        location="Belmont, CA",
        property_type="entire_home",
        accommodates=4,
        bedrooms=2,
        baths=1.5,
        nightly_price=200,
    )

    all_day_results = [
        {
            "date": "2026-05-01",
            "median_price": 180,
            "comps_collected": 2,
            "comps_used": 1,
            "below_similarity_floor": 0,
            "price_outliers_excluded": 0,
            "price_outliers_downweighted": 0,
            "geo_excluded": 0,
            "price_band_excluded": 0,
            "filter_stage": "strict",
            "flags": [],
            "is_sampled": True,
            "is_weekend": False,
            "price_distribution": {},
            "top_comps": [
                {
                    "id": "111",
                    "title": "Comp A",
                    "propertyType": "entire_home",
                    "nightlyPrice": 120,
                    "similarity": 0.91,
                    "url": "https://www.airbnb.com/rooms/111",
                }
            ],
            "comp_prices": {
                "111": 120,
                "222": 140,
            },
            "error": None,
            "selection_mode": "strict",
            "pricing_confidence": "high",
        },
        {
            "date": "2026-05-02",
            "median_price": 185,
            "comps_collected": 2,
            "comps_used": 1,
            "below_similarity_floor": 0,
            "price_outliers_excluded": 0,
            "price_outliers_downweighted": 0,
            "geo_excluded": 0,
            "price_band_excluded": 0,
            "filter_stage": "strict",
            "flags": [],
            "is_sampled": True,
            "is_weekend": True,
            "price_distribution": {},
            "top_comps": [
                {
                    "id": "222",
                    "title": "Comp B",
                    "propertyType": "entire_home",
                    "nightlyPrice": 150,
                    "similarity": 0.9,
                    "url": "https://www.airbnb.com/rooms/222",
                }
            ],
            "comp_prices": {
                "222": 150,
            },
            "error": None,
            "selection_mode": "strict",
            "pricing_confidence": "high",
        },
    ]

    transparent = _build_daily_transparent_result(
        target=target,
        query_criteria={
            "locationBasis": "Belmont, CA",
            "searchAdults": 4,
            "checkin": "2026-05-01",
            "checkout": "2026-05-03",
            "totalNights": 2,
            "sampledNights": 2,
            "queryMode": "day_by_day",
            "propertyTypeFilter": "entire_home",
        },
        all_day_results=all_day_results,
        timings_ms={"total_ms": 10},
        source="scrape",
        extraction_warnings=[],
    )

    comps = transparent["comparableListings"]
    comp_b = next(c for c in comps if c["id"] == "222")
    assert comp_b["priceByDate"]["2026-05-01"] == 140
    assert comp_b["priceByDate"]["2026-05-02"] == 150


def test_chunk_filled_day_comps_appear_in_price_by_date():
    """
    Chunk-filled days are is_sampled=False but carry the chunk anchor's price and
    comps. Their date must still land in each comp's priceByDate, otherwise the
    later days of every 5-day span show no comparable listings.
    """
    target = ListingSpec(
        url="https://www.airbnb.com/rooms/999",
        title="Target listing",
        location="Belmont, CA",
        property_type="entire_home",
        accommodates=4,
        bedrooms=2,
        baths=1.5,
        nightly_price=200,
    )

    comp = {
        "id": "111",
        "title": "Comp A",
        "propertyType": "entire_home",
        "nightlyPrice": 120,
        "similarity": 0.91,
        "url": "https://www.airbnb.com/rooms/111",
    }
    anchor_day = {
        "date": "2026-05-01",
        "median_price": 180,
        "comps_collected": 1,
        "comps_used": 1,
        "below_similarity_floor": 0,
        "price_outliers_excluded": 0,
        "price_outliers_downweighted": 0,
        "geo_excluded": 0,
        "price_band_excluded": 0,
        "filter_stage": "strict",
        "flags": [],
        "is_sampled": True,
        "is_weekend": False,
        "price_distribution": {},
        "top_comps": [dict(comp)],
        "comp_prices": {"111": 120},
        "error": None,
        "selection_mode": "strict",
        "pricing_confidence": "high",
    }
    # Non-anchor chunk-filled day: inherits the anchor's price + comps.
    filled_day = {
        **anchor_day,
        "date": "2026-05-02",
        "is_sampled": False,
        "filter_stage": "chunk_filled",
        "flags": ["interpolated"],
        "top_comps": [dict(comp)],
        "comp_prices": {"111": 120},
    }

    transparent = _build_daily_transparent_result(
        target=target,
        query_criteria={
            "locationBasis": "Belmont, CA",
            "searchAdults": 4,
            "checkin": "2026-05-01",
            "checkout": "2026-05-03",
            "totalNights": 2,
            "sampledNights": 1,
            "queryMode": "day_by_day",
            "propertyTypeFilter": "entire_home",
        },
        all_day_results=[anchor_day, filled_day],
        timings_ms={"total_ms": 10},
        source="scrape",
        extraction_warnings=[],
    )

    comp_a = next(c for c in transparent["comparableListings"] if c["id"] == "111")
    assert comp_a["priceByDate"]["2026-05-01"] == 120
    assert comp_a["priceByDate"]["2026-05-02"] == 120  # filled day must be covered


def test_comps_summary_uses_unique_counts_not_day_sums():
    target = ListingSpec(
        url="https://www.airbnb.com/rooms/999",
        title="Target listing",
        location="Belmont, CA",
        property_type="entire_home",
        accommodates=4,
        bedrooms=2,
        baths=1.5,
        nightly_price=200,
    )

    # Same two comps appear across both days. Day-level sums are 4, but unique count is 2.
    all_day_results = [
        {
            "date": "2026-05-01",
            "median_price": 180,
            "comps_collected": 2,
            "comps_used": 2,
            "below_similarity_floor": 0,
            "price_outliers_excluded": 0,
            "price_outliers_downweighted": 0,
            "geo_excluded": 0,
            "price_band_excluded": 0,
            "filter_stage": "strict",
            "flags": [],
            "is_sampled": True,
            "is_weekend": False,
            "price_distribution": {},
            "top_comps": [
                {"id": "111", "title": "Comp A", "propertyType": "entire_home", "nightlyPrice": 120, "similarity": 0.91, "url": "https://www.airbnb.com/rooms/111"},
                {"id": "222", "title": "Comp B", "propertyType": "entire_home", "nightlyPrice": 140, "similarity": 0.90, "url": "https://www.airbnb.com/rooms/222"},
            ],
            "comp_prices": {"111": 120, "222": 140},
            "error": None,
            "selection_mode": "strict",
            "pricing_confidence": "high",
        },
        {
            "date": "2026-05-02",
            "median_price": 185,
            "comps_collected": 2,
            "comps_used": 2,
            "below_similarity_floor": 0,
            "price_outliers_excluded": 0,
            "price_outliers_downweighted": 0,
            "geo_excluded": 0,
            "price_band_excluded": 0,
            "filter_stage": "strict",
            "flags": [],
            "is_sampled": True,
            "is_weekend": True,
            "price_distribution": {},
            "top_comps": [
                {"id": "111", "title": "Comp A", "propertyType": "entire_home", "nightlyPrice": 125, "similarity": 0.91, "url": "https://www.airbnb.com/rooms/111"},
                {"id": "222", "title": "Comp B", "propertyType": "entire_home", "nightlyPrice": 150, "similarity": 0.90, "url": "https://www.airbnb.com/rooms/222"},
            ],
            "comp_prices": {"111": 125, "222": 150},
            "error": None,
            "selection_mode": "strict",
            "pricing_confidence": "high",
        },
    ]

    transparent = _build_daily_transparent_result(
        target=target,
        query_criteria={
            "locationBasis": "Belmont, CA",
            "searchAdults": 4,
            "checkin": "2026-05-01",
            "checkout": "2026-05-03",
            "totalNights": 2,
            "sampledNights": 2,
            "queryMode": "day_by_day",
            "propertyTypeFilter": "entire_home",
        },
        all_day_results=all_day_results,
        timings_ms={"total_ms": 10},
        source="scrape",
        extraction_warnings=[],
    )

    summary = transparent["compsSummary"]
    assert summary["collected"] == 2
    assert summary["afterFiltering"] == 2
    assert summary["usedForPricing"] == 2
    # Keep day-summed values available for debugging.
    assert summary["dailyTotals"]["collected"] == 4
    assert summary["dailyTotals"]["usedForPricing"] == 4


def test_price_by_date_skips_days_without_valid_sampled_median():
    target = ListingSpec(
        url="https://www.airbnb.com/rooms/999",
        title="Target listing",
        location="Belmont, CA",
        property_type="entire_home",
        accommodates=4,
        bedrooms=2,
        baths=1.5,
        nightly_price=200,
    )

    all_day_results = [
        {
            "date": "2026-05-10",
            "median_price": None,
            "comps_collected": 20,
            "comps_used": 0,
            "below_similarity_floor": 0,
            "price_outliers_excluded": 0,
            "price_outliers_downweighted": 0,
            "geo_excluded": 0,
            "price_band_excluded": 0,
            "filter_stage": "error",
            "flags": ["missing_data"],
            "is_sampled": True,
            "is_weekend": True,
            "price_distribution": {},
            "top_comps": [
                {
                    "id": "1258869471360489399",
                    "title": "Comp X",
                    "propertyType": "entire_home",
                    "nightlyPrice": 413.5,
                    "similarity": 0.92,
                    "url": "https://www.airbnb.com/rooms/1258869471360489399",
                }
            ],
            "comp_prices": {
                "1258869471360489399": 413.5,
            },
            "error": "No priced comps",
            "selection_mode": "strict",
            "pricing_confidence": "low",
        },
        {
            "date": "2026-05-11",
            "median_price": 410.0,
            "comps_collected": 20,
            "comps_used": 10,
            "below_similarity_floor": 0,
            "price_outliers_excluded": 0,
            "price_outliers_downweighted": 0,
            "geo_excluded": 0,
            "price_band_excluded": 0,
            "filter_stage": "strict",
            "flags": [],
            "is_sampled": True,
            "is_weekend": True,
            "price_distribution": {},
            "top_comps": [
                {
                    "id": "1258869471360489399",
                    "title": "Comp X",
                    "propertyType": "entire_home",
                    "nightlyPrice": 413.5,
                    "similarity": 0.92,
                    "url": "https://www.airbnb.com/rooms/1258869471360489399",
                }
            ],
            "comp_prices": {
                "1258869471360489399": 413.5,
            },
            "error": None,
            "selection_mode": "strict",
            "pricing_confidence": "high",
        },
    ]

    transparent = _build_daily_transparent_result(
        target=target,
        query_criteria={
            "locationBasis": "Belmont, CA",
            "searchAdults": 4,
            "checkin": "2026-05-10",
            "checkout": "2026-05-12",
            "totalNights": 2,
            "sampledNights": 2,
            "queryMode": "day_by_day",
            "propertyTypeFilter": "entire_home",
        },
        all_day_results=all_day_results,
        timings_ms={"total_ms": 10},
        source="scrape",
        extraction_warnings=[],
    )

    comps = transparent["comparableListings"]
    comp_x = next(c for c in comps if c["id"] == "1258869471360489399")
    assert "2026-05-10" not in (comp_x.get("priceByDate") or {})
    assert comp_x["priceByDate"]["2026-05-11"] == 413.5


def test_price_by_date_details_keeps_day_level_url_and_query_nights():
    target = ListingSpec(
        url="https://www.airbnb.com/rooms/999",
        title="Target listing",
        location="Belmont, CA",
        property_type="entire_home",
        accommodates=4,
        bedrooms=2,
        baths=1.5,
        nightly_price=200,
    )

    all_day_results = [
        {
            "date": "2026-05-10",
            "median_price": 180,
            "comps_collected": 2,
            "comps_used": 1,
            "below_similarity_floor": 0,
            "price_outliers_excluded": 0,
            "price_outliers_downweighted": 0,
            "geo_excluded": 0,
            "price_band_excluded": 0,
            "filter_stage": "strict",
            "flags": [],
            "is_sampled": True,
            "is_weekend": True,
            "price_distribution": {},
            "top_comps": [
                {
                    "id": "1258869471360489399",
                    "title": "Comp X",
                    "propertyType": "entire_home",
                    "nightlyPrice": 413.5,
                    "similarity": 0.92,
                    "url": "https://www.airbnb.ca/rooms/1258869471360489399?check_in=2026-05-10&check_out=2026-05-12",
                    "queryNights": 2,
                    "queryTotalPrice": 827.0,
                }
            ],
            "comp_prices": {"1258869471360489399": 413.5},
            "error": None,
            "selection_mode": "strict",
            "pricing_confidence": "high",
        }
    ]

    transparent = _build_daily_transparent_result(
        target=target,
        query_criteria={
            "locationBasis": "Belmont, CA",
            "searchAdults": 4,
            "checkin": "2026-05-10",
            "checkout": "2026-05-11",
            "totalNights": 1,
            "sampledNights": 1,
            "queryMode": "day_by_day",
            "propertyTypeFilter": "entire_home",
        },
        all_day_results=all_day_results,
        timings_ms={"total_ms": 10},
        source="scrape",
        extraction_warnings=[],
    )

    comp = next(c for c in transparent["comparableListings"] if c["id"] == "1258869471360489399")
    assert comp["url"] == "https://www.airbnb.ca/rooms/1258869471360489399"
    detail = comp["priceByDateDetails"]["2026-05-10"]
    assert detail["price"] == 413.5
    assert detail["queryNights"] == 2
    assert detail["queryTotalPrice"] == 827.0
    assert detail["url"].startswith("https://www.airbnb.ca/rooms/1258869471360489399?")


def test_fixed_pool_counts_used_for_pricing_from_price_by_date():
    target = ListingSpec(
        url="https://www.airbnb.com/rooms/999",
        title="Target listing",
        location="Belmont, CA",
        property_type="entire_home",
        accommodates=4,
        bedrooms=2,
        baths=1.5,
        nightly_price=200,
    )

    all_day_results = [
        {
            "date": "2026-05-01",
            "median_price": 180,
            "comps_collected": 2,
            "comps_used": 2,
            "below_similarity_floor": 0,
            "price_outliers_excluded": 0,
            "price_outliers_downweighted": 0,
            "geo_excluded": 0,
            "price_band_excluded": 0,
            "filter_stage": "strict",
            "flags": [],
            "is_sampled": True,
            "is_weekend": False,
            "price_distribution": {},
            "top_comps": [],
            "comp_prices": {"111": 120, "222": 140},
            "error": None,
            "selection_mode": "strict",
            "pricing_confidence": "high",
        },
        {
            "date": "2026-05-02",
            "median_price": 185,
            "comps_collected": 2,
            "comps_used": 2,
            "below_similarity_floor": 0,
            "price_outliers_excluded": 0,
            "price_outliers_downweighted": 0,
            "geo_excluded": 0,
            "price_band_excluded": 0,
            "filter_stage": "strict",
            "flags": [],
            "is_sampled": True,
            "is_weekend": True,
            "price_distribution": {},
            "top_comps": [],
            "comp_prices": {"111": 125, "222": 150},
            "error": None,
            "selection_mode": "strict",
            "pricing_confidence": "high",
        },
    ]

    fixed_comp_pool = {
        "111": {"similarity": 0.91, "url": "https://www.airbnb.com/rooms/111", "title": "Comp A"},
        "222": {"similarity": 0.90, "url": "https://www.airbnb.com/rooms/222", "title": "Comp B"},
    }

    transparent = _build_daily_transparent_result(
        target=target,
        query_criteria={
            "locationBasis": "Belmont, CA",
            "searchAdults": 4,
            "checkin": "2026-05-01",
            "checkout": "2026-05-03",
            "totalNights": 2,
            "sampledNights": 2,
            "queryMode": "day_by_day",
            "propertyTypeFilter": "entire_home",
        },
        all_day_results=all_day_results,
        timings_ms={"total_ms": 10},
        source="scrape",
        extraction_warnings=[],
        fixed_comp_pool=fixed_comp_pool,
    )

    summary = transparent["compsSummary"]
    assert summary["usedForPricing"] == 2


def test_fixed_pool_summary_uses_setup_funnel_counts():
    target = ListingSpec(
        url="https://www.airbnb.com/rooms/999",
        title="Target listing",
        location="Belmont, CA",
        property_type="entire_home",
        accommodates=4,
        bedrooms=2,
        baths=1.5,
        nightly_price=200,
    )

    all_day_results = [
        {
            "date": "2026-05-01",
            "median_price": 180,
            "comps_collected": 2,
            "comps_used": 2,
            "below_similarity_floor": 0,
            "price_outliers_excluded": 0,
            "price_outliers_downweighted": 0,
            "geo_excluded": 0,
            "price_band_excluded": 0,
            "filter_stage": "strict",
            "flags": [],
            "is_sampled": True,
            "is_weekend": False,
            "price_distribution": {},
            "top_comps": [],
            "comp_prices": {"111": 120, "222": 140},
            "error": None,
            "selection_mode": "strict",
            "pricing_confidence": "high",
        },
    ]

    fixed_comp_pool = {
        "111": {"similarity": 0.91, "url": "https://www.airbnb.com/rooms/111", "title": "Comp A"},
        "222": {"similarity": 0.90, "url": "https://www.airbnb.com/rooms/222", "title": "Comp B"},
    }

    transparent = _build_daily_transparent_result(
        target=target,
        query_criteria={
            "locationBasis": "Belmont, CA",
            "searchAdults": 4,
            "checkin": "2026-05-01",
            "checkout": "2026-05-02",
            "totalNights": 1,
            "sampledNights": 1,
            "queryMode": "day_by_day",
            "propertyTypeFilter": "entire_home",
            "fixedCompPoolCollectedTotal": 45,
            "fixedCompPoolFilteredTotal": 25,
            "fixedCompPoolSize": 20,
        },
        all_day_results=all_day_results,
        timings_ms={"total_ms": 10},
        source="scrape",
        extraction_warnings=[],
        fixed_comp_pool=fixed_comp_pool,
    )

    summary = transparent["compsSummary"]
    assert summary["collected"] == 45


def test_fixed_pool_prices_are_not_reused_without_date_specific_evidence():
    target = ListingSpec(
        url="https://www.airbnb.com/rooms/999",
        title="Target listing",
        location="Belmont, CA",
        property_type="entire_home",
        accommodates=4,
        bedrooms=2,
        baths=1.5,
        nightly_price=200,
    )

    def _day(date: str, base_id: int) -> dict:
        top_comps = []
        comp_prices = {}
        for i in range(4):
            cid = str(base_id + i)
            top_comps.append({
                "id": cid,
                "title": f"Fresh comp {cid}",
                "propertyType": "entire_home",
                "nightlyPrice": 120 + i,
                "similarity": 0.9 - (i * 0.01),
                "url": f"https://www.airbnb.com/rooms/{cid}",
            })
            comp_prices[cid] = 120 + i
        return {
            "date": date,
            "median_price": 180,
            "comps_collected": 4,
            "comps_used": 4,
            "below_similarity_floor": 0,
            "price_outliers_excluded": 0,
            "price_outliers_downweighted": 0,
            "geo_excluded": 0,
            "price_band_excluded": 0,
            "filter_stage": "strict",
            "flags": [],
            "is_sampled": True,
            "is_weekend": False,
            "price_distribution": {},
            "top_comps": top_comps,
            "comp_prices": comp_prices,
            "error": None,
            "selection_mode": "strict",
            "pricing_confidence": "high",
        }

    fixed_comp_pool = {
        str(7000 + i): {
            "similarity": 0.88 - (i * 0.001),
            "url": f"https://www.airbnb.com/rooms/{7000 + i}",
            "title": f"Reusable comp {i}",
            "property_type": "entire_home",
            "nightly_price": 150 + i,
            "seenCount": 2,
        }
        for i in range(20)
    }

    transparent = _build_daily_transparent_result(
        target=target,
        query_criteria={
            "locationBasis": "Belmont, CA",
            "searchAdults": 4,
            "checkin": "2026-05-01",
            "checkout": "2026-05-03",
            "totalNights": 2,
            "sampledNights": 2,
            "queryMode": "day_by_day",
            "propertyTypeFilter": "entire_home",
        },
        all_day_results=[
            _day("2026-05-01", 1000),
            _day("2026-05-02", 2000),
        ],
        timings_ms={"total_ms": 10},
        source="scrape",
        extraction_warnings=[],
        fixed_comp_pool=fixed_comp_pool,
    )

    for date in ("2026-05-01", "2026-05-02"):
        count_for_date = sum(
            1
            for comp in transparent["comparableListings"]
            if date in (comp.get("priceByDate") or {})
        )
        assert count_for_date == 4

    assert all(
        comp["id"] != "7000"
        for comp in transparent["comparableListings"]
    )
