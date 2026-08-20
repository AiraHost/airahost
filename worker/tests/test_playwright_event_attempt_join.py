"""Every network attempt must be joinable to its outcome by attempt_id.

An operator answering "did the Playwright fallback for this search succeed?"
has exactly one reliable method: find the `playwright_started` event for the
attempt and follow its `attempt_id` to a terminal event (`playwright_captured_json`
or `playwright_failed`). Before this fix that join was broken two ways:

  * The terminal events never carried `attempt_id` at all, so a `started` could
    not be matched to *any* outcome, successful or not.
  * `BrowserRuntimeUnavailable` (browser search) and every non-circuit-open
    browser PDP failure logged only a plain-text warning — no terminal event
    existed to join to.

Also covers the `direct_json` search path, where the wrapper-level success
event (`operation=stays_search`, the majority of all search traffic) dropped
`attempt_id` even though the inner `stays_search_direct` attempt had one.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from worker.core import scrape_events
from worker.core.admission import reset_admission_controller_for_tests
from worker.core.scrape_trace import trace_scope
from worker.scraper.playwright_scraper import PlaywrightScraper
from worker.scraper.scraper_errors import AirbnbSearchBlocked, BrowserRuntimeUnavailable


class _EventCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.events: List[Dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        payload = getattr(record, scrape_events.EVENT_ATTR, None)
        if isinstance(payload, dict):
            self.events.append(payload)


@pytest.fixture(autouse=True)
def _fresh_admission():
    reset_admission_controller_for_tests()
    yield
    reset_admission_controller_for_tests()


@pytest.fixture
def capture():
    cap = _EventCapture()
    logger = logging.getLogger("worker.events")
    # Under pytest the root logger defaults to WARNING, which worker.events
    # inherits — INFO-level events (playwright_started, *_captured_json) would
    # never reach a handler at all without this, silently passing a test that
    # only happened to look at WARNING-level playwright_failed events.
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(cap)
    try:
        yield cap
    finally:
        logger.removeHandler(cap)
        logger.setLevel(previous_level)


def _close_coro(coro: Any) -> None:
    close = getattr(coro, "close", None)
    if callable(close):
        close()


def _payload(count: int = 3) -> Dict[str, Any]:
    return {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [{"listingId": str(9000 + i)} for i in range(count)]
                    }
                }
            }
        }
    }


class _Driver:
    """Scripts each `_run_async` call so the real retry loop runs unmodified."""

    def __init__(self, outcomes: List[Any]):
        self.outcomes = list(outcomes)

    def __call__(self, coro: Any, *, op_name: str = "", timeout_seconds=None):
        _close_coro(coro)
        outcome = self.outcomes.pop(0) if self.outcomes else RuntimeError("no outcome scripted")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _browser_search_scraper(driver: _Driver, monkeypatch) -> PlaywrightScraper:
    scraper = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})
    scraper.captured_search_req = None
    scraper.hardcoded_search_req = None
    monkeypatch.setattr(scraper, "_run_async", driver)
    monkeypatch.setattr(scraper._runtime, "reset", lambda: None)
    monkeypatch.setattr(scraper, "refresh_session", lambda **kwargs: None)
    monkeypatch.setattr(scraper, "_ensure_session_cookies_from_browser", lambda: None)
    monkeypatch.setattr("worker.scraper.playwright_scraper.time.sleep", lambda _s: None)
    return scraper


def _started_and_terminal(events: List[Dict[str, Any]]):
    started = [e for e in events if e["event"] == "playwright_started"]
    terminal = [
        e for e in events if e["event"] in ("playwright_captured_json", "playwright_failed")
    ]
    return started, terminal


def test_successful_fallback_joins_by_attempt_id(capture, monkeypatch):
    driver = _Driver([(200, _payload())])
    scraper = _browser_search_scraper(driver, monkeypatch)

    with trace_scope(report_id="rep-join-ok"):
        scraper.search_listings_with_overrides({"checkin": "2026-09-01"})

    started, terminal = _started_and_terminal(capture.events)
    assert len(started) == 1
    assert len(terminal) == 1
    assert terminal[0]["event"] == "playwright_captured_json"
    assert terminal[0]["attempt_id"] == started[0]["attempt_id"]
    assert terminal[0]["attempt_id"]  # not empty/None


def test_blocked_fallback_joins_by_attempt_id_on_every_attempt(capture, monkeypatch):
    driver = _Driver(
        [AirbnbSearchBlocked("final_url_login"), AirbnbSearchBlocked("final_url_login")]
    )
    scraper = _browser_search_scraper(driver, monkeypatch)

    with trace_scope(report_id="rep-join-blocked"), pytest.raises(AirbnbSearchBlocked):
        scraper.search_listings_with_overrides({"checkin": "2026-09-01"})

    started, terminal = _started_and_terminal(capture.events)
    assert len(started) == 2
    assert len(terminal) == 2
    started_ids = {e["attempt_id"] for e in started}
    terminal_ids = {e["attempt_id"] for e in terminal}
    assert started_ids == terminal_ids, "every start must have a same-ID terminal event"
    assert all(e["event"] == "playwright_failed" for e in terminal)


def test_browser_runtime_unavailable_still_emits_a_terminal_event(capture, monkeypatch):
    """Previously: a plain-text warning only, no structured event at all."""
    driver = _Driver([BrowserRuntimeUnavailable("cdp_connect_failed")])
    scraper = _browser_search_scraper(driver, monkeypatch)

    with trace_scope(report_id="rep-join-runtime"), pytest.raises(BrowserRuntimeUnavailable):
        scraper.search_listings_with_overrides({"checkin": "2026-09-01"})

    started, terminal = _started_and_terminal(capture.events)
    assert len(started) == 1
    assert len(terminal) == 1, "BrowserRuntimeUnavailable must produce a terminal event"
    assert terminal[0]["attempt_id"] == started[0]["attempt_id"]
    assert terminal[0]["reason_code"] == "cdp_connect_failed"


def test_generic_browser_failure_joins_by_attempt_id(capture, monkeypatch):
    driver = _Driver([RuntimeError("navigation crashed"), RuntimeError("navigation crashed")])
    scraper = _browser_search_scraper(driver, monkeypatch)

    with trace_scope(report_id="rep-join-generic"), pytest.raises(RuntimeError):
        scraper.search_listings_with_overrides({"checkin": "2026-09-01"})

    started, terminal = _started_and_terminal(capture.events)
    assert len(started) == len(terminal) == 2
    assert {e["attempt_id"] for e in started} == {e["attempt_id"] for e in terminal}


def _search_template() -> Dict[str, Any]:
    return {
        "url": "https://www.airbnb.com/api/v3/StaysSearch/abc?operationName=StaysSearch",
        "method": "POST",
        "headers": {"content-type": "application/json"},
        "post_data": {
            "operationName": "StaysSearch",
            "variables": {"staysSearchRequest": {"rawParams": []}},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "deadbeef"}},
        },
    }


def test_direct_success_wrapper_event_carries_the_inner_attempt_id(capture):
    """The `stays_search` wrapper event (the majority of all search traffic)
    must expose the same attempt_id as its inner `stays_search_direct` attempt.
    """

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "data": {
                    "presentation": {
                        "staysSearch": {"results": {"searchResults": [{"listingId": "1"}]}}
                    }
                }
            }

    class _Session:
        def post(self, *a, **k):
            return _Resp()

    scraper = PlaywrightScraper({"USE_HARDCODED_STAYSPDP_TEMPLATE": "0"})
    scraper.captured_search_req = _search_template()
    scraper.session = _Session()

    with trace_scope(report_id="rep-direct-join"):
        scraper.search_listings_with_overrides({"checkin": "2026-09-01"})

    inner_started = [
        e
        for e in capture.events
        if e["event"] == "direct_http_started" and e.get("operation") == "stays_search_direct"
    ]
    wrapper_succeeded = [
        e
        for e in capture.events
        if e["event"] == "direct_http_succeeded" and e.get("reason_code") == "stayssearch_direct_ok"
    ]
    assert len(inner_started) == 1
    assert len(wrapper_succeeded) == 1
    assert wrapper_succeeded[0].get("attempt_id"), "wrapper event dropped attempt_id"
    assert wrapper_succeeded[0]["attempt_id"] == inner_started[0]["attempt_id"]


def _pdp_scraper(driver: _Driver, monkeypatch) -> PlaywrightScraper:
    scraper = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})
    monkeypatch.setattr(scraper, "_run_async", driver)
    monkeypatch.setattr(
        scraper, "_reset_browser_connection_after_failure", lambda exc, *, op_name: True
    )
    monkeypatch.setattr("worker.scraper.playwright_scraper.time.sleep", lambda _s: None)
    return scraper


def test_browser_pdp_generic_failure_now_emits_a_terminal_event(capture, monkeypatch):
    """Previously: only a plain-text warning for non-CDP, non-rate-limited
    browser PDP failures — no terminal event, so the attempt was unjoinable.
    """
    driver = _Driver([RuntimeError("navigation crashed"), RuntimeError("navigation crashed")])
    scraper = _pdp_scraper(driver, monkeypatch)

    with trace_scope(report_id="rep-pdp-join"), pytest.raises(RuntimeError):
        scraper._get_listing_details_browser("12345", "2026-09-01", "2026-09-02", 2)

    started, terminal = _started_and_terminal(capture.events)
    assert len(started) == 2
    assert len(terminal) == 2, "browser PDP failures must each emit a terminal event"
    assert {e["attempt_id"] for e in started} == {e["attempt_id"] for e in terminal}


def test_browser_pdp_success_joins_by_attempt_id(capture, monkeypatch):
    # _get_listing_details_browser unpacks (status, response_data).
    driver = _Driver([(200, {"data": {"ok": True}})])
    scraper = _pdp_scraper(driver, monkeypatch)

    with trace_scope(report_id="rep-pdp-join-ok"):
        scraper._get_listing_details_browser("12345", "2026-09-01", "2026-09-02", 2)

    started, terminal = _started_and_terminal(capture.events)
    assert len(started) == 1
    assert len(terminal) == 1
    assert terminal[0]["event"] == "playwright_captured_json"
    assert terminal[0]["attempt_id"] == started[0]["attempt_id"]
