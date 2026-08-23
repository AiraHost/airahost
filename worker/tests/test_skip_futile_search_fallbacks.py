"""search_listings_with_overrides must skip the Playwright fallback for a
StaysSearch direct-HTTP transport failure with no response at all
(`direct_http_failed`), and for an authoritative empty page
(`empty_result_set`) *beyond the first page* — Airbnb re-serves page 1 to the
browser for out-of-range offsets, so a deep-offset fallback costs a
multi-second goto for no new listings (see docs/scraper implementation
prompts/skip_futile_playwright_fallbacks.md for the log evidence backing
that).

A *first*-page empty result is a much weaker signal — a real market/listing
essentially never has zero results on page 1 — so it still escalates to the
browser: a stale/rotated direct-HTTP replay (hardcoded template, expired
persisted-query hash, etc.) looks identical to a genuinely empty market
until a real browser confirms it, and that escalation is also what
re-captures a live StaysSearch template for the rest of the search. Every
other reason must keep escalating to Playwright exactly as before.
"""

import logging
from typing import Any, Dict, List

import worker.scraper.playwright_scraper as pws
from worker.core import scrape_events
from worker.scraper.search_result_contract import VALID_EMPTY, classify_search_payload

_EMPTY_PAYLOAD = {
    "data": {"presentation": {"staysSearch": {"results": {"searchResults": []}}}}
}


class _EventCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.events: List[Dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        payload = getattr(record, scrape_events.EVENT_ATTR, None)
        if isinstance(payload, dict):
            self.events.append(payload)

    def names(self) -> List[str]:
        return [e["event"] for e in self.events]


class _CaptureContext:
    def __enter__(self) -> _EventCapture:
        self.cap = _EventCapture()
        self.logger = logging.getLogger("worker.events")
        self._previous_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.cap)
        return self.cap

    def __exit__(self, *exc_info: Any) -> None:
        self.logger.removeHandler(self.cap)
        self.logger.setLevel(self._previous_level)


def _refuse_browser(overrides: Dict[str, Any], *, op_name: str):
    raise AssertionError("browser search must not be invoked for a skipped fallback reason")


def _make_scraper(*, has_template: bool = True) -> "pws.PlaywrightScraper":
    scraper = object.__new__(pws.PlaywrightScraper)
    scraper.captured_search_req = {"url": "https://x", "post_data": {}} if has_template else None
    scraper.hardcoded_search_req = None
    scraper._search_blocked_reason = None
    scraper._last_direct_search_reason = None
    scraper._last_direct_search_attempt_id = None
    scraper._run_browser_search = _refuse_browser
    return scraper


def test_first_page_empty_result_falls_back_to_browser():
    scraper = _make_scraper()
    scraper.fetch_search_direct = lambda overrides: (200, _EMPTY_PAYLOAD)
    called: Dict[str, bool] = {}

    def fake_browser(overrides: Dict[str, Any], *, op_name: str):
        called["invoked"] = True
        return 200, _EMPTY_PAYLOAD

    scraper._run_browser_search = fake_browser
    with _CaptureContext() as cap:
        result = scraper.search_listings_with_overrides({"checkin": "2026-06-01"})

    assert called.get("invoked") is True
    assert result == (200, _EMPTY_PAYLOAD)
    names = cap.names()
    assert scrape_events.FALLBACK_SELECTED in names
    assert scrape_events.FALLBACK_SKIPPED not in names
    fallback = next(e for e in cap.events if e["event"] == scrape_events.FALLBACK_SELECTED)
    assert fallback["fallback_reason"] == "empty_result_set"


def test_deep_offset_empty_result_returns_unchanged_without_browser():
    scraper = _make_scraper()
    scraper.fetch_search_direct = lambda overrides: (200, _EMPTY_PAYLOAD)
    with _CaptureContext() as cap:
        result = scraper.search_listings_with_overrides(
            {"checkin": "2026-06-01", "itemsOffset": 40}
        )

    assert result == (200, _EMPTY_PAYLOAD)
    names = cap.names()
    assert scrape_events.FALLBACK_SKIPPED in names
    assert scrape_events.FALLBACK_SELECTED not in names


def test_direct_http_failed_returns_canonical_empty_result_without_browser():
    scraper = _make_scraper()
    scraper.fetch_search_direct = lambda overrides: None
    with _CaptureContext() as cap:
        status, data = scraper.search_listings_with_overrides({"checkin": "2026-06-01"})

    assert status == 200
    assert data == pws.build_empty_search_payload()
    # The synthesized payload must be accepted by the existing search
    # classifier the same way a genuine empty page would be.
    state = classify_search_payload(data, status)
    assert state.outcome == VALID_EMPTY
    assert state.reason_code == "empty_result_set"

    names = cap.names()
    assert scrape_events.FALLBACK_SKIPPED in names
    assert scrape_events.FALLBACK_SELECTED not in names
    assert scrape_events.DIRECT_HTTP_SUCCEEDED not in names
    skipped = next(e for e in cap.events if e["event"] == scrape_events.FALLBACK_SKIPPED)
    assert skipped["reason_code"] == "direct_http_failed"
    assert skipped["result_count"] == 0


def test_direct_search_unavailable_still_falls_back_to_browser():
    scraper = _make_scraper(has_template=False)
    called: Dict[str, bool] = {}

    def fake_browser(overrides: Dict[str, Any], *, op_name: str):
        called["invoked"] = True
        return 200, _EMPTY_PAYLOAD

    scraper._run_browser_search = fake_browser
    with _CaptureContext() as cap:
        scraper.search_listings_with_overrides({"checkin": "2026-06-01"})

    assert called.get("invoked") is True
    fallback = next(
        e for e in cap.events if e["event"] == scrape_events.FALLBACK_SELECTED
    )
    assert fallback["fallback_reason"] == "direct_search_unavailable"


def test_graphql_auth_error_still_falls_back_to_browser():
    scraper = _make_scraper()
    blocked_payload = dict(_EMPTY_PAYLOAD)
    blocked_payload["errors"] = [{"extensions": {"code": "CAPTCHA"}}]
    scraper.fetch_search_direct = lambda overrides: (200, blocked_payload)
    called: Dict[str, bool] = {}

    def fake_browser(overrides: Dict[str, Any], *, op_name: str):
        called["invoked"] = True
        return 200, _EMPTY_PAYLOAD

    scraper._run_browser_search = fake_browser
    with _CaptureContext() as cap:
        scraper.search_listings_with_overrides({"checkin": "2026-06-01"})

    assert called.get("invoked") is True
    fallback = next(
        e for e in cap.events if e["event"] == scrape_events.FALLBACK_SELECTED
    )
    assert fallback["fallback_reason"] == "graphql_auth_error"
