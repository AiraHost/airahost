"""Bounded recovery after the CDP browser goes away mid-operation.

A disconnect is worth exactly one retry, and that retry must start from a clean
runtime. Recovery is observed on the shared endpoint runtime rather than on
_run_async: the reset is a full teardown of the driver/loop thread now, not a
coroutine submitted to a loop the failed connection still owns.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from worker.scraper.playwright_scraper import PlaywrightScraper
from worker.scraper.scraper_errors import BrowserRuntimeResourceExhausted


def _close_coro(coro: Any) -> None:
    close = getattr(coro, "close", None)
    if callable(close):
        close()


def _scraper_with_recorder(monkeypatch, handler):
    """A scraper whose async ops and runtime resets land in one ordered list."""
    scraper = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})
    calls: List[str] = []

    def _fake_run_async(coro: Any, *, op_name: str, timeout_seconds=None):
        _close_coro(coro)
        calls.append(op_name)
        return handler(op_name)

    monkeypatch.setattr(scraper, "_run_async", _fake_run_async)
    monkeypatch.setattr(
        scraper._runtime, "reset", lambda: calls.append("runtime_reset")
    )
    return scraper, calls


def test_pdp_reconnects_once_after_browser_context_closes(monkeypatch) -> None:
    pdp_calls = 0

    def _handler(op_name: str):
        nonlocal pdp_calls
        if op_name == "get_listing_details":
            pdp_calls += 1
            if pdp_calls == 1:
                raise RuntimeError(
                    "BrowserContext.new_page: Target page, context or browser has been closed"
                )
            return 200, {"recovered": True}
        return None

    scraper, calls = _scraper_with_recorder(monkeypatch, _handler)

    result = scraper.get_listing_details(
        "123456789",
        checkin="2026-06-01",
        checkout="2026-06-02",
        adults=1,
    )

    assert result == {"recovered": True}
    # Exactly one reset, between the two attempts.
    assert calls.count("runtime_reset") == 1
    pdp_ops = [c for c in calls if c == "get_listing_details"]
    assert len(pdp_ops) == 2
    assert calls.index("runtime_reset") < len(calls) - 1


def test_html_navigation_reconnects_after_new_tab_failure(monkeypatch) -> None:
    def _handler(op_name: str):
        if op_name == "browse_url_html":
            raise RuntimeError(
                "BrowserContext.new_page: Protocol error (Target.createTarget): "
                "Failed to open a new tab"
            )
        if op_name == "browse_url_html_retry":
            return {"html": "<html>ok</html>"}
        return None

    scraper, calls = _scraper_with_recorder(monkeypatch, _handler)

    result = scraper.browse_url_html("https://www.airbnb.com/rooms/123456789")

    assert result == {"html": "<html>ok</html>"}
    assert calls == [
        "browse_url_html",
        "runtime_reset",
        "browse_url_html_retry",
    ]


def test_pdp_does_not_retry_non_browser_failures(monkeypatch) -> None:
    def _handler(op_name: str):
        raise RuntimeError("invalid parsed response")

    scraper, calls = _scraper_with_recorder(monkeypatch, _handler)

    with pytest.raises(RuntimeError, match="invalid parsed response"):
        scraper.get_listing_details(
            "123456789",
            checkin="2026-06-01",
            checkout="2026-06-02",
            adults=1,
        )

    assert "runtime_reset" not in calls
    assert calls.count("get_listing_details") == 1


def test_runtime_unavailable_is_not_treated_as_a_recoverable_disconnect(
    monkeypatch,
) -> None:
    # A host that cannot spawn a driver must not be retried as if the browser had
    # merely disconnected — that is exactly the retry storm the incident showed.
    def _handler(op_name: str):
        raise BrowserRuntimeResourceExhausted(operation=op_name)

    scraper, calls = _scraper_with_recorder(monkeypatch, _handler)

    with pytest.raises(BrowserRuntimeResourceExhausted):
        scraper.browse_url_html("https://www.airbnb.com/rooms/123456789")

    assert "runtime_reset" not in calls
    assert calls == ["browse_url_html"]
