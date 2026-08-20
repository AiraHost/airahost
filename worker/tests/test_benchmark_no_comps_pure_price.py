"""
A day whose market search finds ZERO comps must still be priced at the pure
benchmark price when the benchmark's own page yielded one — matching the
market_median-is-None branch of the blend.

Why this matters: sparse markets (e.g. large-capacity homes) can return no
comps for every sampled day. Before this fix each such day kept
median_price=None despite a high-confidence benchmark price, the whole
benchmark pipeline returned empty, and the worker silently re-ran the full
day-by-day market scrape (run_scrape) — doubling report generation time and
making the progress bar jump backwards after "7/7 days".
"""

from __future__ import annotations

from datetime import date

from worker.core import benchmark as benchmark_core
from worker.scraper.target_extractor import ListingSpec


def test_zero_comp_day_prices_at_pure_benchmark(monkeypatch):
    monkeypatch.setattr(
        benchmark_core, "collect_search_comps", lambda *a, **k: ([], 1)
    )
    monkeypatch.setattr(
        benchmark_core,
        "_extract_benchmark_price_with_min_stay_fallback",
        lambda *a, **k: (250.0, "high", "Saffron Villa"),
    )
    monkeypatch.setattr(benchmark_core.time, "sleep", lambda *_: None)

    target = ListingSpec(
        url="https://www.airbnb.com/rooms/123456789",
        location="Menlo Park, CA",
        lat=37.4529,
        lng=-122.1817,
        accommodates=16,
    )

    result = benchmark_core.estimate_benchmark_price_for_date(
        client=object(),
        target=target,
        benchmark_url="https://www.airbnb.com/rooms/987654321",
        base_origin="https://www.airbnb.com",
        date_i=date(2026, 7, 22),
        adults=16,
    )

    assert result.benchmark_price == 250.0
    # The day must be priced (pure benchmark), not dropped.
    assert result.median_price == 250.0
    # Transparency: still flagged as having no market data.
    assert "missing_data" in result.flags
    # The pinned benchmark stays visible in comparableListings with its name.
    assert result.top_comps and result.top_comps[0]["isPinnedBenchmark"] is True
    assert result.top_comps[0]["title"] == "Saffron Villa"
    assert result.benchmark_title == "Saffron Villa"
