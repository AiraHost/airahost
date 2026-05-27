from __future__ import annotations

from typing import Any, List

import pytest

from worker.scraper.playwright_scraper import PlaywrightScraper


def _close_coro(coro: Any) -> None:
    close = getattr(coro, "close", None)
    if callable(close):
        close()


def test_pdp_reconnects_once_after_browser_context_closes(monkeypatch) -> None:
    scraper = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})
    calls: List[str] = []
    pdp_calls = 0

    def _fake_run_async(coro: Any, *, op_name: str, timeout_seconds=None):
        nonlocal pdp_calls
        _close_coro(coro)
        calls.append(op_name)
        if op_name == "get_listing_details":
            pdp_calls += 1
            if pdp_calls == 1:
                raise RuntimeError("BrowserContext.new_page: Target page, context or browser has been closed")
            return 200, {"recovered": True}
        return None

    monkeypatch.setattr(scraper, "_run_async", _fake_run_async)

    result = scraper.get_listing_details(
        "123456789",
        checkin="2026-06-01",
        checkout="2026-06-02",
        adults=1,
    )

    assert result == {"recovered": True}
    assert calls == [
        "get_listing_details",
        "get_listing_details_browser_reset",
        "get_listing_details",
    ]


def test_html_navigation_reconnects_after_new_tab_failure(monkeypatch) -> None:
    scraper = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})
    calls: List[str] = []

    def _fake_run_async(coro: Any, *, op_name: str, timeout_seconds=None):
        _close_coro(coro)
        calls.append(op_name)
        if op_name == "browse_url_html":
            raise RuntimeError("BrowserContext.new_page: Protocol error (Target.createTarget): Failed to open a new tab")
        if op_name == "browse_url_html_retry":
            return {"html": "<html>ok</html>"}
        return None

    monkeypatch.setattr(scraper, "_run_async", _fake_run_async)

    result = scraper.browse_url_html("https://www.airbnb.com/rooms/123456789")

    assert result == {"html": "<html>ok</html>"}
    assert calls == [
        "browse_url_html",
        "browse_url_html_browser_reset",
        "browse_url_html_retry",
    ]


def test_pdp_does_not_retry_non_browser_failures(monkeypatch) -> None:
    scraper = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})
    calls: List[str] = []

    def _fake_run_async(coro: Any, *, op_name: str, timeout_seconds=None):
        _close_coro(coro)
        calls.append(op_name)
        raise RuntimeError("invalid parsed response")

    monkeypatch.setattr(scraper, "_run_async", _fake_run_async)

    with pytest.raises(RuntimeError, match="invalid parsed response"):
        scraper.get_listing_details(
            "123456789",
            checkin="2026-06-01",
            checkout="2026-06-02",
            adults=1,
        )

    assert calls == ["get_listing_details"]
