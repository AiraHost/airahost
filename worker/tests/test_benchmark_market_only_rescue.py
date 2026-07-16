"""
A benchmark whose calendar is blocked/closed has no bookable price on ANY
sampled day. The market comps were already collected by those same day
queries, so the pipeline must price the days from market medians instead of
returning empty — an empty result discards all of that work and re-runs the
full standard pipeline, roughly doubling report generation time (the 83s
regression seen with rooms/720212490867839664).

When at least one day has a real benchmark anchor, rescue must NOT run:
interpolating failed days from anchored days keeps the benchmark dominant.
"""

from __future__ import annotations

from worker.core.benchmark import BenchmarkDayResult, apply_market_only_rescue


def _day(date: str, benchmark_price=None, market_price=None, median_price=None):
    return BenchmarkDayResult(
        date=date,
        benchmark_price=benchmark_price,
        market_price=market_price,
        median_price=median_price,
    )


def test_all_days_unbookable_are_priced_from_market_medians():
    days = [
        _day("2026-07-23", market_price=344.0),
        _day("2026-07-24", market_price=436.5),
        _day("2026-07-25", market_price=None),  # no market data either
    ]
    rescued = apply_market_only_rescue(days)
    assert rescued == 2
    assert days[0].median_price == 344.0
    assert days[1].median_price == 436.5
    assert days[2].median_price is None
    assert "market_only_pricing" in days[0].flags
    assert "market_only_pricing" in days[1].flags
    assert "market_only_pricing" not in days[2].flags


def test_rescue_does_not_run_when_any_benchmark_anchor_exists():
    days = [
        _day("2026-07-23", benchmark_price=450.0, market_price=344.0, median_price=418.0),
        _day("2026-07-24", market_price=436.5),  # failed day: interpolation fills it
    ]
    rescued = apply_market_only_rescue(days)
    assert rescued == 0
    assert days[1].median_price is None
    assert "market_only_pricing" not in days[1].flags


def test_rescue_handles_empty_input():
    assert apply_market_only_rescue([]) == 0
