"""A confirmed 404 on the user's own listing is terminal, end to end.

Previously `run_scrape()` turned positive 404 evidence into
`([], _empty_transparent("scrape", "No such listing"))`. The orchestrator read
that as an ordinary no-results scrape and went on to run a Playwright daily
retry and the target-listing-only fallback against a listing that does not
exist — burning a full job and ending in a generic "service is busy" error.

The contract now: raise on first evidence, stop everything, fail the report
once with exactly `input listing not found`.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

import pytest

import worker.main as worker_main
import worker.scraper.price_estimator as price_estimator
from worker.core.errors import InputListingNotFound, ReportInputError

LISTING_URL = "https://www.airbnb.com/rooms/123456789"


# ── run_scrape raises on first evidence (required test 15) ──────────────────

def test_run_scrape_raises_terminal_error_on_a_confirmed_404(monkeypatch):
    calls: List[str] = []

    monkeypatch.setattr(
        price_estimator,
        "_preflight_listing_exists",
        lambda client, url: (True, {"roomId": "123456789", "status": 404, "statusIs404": True}),
    )
    monkeypatch.setattr(price_estimator, "AirbnbClient", lambda cfg: object())

    def _spy(name):
        def _fn(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must not run after a confirmed 404")
        return _fn

    monkeypatch.setattr(price_estimator, "extract_target_spec", _spy("extract_target_spec"))

    with pytest.raises(InputListingNotFound) as ctx:
        price_estimator.run_scrape(
            listing_url=LISTING_URL,
            checkin="2026-09-01",
            checkout="2026-09-05",
        )

    assert str(ctx.value) == "input listing not found"
    assert ctx.value.room_id == "123456789"
    assert calls == [], "no scrape stage ran after the 404"


def test_terminal_message_is_exact_and_carries_no_internal_detail():
    exc = InputListingNotFound(room_id="123456789", detail="status=404 statusIs404=True")
    # The public message is what reaches pricing_reports.error_message.
    assert str(exc) == "input listing not found"
    assert "404" not in str(exc)
    assert "123456789" not in str(exc)
    # Internals stay available for structured debug only.
    assert exc.room_id == "123456789"
    assert "status=404" in exc.detail
    assert InputListingNotFound.ERROR_CODE == "input_listing_not_found"


def test_input_listing_not_found_is_a_report_input_error_but_distinct_from_others():
    from worker.core.errors import StaleReportJobError

    assert issubclass(InputListingNotFound, ReportInputError)
    assert not issubclass(InputListingNotFound, StaleReportJobError)
    assert not issubclass(StaleReportJobError, InputListingNotFound)


def test_narrow_200_not_found_page_takes_the_same_terminal_path(monkeypatch):
    # Required test 16: a recognized "page not found" page is as terminal as a
    # literal HTTP 404.
    monkeypatch.setattr(
        price_estimator,
        "_preflight_listing_exists",
        lambda client, url: (
            True,
            {"roomId": "789", "status": 200, "statusIs404": False, "htmlHasOops404": True},
        ),
    )
    monkeypatch.setattr(price_estimator, "AirbnbClient", lambda cfg: object())

    with pytest.raises(InputListingNotFound) as ctx:
        price_estimator.run_scrape(
            listing_url="https://www.airbnb.com/rooms/789",
            checkin="2026-09-01",
            checkout="2026-09-05",
        )
    assert str(ctx.value) == "input listing not found"


def test_inconclusive_preflight_lets_the_scrape_continue(monkeypatch):
    # Required test 20: timeouts/challenges/5xx must never be reported as
    # "input listing not found".
    reached = []

    monkeypatch.setattr(
        price_estimator,
        "_preflight_listing_exists",
        lambda client, url: (False, {"reason": "navigation_failed"}),
    )
    monkeypatch.setattr(price_estimator, "AirbnbClient", lambda cfg: object())

    def _extract(*args, **kwargs):
        reached.append("extract")
        raise RuntimeError("stop the test here — the scrape was allowed to start")

    monkeypatch.setattr(price_estimator, "extract_target_spec", _extract)

    with pytest.raises(RuntimeError) as ctx:
        price_estimator.run_scrape(
            listing_url=LISTING_URL, checkin="2026-09-01", checkout="2026-09-05"
        )

    assert not isinstance(ctx.value, InputListingNotFound)
    assert reached == ["extract"]


# ── _execute_analysis stops everything (required tests 17, 18) ──────────────

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
        worker_main, "set_cached", lambda *a, **k: spies.__setattr__("set_cached", spies.set_cached + 1)
    )
    monkeypatch.setattr(worker_main, "_run_heartbeat", lambda *a, **k: None)

    def _forbid(name):
        def _fn(*a, **k):
            spies.forbidden.append(name)
            raise AssertionError(f"{name} must not run after a confirmed input-listing 404")
        return _fn

    monkeypatch.setattr(worker_main, "_build_target_listing_only_daily_results", _forbid("target_only_fallback"))
    monkeypatch.setattr(worker_main, "_capture_user_listing_prices_for_range", _forbid("live_price_capture"))
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


def test_confirmed_404_fails_the_job_once_and_runs_no_scrape_stage(orchestration, monkeypatch):
    scrape_calls: List[str] = []

    def _run_scrape(**kwargs):
        scrape_calls.append("run_scrape")
        raise InputListingNotFound(room_id="123456789", detail="status=404")

    monkeypatch.setattr(price_estimator, "run_scrape", _run_scrape)

    worker_main._execute_analysis(_job(), uuid.uuid4(), is_nightly=False)

    # Exactly one failure, with the exact public message and stable code.
    assert len(orchestration.fail_job) == 1
    failure = orchestration.fail_job[0]
    assert failure["error_message"] == "input listing not found"
    assert failure["debug"]["error"] == "input_listing_not_found"
    assert failure["debug"]["roomId"] == "123456789"

    # Search/daily retry ran once at most (the first run_scrape); nothing after.
    assert scrape_calls == ["run_scrape"], "no Playwright daily retry after a 404"
    assert orchestration.forbidden == []
    assert orchestration.complete_job == 0
    assert orchestration.set_cached == 0


def test_benchmark_mode_propagates_the_terminal_error_instead_of_falling_back(
    orchestration, monkeypatch
):
    # Required test 18: Mode C's broad `except Exception` used to swallow this
    # and fall through to criteria/URL scraping.
    fallback_calls: List[str] = []

    def _run_benchmark_scrape(**kwargs):
        raise InputListingNotFound(room_id="123456789", detail="status=404")

    def _run_scrape(**kwargs):
        fallback_calls.append("run_scrape")
        return [], {}

    monkeypatch.setattr(price_estimator, "run_benchmark_scrape", _run_benchmark_scrape)
    monkeypatch.setattr(price_estimator, "run_scrape", _run_scrape)

    job = _job()
    job["input_attributes"]["preferredComps"] = [
        {"listingUrl": "https://www.airbnb.com/rooms/555", "enabled": True}
    ]

    worker_main._execute_analysis(job, uuid.uuid4(), is_nightly=False)

    assert fallback_calls == [], "benchmark failure must not fall back after a 404"
    assert len(orchestration.fail_job) == 1
    assert orchestration.fail_job[0]["error_message"] == "input listing not found"


def test_a_comparable_404_is_not_terminal_and_benchmark_still_falls_back(
    orchestration, monkeypatch
):
    # A 404 from an optional benchmark/comparable keeps its non-terminal policy:
    # the ordinary exception path still hands over to URL scraping.
    fallback_calls: List[str] = []

    def _run_benchmark_scrape(**kwargs):
        raise RuntimeError("benchmark comp 404 / unusable")

    def _run_scrape(**kwargs):
        fallback_calls.append("run_scrape")
        raise InputListingNotFound(room_id="123456789", detail="status=404")

    monkeypatch.setattr(price_estimator, "run_benchmark_scrape", _run_benchmark_scrape)
    monkeypatch.setattr(price_estimator, "run_scrape", _run_scrape)

    job = _job()
    job["input_attributes"]["preferredComps"] = [
        {"listingUrl": "https://www.airbnb.com/rooms/555", "enabled": True}
    ]

    worker_main._execute_analysis(job, uuid.uuid4(), is_nightly=False)

    assert fallback_calls == ["run_scrape"], "a comparable failure still falls back"
    assert len(orchestration.fail_job) == 1
    assert orchestration.fail_job[0]["error_message"] == "input listing not found"


def test_blocked_search_session_fails_the_report_with_an_actionable_message(
    orchestration, monkeypatch
):
    # Exhausted recovery against a blocked session must not become a zero-comp
    # "successful" report, and must not be dressed up as a generic busy error.
    from worker.scraper.scraper_errors import AirbnbSearchBlocked

    monkeypatch.setattr(
        price_estimator,
        "run_scrape",
        lambda **k: (_ for _ in ()).throw(AirbnbSearchBlocked("final_url_login")),
    )

    worker_main._execute_analysis(_job(), uuid.uuid4(), is_nightly=False)

    assert len(orchestration.fail_job) == 1
    failure = orchestration.fail_job[0]
    assert failure["error_message"] == (
        "Airbnb search session is blocked; authentication refresh required."
    )
    assert failure["debug"]["error"] == "airbnb_search_blocked"
    assert failure["debug"]["reasonCode"] == "final_url_login"
    assert orchestration.complete_job == 0
    assert orchestration.set_cached == 0
    assert orchestration.forbidden == [], "no target-only fallback after a session block"


def test_stale_dates_fail_before_any_scrape_is_attempted(orchestration, monkeypatch):
    scrape_calls: List[str] = []
    monkeypatch.setattr(
        price_estimator, "run_scrape", lambda **k: scrape_calls.append("run_scrape") or ([], {})
    )
    monkeypatch.setenv("REPORT_BUSINESS_TIMEZONE", "UTC")

    worker_main._execute_analysis(
        _job(input_date_start="2020-01-01", input_date_end="2020-01-05"),
        uuid.uuid4(),
        is_nightly=False,
    )

    assert scrape_calls == []
    assert len(orchestration.fail_job) == 1
    assert orchestration.fail_job[0]["debug"]["error"] == "report_dates_in_past"
    assert orchestration.set_cached == 0
