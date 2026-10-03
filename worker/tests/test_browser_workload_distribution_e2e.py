"""
E2E-style workload distribution tests for multi-browser Playwright tasks.

These tests verify that when concurrent browser tasks are scheduled, work is
spread across all available browser slots with near-even distribution
(difference between busiest and least busy slot is <= 1).
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List

import pytest

from worker.core import benchmark as benchmark_core
from worker.scraper import comp_collection
from worker.scraper import price_estimator
from worker.scraper.day_query import DayResult
from worker.scraper.target_extractor import ListingSpec
import worker.main as worker_main


def _assert_near_even(counts: List[int], expected_total: int) -> None:
    assert sum(counts) == expected_total
    assert min(counts) > 0
    assert max(counts) - min(counts) <= 1


class _StubBrowserClient:
    def __init__(self, slot_id: int, counts: List[int], lock: threading.Lock):
        self.slot_id = slot_id
        self.cdp_url = f"http://127.0.0.1:{9222 + slot_id}"
        self.config = {"CDP_URL": self.cdp_url}
        self._counts = counts
        self._lock = lock

    def ensure_browser_ready(self) -> None:
        return

    def close_browser(self) -> None:
        return

    def get_listing_details(self, *args, **kwargs) -> Dict[str, Any]:
        with self._lock:
            self._counts[self.slot_id] += 1
        return {"ok": True, "slot": self.slot_id}


class _RootClient:
    def __init__(self):
        self.config = {"CDP_URL": "http://127.0.0.1:9222"}

    def get_listing_details(self, *args, **kwargs):
        return {}


def test_comp_spec_repair_distributes_evenly_across_browser_pool(monkeypatch):
    counts = [0, 0, 0]
    lock = threading.Lock()
    browser_pool = [_StubBrowserClient(i, counts, lock) for i in range(3)]

    monkeypatch.setenv("DAY_QUERY_MAX_WORKERS", "3")
    monkeypatch.setattr(
        price_estimator,
        "build_warmed_browser_client_pool",
        lambda **kwargs: browser_pool,
    )
    monkeypatch.setattr(
        price_estimator,
        "close_browser_client_pool",
        lambda _pool: None,
    )

    def _fake_extract_target_spec(client, item_url, fail_on_unusable=False):
        with lock:
            counts[client.slot_id] += 1
        spec = ListingSpec(
            url=item_url,
            title="Recovered title",
            location="Vancouver, BC",
            accommodates=4,
            bedrooms=2,
            baths=1.5,
            property_type="entire_home",
        )
        return spec, []

    monkeypatch.setattr(price_estimator, "extract_target_spec", _fake_extract_target_spec)

    comparable_listings = []
    total_candidates = 11
    for i in range(total_candidates):
        comparable_listings.append(
            {
                "url": f"https://www.airbnb.com/rooms/{700000 + i}",
                "title": "",
                "location": "",
                "accommodates": None,
                "bedrooms": None,
                "baths": None,
            }
        )

    transparent_result = {"comparableListings": comparable_listings}
    extraction_warnings: List[str] = []

    price_estimator._repair_incomplete_comparable_specs(
        _RootClient(),
        transparent_result,
        extraction_warnings,
        limit=total_candidates,
    )

    _assert_near_even(counts, total_candidates)


def test_comp_pdp_enrichment_distributes_evenly_across_browser_pool(monkeypatch):
    counts = [0, 0, 0]
    lock = threading.Lock()
    browser_pool = [_StubBrowserClient(i, counts, lock) for i in range(3)]

    monkeypatch.setenv("PDP_DETAIL_MAX_WORKERS", "3")
    monkeypatch.setattr(
        comp_collection,
        "build_warmed_browser_client_pool",
        lambda **kwargs: browser_pool,
    )
    monkeypatch.setattr(
        comp_collection,
        "close_browser_client_pool",
        lambda _pool: None,
    )
    monkeypatch.setattr(
        comp_collection,
        "parse_pdp_response",
        lambda pdp, lid, base: {
            "baths": 1.0,
            "property_type": "entire_home",
            "amenities": ["Wifi"],
        },
    )

    total_candidates = 10
    comps = [
        ListingSpec(url=f"https://www.airbnb.com/rooms/{810000 + i}")
        for i in range(total_candidates)
    ]

    comp_collection._enrich_comps_baths_and_property_type_from_pdp(
        _RootClient(),
        comps,
        checkin="2026-06-01",
        checkout="2026-06-02",
        adults=2,
    )

    _assert_near_even(counts, total_candidates)


def test_run_scrape_day_query_accepts_runner_keyword_args(monkeypatch):
    class _StopAfterKwargProbe(Exception):
        pass

    monkeypatch.setenv("DAY_QUERY_MAX_WORKERS", "3")

    pool_clients = [object(), object(), object()]
    pool_locks = [threading.Lock(), threading.Lock(), threading.Lock()]
    used_clients: List[object] = []

    monkeypatch.setattr(
        price_estimator,
        "extract_target_spec",
        lambda _client, listing_url, fail_on_unusable=True: (
            ListingSpec(
                url=listing_url,
                title="Target listing",
                location="Seattle, WA",
                accommodates=4,
                bedrooms=2,
                baths=1.0,
                property_type="entire_home",
            ),
            [],
        ),
    )
    monkeypatch.setattr(
        price_estimator,
        "_build_browser_pool",
        lambda **kwargs: (pool_clients, pool_locks),
    )
    monkeypatch.setattr(
        price_estimator,
        "close_browser_client_pool",
        lambda _pool: None,
    )

    def _fake_estimate_base_price_for_date(
        day_client,
        _target,
        _base_origin,
        date_i,
        _effective_adults,
        **kwargs,
    ):
        used_clients.append(day_client)
        return DayResult(
            date=date_i.isoformat(),
            median_price=188.0,
            comps_collected=3,
            comps_used=2,
        )

    monkeypatch.setattr(
        price_estimator,
        "estimate_base_price_for_date",
        _fake_estimate_base_price_for_date,
    )

    def _fake_execute_day_queries_concurrently(
        query_func,
        args_list,
        max_workers=2,
        early_stop_threshold=None,
        progress_callback=None,
    ):
        assert isinstance(args_list, list) and args_list
        first = dict(args_list[0])
        assert set(first.keys()) == {"night_idx", "browser_slot"}
        _ = query_func(**first)
        raise _StopAfterKwargProbe()

    monkeypatch.setattr(
        price_estimator,
        "execute_day_queries_concurrently",
        _fake_execute_day_queries_concurrently,
    )

    with pytest.raises(_StopAfterKwargProbe):
        price_estimator.run_scrape(
            listing_url="https://www.airbnb.com/rooms/123456789",
            checkin="2026-06-01",
            checkout="2026-06-04",
            rate_limit_seconds=0.0,
        )

    assert used_clients
    assert used_clients[0] is pool_clients[0]


def test_run_benchmark_day_query_accepts_runner_keyword_args(monkeypatch):
    class _StopAfterKwargProbe(Exception):
        pass

    monkeypatch.setenv("BENCHMARK_DAY_QUERY_MAX_WORKERS", "3")

    pool_clients = [object(), object(), object()]
    pool_locks = [threading.Lock(), threading.Lock(), threading.Lock()]
    used_clients: List[object] = []

    monkeypatch.setattr(
        benchmark_core,
        "probe_benchmark_discounts",
        lambda client, benchmark_url, base_origin, d_start: {},
    )
    monkeypatch.setattr(
        price_estimator,
        "extract_target_spec",
        lambda _client, listing_url, fail_on_unusable=True: (
            ListingSpec(
                url=listing_url,
                title="Benchmark listing",
                location="Seattle, WA",
                accommodates=4,
                bedrooms=2,
                baths=1.0,
                property_type="entire_home",
            ),
            [],
        ),
    )
    monkeypatch.setattr(
        price_estimator,
        "_build_browser_pool",
        lambda **kwargs: (pool_clients, pool_locks),
    )
    monkeypatch.setattr(
        price_estimator,
        "close_browser_client_pool",
        lambda _pool: None,
    )

    def _fake_estimate_benchmark_price_for_date(
        day_client,
        _target,
        _benchmark_url,
        _base_origin,
        date_i,
        _effective_adults,
        **kwargs,
    ):
        used_clients.append(day_client)
        return benchmark_core.BenchmarkDayResult(
            date=date_i.isoformat(),
            median_price=201.0,
            benchmark_price=198.0,
            market_price=205.0,
            benchmark_fetch_status=benchmark_core.FETCH_STATUS_SEARCH_HIT,
            fetch_confidence="high",
            effective_weight=1.0,
            comps_collected=4,
            comps_used=3,
        )

    monkeypatch.setattr(
        benchmark_core,
        "estimate_benchmark_price_for_date",
        _fake_estimate_benchmark_price_for_date,
    )

    def _fake_execute_day_queries_concurrently(
        query_func,
        args_list,
        max_workers=2,
        early_stop_threshold=None,
        progress_callback=None,
    ):
        assert isinstance(args_list, list) and args_list
        assert len(args_list) == 23
        first = dict(args_list[0])
        assert set(first.keys()) == {"night_idx", "browser_slot"}
        _ = query_func(**first)
        raise _StopAfterKwargProbe()

    monkeypatch.setattr(
        price_estimator,
        "execute_day_queries_concurrently",
        _fake_execute_day_queries_concurrently,
    )

    with pytest.raises(_StopAfterKwargProbe):
        price_estimator.run_benchmark_scrape(
            benchmark_url="https://www.airbnb.com/rooms/987654321",
            checkin="2026-06-01",
            checkout="2026-06-24",
            rate_limit_seconds=0.0,
        )

    assert used_clients
    assert used_clients[0] is pool_clients[0]


def test_self_price_capture_day_query_accepts_runner_keyword_args(monkeypatch):
    class _PoolClient:
        def __init__(self, slot_id: int):
            self.slot_id = slot_id
            self.cdp_url = f"http://127.0.0.1:{9222 + slot_id}"

        def ensure_browser_ready(self) -> None:
            return

        def close_browser(self) -> None:
            return

    browser_pool = [_PoolClient(i) for i in range(3)]
    used_slots: List[int] = []
    pool_kwargs: Dict[str, Any] = {}
    used_adults: List[int] = []

    monkeypatch.setattr(worker_main, "DAY_QUERY_MAX_WORKERS", 3)
    monkeypatch.setattr(worker_main, "RATE_LIMIT_SECONDS", 0.0)
    monkeypatch.setattr(
        "worker.scraper.browser_runtime.build_warmed_browser_client_pool",
        lambda **kwargs: (pool_kwargs.update(kwargs) or browser_pool),
    )
    monkeypatch.setattr(
        "worker.scraper.browser_runtime.close_browser_client_pool",
        lambda _pool: None,
    )

    def _fake_capture_target_live_price(
        listing_url,
        checkin,
        checkout,
        cdp_url,
        cdp_connect_timeout_ms,
        client,
        allow_retry_matrix,
        adults=1,
    ):
        used_slots.append(int(client.slot_id))
        used_adults.append(int(adults))
        return {
            "observedListingPrice": 120 + int(client.slot_id),
            "livePriceStatus": "captured",
            "livePriceStatusReason": "",
            "observedListingPriceSource": "mock",
            "observedListingPriceConfidence": "high",
            "observedListingPriceCapturedAt": f"{checkin}T00:00:00Z",
        }

    monkeypatch.setattr(
        "worker.scraper.target_extractor.capture_target_live_price",
        _fake_capture_target_live_price,
    )

    def _fake_execute_day_queries_concurrently(
        query_func,
        args_list,
        max_workers=2,
        early_stop_threshold=None,
        progress_callback=None,
    ):
        assert isinstance(args_list, list) and args_list
        assert max_workers == 3
        rows = [query_func(**dict(item)) for item in args_list]
        return rows, object()

    monkeypatch.setattr(
        "worker.core.concurrent_runner.execute_day_queries_concurrently",
        _fake_execute_day_queries_concurrently,
    )

    result = worker_main._capture_user_listing_prices_for_range(
        report_id="regression-self-price-kwargs",
        listing_url="https://www.airbnb.com/rooms/123456789",
        start_date="2026-06-01",
        end_date="2026-06-07",
        minimum_booking_nights=1,
        adults=8,
    )

    assert result["capturedDays"] == 6
    assert set(used_slots) == {0, 1, 2}
    assert used_slots.count(0) == 2
    assert used_slots.count(1) == 2
    assert used_slots.count(2) == 2
    assert used_adults == [8, 8, 8, 8, 8, 8]
    assert pool_kwargs["base_config"]["ADULTS"] == 8


def test_self_price_capture_does_not_backfill_observed_price_from_later_date(monkeypatch):
    class _PoolClient:
        def __init__(self, slot_id: int):
            self.slot_id = slot_id
            self.cdp_url = f"http://127.0.0.1:{9222 + slot_id}"

        def ensure_browser_ready(self) -> None:
            return

        def close_browser(self) -> None:
            return

    browser_pool = [_PoolClient(0)]
    allow_retry_values: List[bool] = []

    monkeypatch.setattr(worker_main, "DAY_QUERY_MAX_WORKERS", 1)
    monkeypatch.setattr(worker_main, "RATE_LIMIT_SECONDS", 0.0)
    monkeypatch.setattr(
        "worker.scraper.browser_runtime.build_warmed_browser_client_pool",
        lambda **kwargs: browser_pool,
    )
    monkeypatch.setattr(
        "worker.scraper.browser_runtime.close_browser_client_pool",
        lambda _pool: None,
    )

    def _fake_capture_target_live_price(
        listing_url,
        checkin,
        checkout,
        cdp_url,
        cdp_connect_timeout_ms,
        client,
        allow_retry_matrix,
        adults=1,
    ):
        allow_retry_values.append(bool(allow_retry_matrix))
        if checkin == "2026-06-01":
            return {
                "observedListingPrice": None,
                "livePriceStatus": "no_price_found",
                "livePriceStatusReason": "No nightly price found",
                "observedListingPriceSource": None,
                "observedListingPriceConfidence": "failed",
                "observedListingPriceCapturedAt": f"{checkin}T00:00:00Z",
            }
        return {
            "observedListingPrice": 155,
            "livePriceStatus": "captured",
            "livePriceStatusReason": "",
            "observedListingPriceSource": "mock",
            "observedListingPriceConfidence": "high",
            "observedListingPriceCapturedAt": f"{checkin}T00:00:00Z",
        }

    monkeypatch.setattr(
        "worker.scraper.target_extractor.capture_target_live_price",
        _fake_capture_target_live_price,
    )

    def _fake_execute_day_queries_concurrently(
        query_func,
        args_list,
        max_workers=2,
        early_stop_threshold=None,
        progress_callback=None,
    ):
        rows = [query_func(**dict(item)) for item in args_list]
        return rows, object()

    monkeypatch.setattr(
        "worker.core.concurrent_runner.execute_day_queries_concurrently",
        _fake_execute_day_queries_concurrently,
    )

    result = worker_main._capture_user_listing_prices_for_range(
        report_id="regression-self-price-no-cross-day-backfill",
        listing_url="https://www.airbnb.com/rooms/123456789",
        start_date="2026-06-01",
        end_date="2026-06-03",
        minimum_booking_nights=1,
    )

    # 2026-06-01 never prices (1-night, then the 2-night fallback), so the
    # daily-capture retry pass re-attempts it once more; 2026-06-02 prices on
    # its first try and is never retried. allow_retry_matrix stays False
    # throughout — none of these calls opt into cross-window retry.
    assert allow_retry_values == [False, False, False, False, False]
    assert result["capturedDays"] == 1
    assert result["priceByDate"] == {"2026-06-02": 155}
    assert result["observedListingPrice"] is None
    assert result["observedListingPriceDate"] == "2026-06-01"


def test_self_price_capture_retries_missing_dates(monkeypatch):
    class _PoolClient:
        cdp_url = "http://127.0.0.1:9222"

        def ensure_browser_ready(self) -> None:
            return

        def close_browser(self) -> None:
            return

    attempts_by_date: Dict[str, int] = {}
    execute_calls: List[List[Dict[str, Any]]] = []

    monkeypatch.setattr(worker_main, "DAY_QUERY_MAX_WORKERS", 1)
    monkeypatch.setattr(worker_main, "RATE_LIMIT_SECONDS", 0.0)
    monkeypatch.setattr(
        "worker.scraper.browser_runtime.build_warmed_browser_client_pool",
        lambda **_kwargs: [_PoolClient()],
    )
    monkeypatch.setattr(
        "worker.scraper.browser_runtime.close_browser_client_pool",
        lambda _pool: None,
    )

    def _fake_capture_target_live_price(
        listing_url,
        checkin,
        checkout,
        cdp_url,
        cdp_connect_timeout_ms,
        client,
        allow_retry_matrix,
        adults=1,
    ):
        attempts_by_date[checkin] = attempts_by_date.get(checkin, 0) + 1
        if checkin == "2026-06-02" and attempts_by_date[checkin] <= 2:
            return {
                "observedListingPrice": None,
                "livePriceStatus": "no_price_found",
                "livePriceStatusReason": "transient blank widget",
                "observedListingPriceSource": None,
                "observedListingPriceConfidence": "failed",
                "observedListingPriceCapturedAt": f"{checkin}T00:00:00Z",
            }
        return {
            "observedListingPrice": 150 + attempts_by_date[checkin],
            "livePriceStatus": "captured",
            "livePriceStatusReason": "",
            "observedListingPriceSource": "mock",
            "observedListingPriceConfidence": "high",
            "observedListingPriceCapturedAt": f"{checkin}T00:00:00Z",
        }

    monkeypatch.setattr(
        "worker.scraper.target_extractor.capture_target_live_price",
        _fake_capture_target_live_price,
    )

    def _fake_execute_day_queries_concurrently(
        query_func,
        args_list,
        max_workers=2,
        early_stop_threshold=None,
        progress_callback=None,
    ):
        execute_calls.append([dict(item) for item in args_list])
        rows = [query_func(**dict(item)) for item in args_list]
        return rows, object()

    monkeypatch.setattr(
        "worker.core.concurrent_runner.execute_day_queries_concurrently",
        _fake_execute_day_queries_concurrently,
    )

    result = worker_main._capture_user_listing_prices_for_range(
        report_id="regression-self-price-missing-day-retry",
        listing_url="https://www.airbnb.com/rooms/123456789",
        start_date="2026-06-01",
        end_date="2026-06-04",
        minimum_booking_nights=1,
    )

    assert len(execute_calls) == 2
    assert [item["day_index"] for item in execute_calls[1]] == [1]
    assert attempts_by_date == {
        "2026-06-01": 1,
        "2026-06-02": 3,
        "2026-06-03": 1,
    }
    assert result["capturedDays"] == 3
    assert result["priceByDate"]["2026-06-02"] == 153


def test_self_price_capture_tries_two_night_window_after_one_night_miss(monkeypatch):
    class _PoolClient:
        def __init__(self):
            self.cdp_url = "http://127.0.0.1:9222"

        def ensure_browser_ready(self) -> None:
            return

        def close_browser(self) -> None:
            return

    calls: List[Dict[str, Any]] = []
    browser_pool = [_PoolClient()]

    monkeypatch.setattr(worker_main, "DAY_QUERY_MAX_WORKERS", 1)
    monkeypatch.setattr(worker_main, "RATE_LIMIT_SECONDS", 0.0)
    monkeypatch.setattr(
        "worker.scraper.browser_runtime.build_warmed_browser_client_pool",
        lambda **kwargs: browser_pool,
    )
    monkeypatch.setattr(
        "worker.scraper.browser_runtime.close_browser_client_pool",
        lambda _pool: None,
    )

    def _fake_capture_target_live_price(
        listing_url,
        checkin,
        checkout,
        cdp_url,
        cdp_connect_timeout_ms,
        client,
        allow_retry_matrix,
        adults=1,
    ):
        calls.append({"checkin": checkin, "checkout": checkout, "allow_retry_matrix": allow_retry_matrix})
        if checkout == "2026-06-02":
            return {
                "observedListingPrice": None,
                "livePriceStatus": "no_price_found",
                "livePriceStatusReason": "No nightly price found",
                "observedListingPriceSource": None,
                "observedListingPriceConfidence": "failed",
                "observedListingPriceCapturedAt": f"{checkin}T00:00:00Z",
            }
        return {
            "observedListingPrice": 150,
            "livePriceStatus": "captured",
            "livePriceStatusReason": "Nightly price captured for two-night fallback",
            "observedListingPriceSource": "mock",
            "observedListingPriceConfidence": "high",
            "observedListingPriceCapturedAt": f"{checkin}T00:00:00Z",
        }

    monkeypatch.setattr(
        "worker.scraper.target_extractor.capture_target_live_price",
        _fake_capture_target_live_price,
    )

    monkeypatch.setattr(
        "worker.core.concurrent_runner.execute_day_queries_concurrently",
        lambda query_func, args_list, **_kwargs: ([query_func(**dict(args_list[0]))], object()),
    )

    result = worker_main._capture_user_listing_prices_for_range(
        report_id="regression-self-price-two-night-fallback",
        listing_url="https://www.airbnb.com/rooms/123456789",
        start_date="2026-06-01",
        end_date="2026-06-02",
        minimum_booking_nights=2,
    )

    assert [(c["checkin"], c["checkout"]) for c in calls] == [
        ("2026-06-01", "2026-06-02"),
        ("2026-06-01", "2026-06-03"),
    ]
    assert all(c["allow_retry_matrix"] is False for c in calls)
    assert result["observedListingPrice"] == 150
    assert result["priceByDate"] == {"2026-06-01": 150}


def test_self_price_capture_prefers_one_night_before_minimum_stay_fallback(monkeypatch):
    class _PoolClient:
        cdp_url = "http://127.0.0.1:9222"

        def ensure_browser_ready(self) -> None:
            return

        def close_browser(self) -> None:
            return

    calls: List[Dict[str, Any]] = []

    monkeypatch.setattr(worker_main, "DAY_QUERY_MAX_WORKERS", 1)
    monkeypatch.setattr(worker_main, "RATE_LIMIT_SECONDS", 0.0)
    monkeypatch.setattr(
        "worker.scraper.browser_runtime.build_warmed_browser_client_pool",
        lambda **_kwargs: [_PoolClient()],
    )
    monkeypatch.setattr(
        "worker.scraper.browser_runtime.close_browser_client_pool",
        lambda _pool: None,
    )

    def _fake_capture_target_live_price(
        listing_url,
        checkin,
        checkout,
        cdp_url,
        cdp_connect_timeout_ms,
        client,
        allow_retry_matrix,
        adults=1,
    ):
        calls.append({"checkin": checkin, "checkout": checkout})
        return {
            "observedListingPrice": 430,
            "livePriceStatus": "captured",
            "livePriceStatusReason": "",
            "observedListingPriceSource": "mock",
            "observedListingPriceConfidence": "high",
            "observedListingPriceCapturedAt": f"{checkin}T00:00:00Z",
        }

    monkeypatch.setattr(
        "worker.scraper.target_extractor.capture_target_live_price",
        _fake_capture_target_live_price,
    )
    monkeypatch.setattr(
        "worker.core.concurrent_runner.execute_day_queries_concurrently",
        lambda query_func, args_list, **_kwargs: ([query_func(**dict(args_list[0]))], object()),
    )

    result = worker_main._capture_user_listing_prices_for_range(
        report_id="regression-self-price-one-night-first",
        listing_url="https://www.airbnb.com/rooms/123456789",
        start_date="2026-06-17",
        end_date="2026-06-18",
        minimum_booking_nights=3,
    )

    assert calls == [{"checkin": "2026-06-17", "checkout": "2026-06-18"}]
    assert result["observedListingPrice"] == 430
    assert result["priceByDate"] == {"2026-06-17": 430}


def test_get_listing_url_prefers_input_attributes_when_payload_urls_conflict(caplog):
    job = {
        "input_listing_url": "https://www.airbnb.com/rooms/50302420",
        "input_attributes": {
            "listingUrl": "https://www.airbnb.com/rooms/12034936",
        },
    }

    caplog.set_level("WARNING")
    resolved = worker_main._get_listing_url(job)

    assert resolved == "https://www.airbnb.com/rooms/12034936"
    assert "Listing URL mismatch" in caplog.text


def test_get_listing_url_falls_back_to_top_level_when_attributes_missing():
    job = {
        "input_listing_url": "https://www.airbnb.com/rooms/12034936",
        "input_attributes": {},
    }

    resolved = worker_main._get_listing_url(job)

    assert resolved == "https://www.airbnb.com/rooms/12034936"
