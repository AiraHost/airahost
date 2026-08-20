"""A host that cannot give the worker a browser fails the report, once, honestly.

In the incident the WinError 1455 spawn failure reached the orchestrator as a
plain exception, so the report ended as the generic "Service is busy. An error
occurred during analysis" — indistinguishable from an Airbnb-side problem, and
only after the browser fallback had been re-attempted per date and offset.

The typed error now carries a fixed public message and sanitized structured
fields, and the orchestrator fails the job exactly once with them.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

import pytest

import worker.main as worker_main
import worker.scraper.price_estimator as price_estimator
from worker.scraper.scraper_errors import (
    BrowserRuntimeResourceExhausted,
    BrowserRuntimeUnavailable,
)

LISTING_URL = "https://www.airbnb.com/rooms/123456789"


class _FakeQuery:
    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def single(self):
        return self

    def execute(self):
        return type("R", (), {"data": {}})()


class _FakeSupabase:
    def table(self, _name):
        return _FakeQuery()


class _Spies:
    def __init__(self):
        self.fail_job: List[Dict[str, Any]] = []
        self.complete_job = 0
        self.set_cached = 0
        self.forbidden: List[str] = []


@pytest.fixture
def orchestration(monkeypatch):
    spies = _Spies()

    monkeypatch.setattr(worker_main.db_helpers, "get_client", lambda: _FakeSupabase())
    monkeypatch.setattr(worker_main.db_helpers, "update_progress", lambda *a, **k: None)
    monkeypatch.setattr(
        worker_main.db_helpers,
        "fail_job",
        lambda client, report_id, token, *, error_message, debug: spies.fail_job.append(
            {"error_message": error_message, "debug": debug}
        ),
    )
    monkeypatch.setattr(
        worker_main.db_helpers,
        "complete_job",
        lambda *a, **k: spies.__setattr__("complete_job", spies.complete_job + 1),
    )
    monkeypatch.setattr(worker_main, "get_cached", lambda client, key: None)
    monkeypatch.setattr(
        worker_main,
        "set_cached",
        lambda *a, **k: spies.__setattr__("set_cached", spies.set_cached + 1),
    )
    monkeypatch.setattr(worker_main, "_run_heartbeat", lambda *a, **k: None)

    def _forbid(name):
        def _fn(*a, **k):
            spies.forbidden.append(name)
            raise AssertionError(
                f"{name} must not run after the browser runtime is unavailable"
            )

        return _fn

    monkeypatch.setattr(
        worker_main, "_build_target_listing_only_daily_results", _forbid("target_only_fallback")
    )
    monkeypatch.setattr(
        worker_main, "_capture_user_listing_prices_for_range", _forbid("live_price_capture")
    )
    monkeypatch.setattr(worker_main, "_build_scrape_calendar", _forbid("build_calendar"))
    return spies


def _job(**overrides):
    job = {
        "id": str(uuid.uuid4()),
        "input_address": "Belmont, California",
        "input_attributes": {"inputMode": "url", "listingUrl": LISTING_URL},
        "input_listing_url": LISTING_URL,
        "input_date_start": "2099-09-01",
        "input_date_end": "2099-09-05",
        "discount_policy": {},
        "minimum_booking_nights": 1,
    }
    job.update(overrides)
    return job


def test_resource_exhaustion_fails_the_job_once_with_the_sanitized_message(
    orchestration, monkeypatch
):
    scrape_calls: List[str] = []

    def _run_scrape(**kwargs):
        scrape_calls.append("run_scrape")
        raise BrowserRuntimeResourceExhausted(
            operation="browser_fallback",
            endpoint="127.0.0.1:9222",
            active_runtimes=0,
            runtime_cap=2,
            attempt=1,
        )

    monkeypatch.setattr(price_estimator, "run_scrape", _run_scrape)

    worker_main._execute_analysis(_job(), uuid.uuid4(), is_nightly=False)

    assert len(orchestration.fail_job) == 1
    failure = orchestration.fail_job[0]
    assert failure["error_message"] == (
        "browser runtime unavailable: system virtual memory exhausted"
    )
    assert failure["debug"]["error"] == "browser_runtime_resource_exhausted"
    assert failure["debug"]["reasonCode"] == "process_spawn_resource_exhausted"
    assert failure["debug"]["endpoint"] == "127.0.0.1:9222"
    assert failure["debug"]["runtimeCap"] == 2

    # Not an empty-search success, not a challenge, and no second browser pass.
    assert scrape_calls == ["run_scrape"]
    assert orchestration.forbidden == []
    assert orchestration.complete_job == 0
    assert orchestration.set_cached == 0


def test_generic_runtime_unavailability_uses_the_base_public_message(
    orchestration, monkeypatch
):
    monkeypatch.setattr(
        price_estimator,
        "run_scrape",
        lambda **k: (_ for _ in ()).throw(
            BrowserRuntimeUnavailable("cdp_connect_failed", endpoint="127.0.0.1:9222")
        ),
    )

    worker_main._execute_analysis(_job(), uuid.uuid4(), is_nightly=False)

    assert len(orchestration.fail_job) == 1
    failure = orchestration.fail_job[0]
    assert failure["error_message"] == "browser runtime unavailable"
    assert failure["debug"]["error"] == "browser_runtime_unavailable"
    assert failure["debug"]["reasonCode"] == "cdp_connect_failed"


def test_live_price_capture_degrades_instead_of_discarding_a_finished_report(
    monkeypatch,
):
    # Live-price capture only enriches an already-built calendar, so losing the
    # browser there must not throw the analysis away.
    def _boom(**_kwargs):
        raise BrowserRuntimeResourceExhausted(operation="self_price_capture:warmup")

    monkeypatch.setattr(worker_main, "_capture_user_listing_prices_for_range", _boom)

    payload = worker_main._capture_user_listing_prices_or_degrade(
        report_id="r-1",
        listing_url=LISTING_URL,
        start_date="2026-06-01",
        end_date="2026-06-05",
        minimum_booking_nights=1,
    )

    assert payload["priceByDate"] == {}
    assert payload["capturedDays"] == 0
    assert payload["totalDays"] == 4
    assert payload["observedListingPrice"] is None
    assert payload["livePriceStatus"] == "browser_runtime_unavailable"


def test_the_public_message_leaks_nothing_operational(orchestration, monkeypatch):
    monkeypatch.setattr(
        price_estimator,
        "run_scrape",
        lambda **k: (_ for _ in ()).throw(
            BrowserRuntimeResourceExhausted(
                operation="self_price_capture:warmup",
                endpoint="127.0.0.1:9222",
                detail="C:/Users/Aira/... CreateProcess node.exe",
            )
        ),
    )

    worker_main._execute_analysis(_job(), uuid.uuid4(), is_nightly=False)

    failure = orchestration.fail_job[0]
    blob = f"{failure['error_message']} {failure['debug']}".lower()
    for forbidden in ("createprocess", "node.exe", "c:/users", "cookie", "token", "<html"):
        assert forbidden not in blob
