"""
Sparse markets: a day whose market search returns zero comps must double the
map radius, retry itself once at the doubled radius, and every later day
query in the same job must start with the doubled radius. Without this, thin
markets produce comp-less days at the capped 5-mile radius on every single
day, and the report degrades to pure-benchmark (or empty) pricing when
nearby-but-not-that-nearby comps exist.
"""

from __future__ import annotations

from datetime import date

import pytest

from worker.core import benchmark as benchmark_core
from worker.core.benchmark import MAP_RADIUS_CAP_KM, RadiusEscalation
from worker.scraper.target_extractor import ListingSpec


def _target() -> ListingSpec:
    return ListingSpec(
        url="https://www.airbnb.com/rooms/123456789",
        location="Seattle, WA",
        lat=47.6062,
        lng=-122.3321,
        accommodates=4,
    )


def _run_day(day: date, escalation: RadiusEscalation) -> None:
    benchmark_core.estimate_benchmark_price_for_date(
        client=object(),
        target=_target(),
        benchmark_url="https://www.airbnb.com/rooms/987654321",
        base_origin="https://www.airbnb.com",
        date_i=day,
        adults=4,
        radius_escalation=escalation,
    )


def test_zero_comp_day_doubles_radius_and_later_days_start_doubled(monkeypatch):
    radii_used: list = []

    def _fake_collect_search_comps(*args, **kwargs):
        radii_used.append(kwargs.get("map_radius_km"))
        return [], 1  # always zero comps (sparse market)

    monkeypatch.setattr(benchmark_core, "collect_search_comps", _fake_collect_search_comps)
    monkeypatch.setattr(
        benchmark_core,
        "_extract_benchmark_price_with_min_stay_fallback",
        lambda *a, **k: (None, "failed", None),
    )
    monkeypatch.setattr(benchmark_core.time, "sleep", lambda *_: None)

    escalation = RadiusEscalation(factor=2.0)

    # Day 1: base radius → zero comps → escalate → one retry at doubled radius.
    _run_day(date(2026, 8, 1), escalation)
    assert radii_used[0] == pytest.approx(MAP_RADIUS_CAP_KM)
    assert radii_used[1] == pytest.approx(MAP_RADIUS_CAP_KM * 2.0)

    # Day 2: starts at the doubled radius; no further escalation/retry
    # (multiplier is already at its one-shot maximum).
    _run_day(date(2026, 8, 2), escalation)
    assert radii_used[2] == pytest.approx(MAP_RADIUS_CAP_KM * 2.0)
    assert len(radii_used) == 3


def test_day_with_comps_never_escalates(monkeypatch):
    radii_used: list = []

    def _fake_collect_search_comps(*args, **kwargs):
        radii_used.append(kwargs.get("map_radius_km"))
        comp = ListingSpec(
            url="https://www.airbnb.com/rooms/111",
            nightly_price=200.0,
            lat=47.6063,
            lng=-122.3322,
        )
        return [comp], 1

    monkeypatch.setattr(benchmark_core, "collect_search_comps", _fake_collect_search_comps)
    monkeypatch.setattr(
        benchmark_core,
        "_extract_benchmark_price_with_min_stay_fallback",
        lambda *a, **k: (250.0, "high", "Nice Place"),
    )
    monkeypatch.setattr(benchmark_core.time, "sleep", lambda *_: None)

    escalation = RadiusEscalation(factor=2.0)
    _run_day(date(2026, 8, 1), escalation)

    assert radii_used == [pytest.approx(MAP_RADIUS_CAP_KM)]
    assert escalation.multiplier == 1.0
