"""
Direct-HTTP-first PDP detail fetch.

These tests pin the speed optimization: PDP structural/detail fetches must be
served by the direct StaysPdpSections HTTP replay (no browser tab) whenever the
replay returns a usable payload, and must fall back to the browser only when it
does not. Regressing this silently re-introduces a browser navigation per comp /
per target listing — the multi-minute slowdown this change removes.
"""

from __future__ import annotations

from typing import Any, Dict

from worker.scraper.playwright_scraper import PlaywrightScraper


def _scraper_with_session_cookie() -> PlaywrightScraper:
    scraper = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})
    # Direct replay is gated on an authenticated session + a captured template.
    scraper.captured_pdp_req = {"url": "x", "headers": {}, "post_data": {"variables": {}}}
    scraper.session.cookies.set("bev", "1", domain=".airbnb.com", path="/")
    return scraper


def _priced_payload() -> Dict[str, Any]:
    # Minimal but structurally-valid PDP payload that carries a booking price.
    return PlaywrightScraper._build_minimal_pdp_payload("$155")


def test_fast_detail_uses_direct_http_and_skips_browser(monkeypatch) -> None:
    scraper = _scraper_with_session_cookie()
    payload = _priced_payload()

    monkeypatch.setattr(scraper, "fetch_pdp_price_direct", lambda **_kw: payload)

    def _browser_should_not_run(*_a, **_k):
        raise AssertionError("browser path must not run when direct HTTP succeeds")

    monkeypatch.setattr(scraper, "_get_listing_details_browser", _browser_should_not_run)

    result = scraper.get_listing_details_fast(
        "123", checkin="2026-06-01", checkout="2026-06-02", adults=2
    )
    assert result is payload


def test_fast_detail_accepts_no_price_payload(monkeypatch) -> None:
    # Structural enrichment must accept a usable payload even without a price
    # (e.g. dates unavailable) rather than paying for a browser navigation.
    scraper = _scraper_with_session_cookie()
    payload = PlaywrightScraper._build_minimal_pdp_payload(None)  # sections, no price

    monkeypatch.setattr(scraper, "fetch_pdp_price_direct", lambda **_kw: payload)
    monkeypatch.setattr(
        scraper,
        "_get_listing_details_browser",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not browse")),
    )

    result = scraper.get_listing_details_fast("123", checkin="2026-06-01", checkout="2026-06-02")
    assert result is payload


def test_fast_detail_falls_back_to_browser_on_empty_payload(monkeypatch) -> None:
    scraper = _scraper_with_session_cookie()
    sentinel = {"from": "browser"}

    monkeypatch.setattr(scraper, "fetch_pdp_price_direct", lambda **_kw: {})  # not a PDP
    monkeypatch.setattr(scraper, "_get_listing_details_browser", lambda *_a, **_k: sentinel)

    result = scraper.get_listing_details_fast("123", checkin="2026-06-01", checkout="2026-06-02")
    assert result is sentinel


def test_price_path_defers_no_price_payload_to_browser(monkeypatch) -> None:
    # get_listing_details (price-aware) must NOT accept a no-price/bookable payload
    # from direct HTTP — it defers to the browser so the DOM price fallback can run.
    scraper = _scraper_with_session_cookie()
    no_price = PlaywrightScraper._build_minimal_pdp_payload(None)
    sentinel = {"from": "browser"}

    monkeypatch.setattr(scraper, "fetch_pdp_price_direct", lambda **_kw: no_price)
    monkeypatch.setattr(scraper, "_get_listing_details_browser", lambda *_a, **_k: sentinel)

    result = scraper.get_listing_details("123", checkin="2026-06-01", checkout="2026-06-02")
    assert result is sentinel


def test_direct_replay_skipped_without_session_cookies(monkeypatch) -> None:
    # No authenticated cookies -> direct replay is doomed; go straight to browser.
    scraper = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})
    scraper.captured_pdp_req = {"url": "x", "headers": {}, "post_data": {"variables": {}}}
    sentinel = {"from": "browser"}

    def _direct_should_not_run(**_kw):
        raise AssertionError("direct replay must not run without session cookies")

    monkeypatch.setattr(scraper, "fetch_pdp_price_direct", _direct_should_not_run)
    monkeypatch.setattr(scraper, "_get_listing_details_browser", lambda *_a, **_k: sentinel)

    result = scraper.get_listing_details_fast("123", checkin="2026-06-01", checkout="2026-06-02")
    assert result is sentinel
