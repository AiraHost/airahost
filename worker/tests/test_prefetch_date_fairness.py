"""
Round-robin fairness for the multi-date PDP price prefetch.

The prefetch submits (date, comp) work items to a bounded thread pool in
submission order. If items are grouped by date, progressive Airbnb
throttling/challenges starve the LAST dates, which surfaces as comparable-listing
counts steadily declining toward later dates. Interleaving the work round-robin
across dates keeps coverage balanced under that degradation.
"""

from __future__ import annotations

from worker.scraper.price_estimator import _interleave_by_date_round_robin


def test_interleave_cycles_one_item_per_date_first() -> None:
    per_date = [
        ["d1c1", "d1c2", "d1c3"],
        ["d2c1", "d2c2", "d2c3"],
        ["d3c1", "d3c2", "d3c3"],
    ]
    out = _interleave_by_date_round_robin(per_date)
    # First wave takes one comp from each date before any date's second comp.
    assert out[:3] == ["d1c1", "d2c1", "d3c1"]
    assert out[3:6] == ["d1c2", "d2c2", "d3c2"]
    assert len(out) == 9


def test_interleave_handles_unequal_lengths() -> None:
    per_date = [["a1", "a2", "a3"], ["b1"], ["c1", "c2"]]
    out = _interleave_by_date_round_robin(per_date)
    # No None padding leaks through; every item is preserved exactly once.
    assert out == ["a1", "b1", "c1", "a2", "c2", "a3"]


def test_early_cutoff_spreads_evenly_across_dates() -> None:
    # Simulate a hard budget (rate-limit cutoff) of the first N processed items.
    # With round-robin order, that budget is shared evenly across all dates
    # instead of being consumed entirely by the earliest dates.
    n_dates = 4
    comps_per_date = 10
    per_date = [
        [f"date{d}_comp{c}" for c in range(comps_per_date)]
        for d in range(n_dates)
    ]
    order = _interleave_by_date_round_robin(per_date)

    budget = 20  # only the first 20 items "succeed"
    served = order[:budget]
    per_date_served = [
        sum(1 for item in served if item.startswith(f"date{d}_"))
        for d in range(n_dates)
    ]
    # Every date gets an equal share (20 / 4 = 5); none is starved to zero.
    assert per_date_served == [5, 5, 5, 5]
