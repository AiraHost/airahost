"""Browser StaysSearch: blocked handling, bounded recovery, circuit breaker.

Production ordering that these tests forbid:

    challenge falsely detected
      -> every /rooms/ anchor on the page scraped
      -> synthetic HTTP 200 StaysSearch payload returned
      -> collector accepts the page, 18 ID-only candidates, 0 priced comps,
         repeated for every date and offset in the report.

A blocked page now raises AirbnbSearchBlocked, recovery runs at most once, and
a session-wide block short-circuits the rest of the report instead of opening a
browser page per date/offset.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import pytest

from worker.scraper.playwright_scraper import PlaywrightScraper
from worker.scraper.scraper_errors import AirbnbSearchBlocked, AirbnbSearchDegraded


def _scraper() -> PlaywrightScraper:
    return PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})


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


class _SearchDriver:
    """Drives _run_browser_search by scripting each _search_via_browser call."""

    def __init__(self, outcomes: List[Any]):
        self.outcomes = list(outcomes)
        self.pages_opened = 0
        self.ops: List[str] = []

    def __call__(self, coro: Any, *, op_name: str, timeout_seconds=None):
        _close_coro(coro)
        self.ops.append(op_name)
        # Every real search attempt opens exactly one browser page.
        self.pages_opened += 1
        outcome = self.outcomes.pop(0) if self.outcomes else RuntimeError("no outcome scripted")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _install(scraper: PlaywrightScraper, driver: _SearchDriver, monkeypatch) -> None:
    # Force the browser path: with a template present, _try_direct_search would
    # issue a live HTTP request, which no unit test may depend on.
    scraper.captured_search_req = None
    scraper.hardcoded_search_req = None
    monkeypatch.setattr(scraper, "_run_async", driver)
    # The CDP reset is now a teardown of the shared endpoint runtime rather than
    # a coroutine on the loop, so it is observed on the runtime.
    monkeypatch.setattr(
        scraper._runtime, "reset", lambda: driver.ops.append("runtime_reset")
    )
    monkeypatch.setattr(scraper, "refresh_session", lambda **kwargs: None)
    monkeypatch.setattr(scraper, "_ensure_session_cookies_from_browser", lambda: None)
    monkeypatch.setattr("worker.scraper.playwright_scraper.time.sleep", lambda _s: None)


# ── A blocked page never becomes a 200 payload (required test 1) ─────────────

def test_blocked_page_raises_instead_of_returning_a_minimal_payload(monkeypatch):
    driver = _SearchDriver(
        [AirbnbSearchBlocked("visible_captcha"), AirbnbSearchBlocked("visible_captcha")]
    )
    scraper = _scraper()
    _install(scraper, driver, monkeypatch)

    with pytest.raises(AirbnbSearchBlocked) as ctx:
        scraper.search_listings_with_overrides({"checkin": "2026-09-01"})

    assert ctx.value.reason_code == "visible_captcha"
    # No payload of any kind escaped, synthetic or otherwise.
    assert "200" not in str(ctx.value)


def test_blocked_exception_message_leaks_no_html_cookies_or_query(monkeypatch):
    exc = AirbnbSearchBlocked("final_url_login")
    message = str(exc)
    assert "<" not in message
    assert "cookie" not in message.lower()
    assert "checkin" not in message.lower()
    assert "final_url_login" in message


# ── Bounded recovery (required tests 3, 4) ───────────────────────────────────

def test_first_block_triggers_one_recovery_then_a_healthy_retry_succeeds(monkeypatch):
    driver = _SearchDriver([AirbnbSearchBlocked("final_url_login"), (200, _payload())])
    scraper = _scraper()
    _install(scraper, driver, monkeypatch)

    status, data = scraper.search_listings_with_overrides({"checkin": "2026-09-01"})

    assert status == 200
    assert data == _payload()
    assert driver.pages_opened == 2
    # Recovery ran exactly once, between the two attempts.
    assert driver.ops.count("runtime_reset") == 1
    # A recovered session does not leave the breaker latched.
    assert scraper._search_blocked_reason is None


def test_two_blocked_attempts_raise_and_open_no_third_page(monkeypatch):
    driver = _SearchDriver(
        [AirbnbSearchBlocked("final_url_challenge"), AirbnbSearchBlocked("final_url_challenge")]
    )
    scraper = _scraper()
    _install(scraper, driver, monkeypatch)

    with pytest.raises(AirbnbSearchBlocked):
        scraper.search_listings_with_overrides({"checkin": "2026-09-01"})

    assert driver.pages_opened == 2  # the configured attempt budget, no more
    assert scraper.SEARCH_ATTEMPT_BUDGET == 2


# ── Session circuit breaker (required test 5) ────────────────────────────────

def test_circuit_breaker_stops_a_navigation_storm_across_later_dates(monkeypatch):
    driver = _SearchDriver(
        [AirbnbSearchBlocked("final_url_login"), AirbnbSearchBlocked("final_url_login")]
    )
    scraper = _scraper()
    _install(scraper, driver, monkeypatch)

    with pytest.raises(AirbnbSearchBlocked):
        scraper.search_listings_with_overrides({"checkin": "2026-09-01"})
    pages_after_first_date = driver.pages_opened

    # Every later date/offset in the same report must fail fast.
    for day in range(2, 10):
        for offset in (0, 20, 40):
            with pytest.raises(AirbnbSearchBlocked) as ctx:
                scraper.search_listings_with_overrides(
                    {"checkin": f"2026-09-0{day}", "itemsOffset": offset}
                )
            assert ctx.value.reason_code == "final_url_login"

    assert driver.pages_opened == pages_after_first_date  # no further navigations


def test_circuit_breaker_resets_after_a_verified_healthy_search(monkeypatch):
    scraper = _scraper()
    driver = _SearchDriver([])
    _install(scraper, driver, monkeypatch)
    scraper._trip_search_circuit("final_url_login")

    with pytest.raises(AirbnbSearchBlocked):
        scraper.search_listings_with_overrides({})

    # A healthy direct-HTTP result is authoritative evidence the session works.
    scraper._reset_search_circuit()
    driver.outcomes = [(200, _payload())]
    status, _ = scraper.search_listings_with_overrides({})
    assert status == 200
    assert scraper._search_blocked_reason is None


def test_degraded_is_not_blocked_and_does_not_trip_the_breaker(monkeypatch):
    # Missing StaysSearch traffic alone is not a challenge.
    driver = _SearchDriver(
        [AirbnbSearchDegraded("no_stayssearch_response_hydrating_or_shell")] * 2
    )
    scraper = _scraper()
    _install(scraper, driver, monkeypatch)

    with pytest.raises(RuntimeError) as ctx:
        scraper.search_listings_with_overrides({})

    assert not isinstance(ctx.value, AirbnbSearchBlocked)
    assert scraper._search_blocked_reason is None


# ── Filter-change fallback URL (required test 27) ────────────────────────────

def test_filter_change_fallback_url_has_exactly_one_search_type():
    scraper = _scraper()
    search_url = scraper._build_search_navigation_url({"query": "Belmont, California"})
    assert parse_qs(urlparse(search_url).query)["search_type"] == ["AUTOSUGGEST"]

    fallback = scraper._force_url_query_params(search_url, search_type="filter_change")
    values = parse_qs(urlparse(fallback).query)["search_type"]

    # The old code appended "&search_type=filter_change" whenever search_type
    # was already present, sending Airbnb both AUTOSUGGEST and filter_change.
    assert values == ["filter_change"]
    assert fallback.count("search_type=") == 1


# ── Payload acceptance at the browser boundary ───────────────────────────────

def test_captured_payload_with_auth_error_is_rejected_as_blocked(monkeypatch):
    auth_error_payload = {
        **_payload(),
        "errors": [{"message": "Not authorized", "extensions": {"code": "UNAUTHORIZED"}}],
    }
    driver = _SearchDriver([(200, auth_error_payload), (200, auth_error_payload)])
    scraper = _scraper()
    _install(scraper, driver, monkeypatch)

    with pytest.raises(AirbnbSearchBlocked) as ctx:
        scraper.search_listings_with_overrides({})
    assert ctx.value.reason_code == "graphql_auth_error"


def test_direct_search_hit_clears_a_stale_breaker_flag(monkeypatch):
    scraper = _scraper()
    scraper.captured_search_req = {"url": "https://x", "post_data": {}}
    scraper.hardcoded_search_req = None
    monkeypatch.setattr(scraper, "fetch_search_direct", lambda overrides: (200, _payload()))

    status, _ = scraper.search_listings_with_overrides({})
    assert status == 200
    assert scraper._search_blocked_reason is None
