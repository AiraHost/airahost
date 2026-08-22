"""_search_via_browser: what one browser search attempt may and may not return.

Drives the real coroutine against fake Playwright page/response objects. The
four ways a capture can fail — no matching response, JSON decode failure,
GraphQL auth error, valid payload — must stay distinguishable; the old handler
swallowed every exception, so all of them looked identical to "challenged".
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from worker.scraper.playwright_scraper import PlaywrightScraper
from worker.scraper.scraper_errors import AirbnbSearchBlocked, AirbnbSearchDegraded

SEARCH_HOST = "https://www.airbnb.com"
STAYSSEARCH_URL = f"{SEARCH_HOST}/api/v3/StaysSearch/abc123?operationName=StaysSearch"


def _valid_payload(count: int = 3) -> Dict[str, Any]:
    return {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "listingId": str(7000 + i),
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {"price": "$200 USD", "qualifier": "night"}
                                },
                            }
                            for i in range(count)
                        ]
                    }
                }
            }
        }
    }


def _challenged_dom(room_ids: List[str]) -> Dict[str, Any]:
    """A challenge page that still carries room anchors — the incident's shape."""
    return {
        "html_len": 460_000,
        "room_card_count": len(room_ids),
        "has_search_ui": False,
        "has_result_container": False,
        "visible_text": "Verify you are human. Complete the security check to continue.",
    }


def _healthy_dom(cards: int = 18) -> Dict[str, Any]:
    return {
        "html_len": 480_000,
        "room_card_count": cards,
        "has_search_ui": True,
        "has_result_container": True,
        "visible_text": "Over 1,000 homes in Belmont",
    }


def _shell_dom() -> Dict[str, Any]:
    return {
        "html_len": 15,
        "room_card_count": 0,
        "has_search_ui": False,
        "has_result_container": False,
        "visible_text": "",
    }


class _FakeRequest:
    def __init__(self, method: str = "POST", url: str = STAYSSEARCH_URL):
        self.method = method
        self.url = url
        self.post_data = json.dumps({"operationName": "StaysSearch", "variables": {}})

    async def all_headers(self):
        return {"content-type": "application/json"}


class _FakeResponse:
    def __init__(self, url: str, status: int = 200, payload=None, json_raises=False, method="POST"):
        self.url = url
        self.status = status
        self._payload = payload
        self._json_raises = json_raises
        self.request = _FakeRequest(method=method, url=url)

    async def json(self):
        if self._json_raises:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _FakeLocator:
    def __init__(self, texts: List[str]):
        self._texts = texts

    async def all_text_contents(self):
        return list(self._texts)


class _FakePage:
    def __init__(
        self,
        *,
        final_url: str,
        dom_signals,
        responses: Optional[List[_FakeResponse]] = None,
        deferred_state_scripts: Optional[List[str]] = None,
    ):
        self.url = final_url
        self._dom_signals = dom_signals
        self._responses = list(responses or [])
        self._deferred_state_scripts = list(deferred_state_scripts or [])
        self._handlers: List[Any] = []
        self.navigations: List[str] = []
        self.mouse = self
        self.evaluate_calls = 0

    # Playwright surface -------------------------------------------------
    def on(self, event, handler):
        if event == "response":
            self._handlers.append(handler)

    def locator(self, selector):
        if selector == 'script[id^="data-deferred-state"]':
            return _FakeLocator(self._deferred_state_scripts)
        return _FakeLocator([])

    async def goto(self, url, **kwargs):
        self.navigations.append(url)
        for resp in self._responses:
            for handler in self._handlers:
                handler(resp)
        self._responses = []
        return None

    async def content(self):
        return "<html></html>"

    async def evaluate(self, _js):
        self.evaluate_calls += 1
        signals = self._dom_signals
        return signals(self.evaluate_calls) if callable(signals) else signals

    async def wait_for_timeout(self, _ms):
        await asyncio.sleep(0)

    async def wheel(self, _x, _y):
        await asyncio.sleep(0)

    async def bring_to_front(self):
        return None

    async def close(self):
        return None


def _run_search(page: _FakePage, monkeypatch, overrides=None):
    scraper = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})

    async def _context():
        return object()

    async def _open(_ctx):
        return page

    async def _close(_page):
        return None

    async def _sync(_ctx):
        return None

    async def _nav(_page, *, url, label, wait_until, timeout):
        await page.goto(url)
        return {"requested_url": url, "final_url": page.url, "status": 200, "html": "<html></html>"}

    monkeypatch.setattr(scraper, "_get_thread_context", _context)
    monkeypatch.setattr(scraper, "_open_capped_page", _open)
    monkeypatch.setattr(scraper, "_close_capped_page", _close)
    monkeypatch.setattr(scraper, "_sync_session_cookies_into_context", lambda _ctx: None)
    monkeypatch.setattr(scraper, "_sync_context_cookies_into_session", _sync)
    monkeypatch.setattr(scraper, "_save_cached_state", lambda: None)
    monkeypatch.setattr(scraper, "_navigate_and_capture_html", _nav)
    return scraper, asyncio.run(scraper._search_via_browser(overrides or {"query": "Belmont"}))


# ── Required test 1: 18 room anchors on a challenged page ────────────────────

def test_challenged_page_with_18_room_anchors_raises_and_returns_no_payload(monkeypatch):
    room_ids = [str(8000 + i) for i in range(18)]
    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont--California/homes?checkin=2026-09-01",
        dom_signals=_challenged_dom(room_ids),
    )
    with pytest.raises(AirbnbSearchBlocked) as ctx:
        _run_search(page, monkeypatch)

    assert ctx.value.reason_code.startswith("visible_")
    # The anchors are present and are deliberately not turned into results.
    assert page._dom_signals["room_card_count"] == 18


def test_blocked_final_url_raises_even_though_navigation_returned_200(monkeypatch):
    page = _FakePage(final_url=f"{SEARCH_HOST}/login?redirect=/s/Belmont/homes", dom_signals=_shell_dom())
    with pytest.raises(AirbnbSearchBlocked) as ctx:
        _run_search(page, monkeypatch)
    assert ctx.value.reason_code == "final_url_login"


# ── Required test 2: the 15-byte shell ───────────────────────────────────────

def test_empty_shell_without_captured_stayssearch_is_degraded_not_blocked_or_empty(monkeypatch):
    page = _FakePage(final_url=f"{SEARCH_HOST}/s/Belmont/homes", dom_signals=_shell_dom())
    with pytest.raises(AirbnbSearchDegraded) as ctx:
        _run_search(page, monkeypatch)

    assert not isinstance(ctx.value, AirbnbSearchBlocked)
    assert ctx.value.reason_code == "no_stayssearch_response_hydrating_or_shell"


# ── Required test 28: capture-failure modes stay distinguishable ─────────────

def test_no_matching_response_is_reported_as_missing_traffic(monkeypatch):
    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont/homes",
        dom_signals=_healthy_dom(),
        responses=[_FakeResponse(f"{SEARCH_HOST}/api/v3/SomethingElse", payload={"data": {}})],
    )
    with pytest.raises(AirbnbSearchDegraded) as ctx:
        _run_search(page, monkeypatch)
    # Healthy-looking page, no API traffic: degraded, and explicitly not a challenge.
    assert ctx.value.reason_code == "no_stayssearch_response_healthy_search"


def test_json_decode_failure_is_reported_separately_from_a_challenge(monkeypatch):
    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont/homes",
        dom_signals=_healthy_dom(),
        responses=[_FakeResponse(STAYSSEARCH_URL, json_raises=True)],
    )
    with pytest.raises(AirbnbSearchDegraded) as ctx:
        _run_search(page, monkeypatch)
    assert ctx.value.reason_code == "stayssearch_json_decode_failed"


def test_non_dict_payload_is_reported_as_invalid_shape(monkeypatch):
    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont/homes",
        dom_signals=_healthy_dom(),
        responses=[_FakeResponse(STAYSSEARCH_URL, payload=["not", "a", "dict"])],
    )
    with pytest.raises(AirbnbSearchDegraded) as ctx:
        _run_search(page, monkeypatch)
    assert ctx.value.reason_code == "stayssearch_invalid_payload_shape"


def test_graphql_auth_error_response_is_blocked_not_degraded(monkeypatch):
    payload = {
        **_valid_payload(),
        "errors": [{"message": "Login required", "extensions": {"code": "UNAUTHORIZED"}}],
    }
    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont/homes",
        dom_signals=_healthy_dom(),
        responses=[_FakeResponse(STAYSSEARCH_URL, payload=payload)],
    )
    with pytest.raises(AirbnbSearchBlocked) as ctx:
        _run_search(page, monkeypatch)
    assert ctx.value.reason_code == "graphql_auth_error"


def test_valid_payload_is_returned_unchanged(monkeypatch):
    payload = _valid_payload(5)
    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont/homes",
        dom_signals=_healthy_dom(),
        responses=[_FakeResponse(STAYSSEARCH_URL, payload=payload)],
    )
    _scraper, (status, data) = _run_search(page, monkeypatch)
    assert status == 200
    assert data == payload


def test_rate_limited_stayssearch_is_reported_as_rate_limiting_not_a_block(monkeypatch):
    from worker.scraper.playwright_scraper import AirbnbRateLimited

    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont/homes",
        dom_signals=_healthy_dom(),
        responses=[_FakeResponse(STAYSSEARCH_URL, status=429, payload={"errors": []})],
    )
    with pytest.raises(AirbnbRateLimited):
        _run_search(page, monkeypatch)


# ── Hydration: a commit-time shell must not stick (required test 25) ─────────

def test_shell_at_first_classification_recovers_to_healthy_after_hydration(monkeypatch):
    # First DOM read sees the unhydrated shell; the second (after the fallback
    # navigation) sees a hydrated grid and a captured payload.
    payload = _valid_payload(4)
    responses = [_FakeResponse(STAYSSEARCH_URL, payload=payload)]
    reads: List[Dict[str, Any]] = []

    def _signals(call_index: int):
        signals = _shell_dom() if call_index == 1 else _healthy_dom()
        reads.append(signals)
        return signals

    page = _FakePage(final_url=f"{SEARCH_HOST}/s/Belmont/homes", dom_signals=_signals)
    # Deliver the payload only on the second navigation.
    page._responses = []

    async def _goto(url, **kwargs):
        page.navigations.append(url)
        if len(page.navigations) >= 2:
            for handler in page._handlers:
                for resp in responses:
                    handler(resp)
        return None

    page.goto = _goto

    _scraper, (status, data) = _run_search(page, monkeypatch)
    assert status == 200
    assert data == payload
    assert len(page.navigations) == 2  # primary + filter-change fallback
    assert reads[0]["html_len"] == 15  # the shell reading did not stick


def test_filter_change_fallback_navigation_carries_one_search_type(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    page = _FakePage(final_url=f"{SEARCH_HOST}/s/Belmont/homes", dom_signals=_healthy_dom())
    with pytest.raises(AirbnbSearchDegraded):
        _run_search(page, monkeypatch)

    assert len(page.navigations) == 2
    fallback_query = parse_qs(urlparse(page.navigations[1]).query)
    assert fallback_query["search_type"] == ["filter_change"]


def test_blocked_page_skips_the_fallback_navigation_entirely(monkeypatch):
    # No point nudging a page that is already proven to be a wall.
    page = _FakePage(final_url=f"{SEARCH_HOST}/s/Belmont/homes", dom_signals=_challenged_dom(["1", "2"]))
    with pytest.raises(AirbnbSearchBlocked):
        _run_search(page, monkeypatch)
    assert len(page.navigations) == 1


# ── Required test 14: the attempt log carries no HTML, query, or secrets ─────

def test_search_attempt_log_is_bounded_and_redacted(monkeypatch, caplog):
    payload = _valid_payload(2)
    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont--California/homes?checkin=2026-09-01&adults=4",
        dom_signals=_healthy_dom(),
        responses=[_FakeResponse(STAYSSEARCH_URL, payload=payload)],
    )
    with caplog.at_level("INFO", logger="worker.scraper.playwright_scraper"):
        _run_search(page, monkeypatch, overrides={"query": "Belmont", "itemsOffset": 40})

    attempts = [r for r in caplog.records if r.getMessage().startswith("[search_attempt]")]
    assert len(attempts) == 1, "exactly one summary event per search attempt"
    line = attempts[0].getMessage()
    assert "source=browser" in line
    assert "offset=40" in line
    assert "final_path=/s/Belmont--California/homes" in line
    # Nothing that could leak a page body, a user's dates, or a credential.
    assert "<html" not in line
    assert "checkin" not in line
    assert "adults=4" not in line
    assert "cookie" not in line.lower()

    # And no 4,000-character HTML preview anywhere in the attempt's logging.
    for record in caplog.records:
        assert "html_preview" not in record.getMessage()
        assert len(record.getMessage()) < 2000


# ── SSR-embedded StaysSearch recovery ─────────────────────────────────────
# Airbnb's search page frequently answers a first-page load without ever
# issuing a separate client-side StaysSearch XHR: results are server-rendered
# and shipped inline in a data-deferred-state-N script tag as a niobe GraphQL
# cache entry keyed "StaysSearch:{...variables...}". When no live XHR fires,
# that embedded state is the only place the results exist.


def _deferred_state_script(payload: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "niobeClientData": [
                [
                    "StaysSearch:{\"unrelatedCacheKeyVariables\":true}",
                    {"data": payload["data"], "variables": {}},
                ]
            ]
        }
    )


def test_embedded_ssr_state_is_used_when_no_live_xhr_fires(monkeypatch):
    payload = _valid_payload(6)
    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont/homes",
        dom_signals=_healthy_dom(),
        responses=[],  # no live XHR captured at all
        deferred_state_scripts=[_deferred_state_script(payload)],
    )
    _scraper, (status, data) = _run_search(page, monkeypatch)
    assert status == 200
    assert data["data"] == payload["data"]
    # Recovered from the primary navigation's embedded state — no need to
    # spend a second navigation on the filter_change nudge.
    assert len(page.navigations) == 1


def test_embedded_ssr_state_is_tried_again_after_filter_change_nudge(monkeypatch):
    # The primary page has no embedded state yet; only the fallback
    # navigation's rendered document carries it (e.g. hydration was still
    # settling on the first read).
    payload = _valid_payload(4)
    calls: List[int] = []

    def _scripts(call_index: int):
        calls.append(call_index)
        return [] if call_index == 1 else [_deferred_state_script(payload)]

    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont/homes",
        dom_signals=_healthy_dom(),
        responses=[],
    )
    page.locator_calls = 0
    original_locator = page.locator

    def _locator(selector):
        if selector == 'script[id^="data-deferred-state"]':
            page.locator_calls += 1
            return _FakeLocator(_scripts(page.locator_calls))
        return original_locator(selector)

    page.locator = _locator

    _scraper, (status, data) = _run_search(page, monkeypatch)
    assert status == 200
    assert data["data"] == payload["data"]
    assert len(page.navigations) == 2  # primary + filter-change fallback


def test_no_embedded_state_and_no_live_xhr_still_raises_degraded(monkeypatch):
    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont/homes",
        dom_signals=_healthy_dom(),
        responses=[],
        deferred_state_scripts=[],
    )
    with pytest.raises(AirbnbSearchDegraded) as ctx:
        _run_search(page, monkeypatch)
    assert ctx.value.reason_code == "no_stayssearch_response_healthy_search"


def test_malformed_embedded_state_is_ignored_not_raised(monkeypatch):
    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont/homes",
        dom_signals=_healthy_dom(),
        responses=[],
        deferred_state_scripts=["{not valid json", "{\"niobeClientData\": \"not-a-list\"}"],
    )
    with pytest.raises(AirbnbSearchDegraded):
        _run_search(page, monkeypatch)


# ── CDN static-asset URLs must never be mistaken for the StaysSearch API ───
# A response listener matching by bare substring ("stayssearch" in url) also
# matches the CDN-served route bundle
# (.../StaysSearchRoute/StaysSearchRoute.<hash>.js), which is never JSON and
# always fails decode — that used to surface as the misleading
# "stayssearch_json_decode_failed", masking the real problem (no API response
# at all). It must be ignored so the true, embedded-state-based diagnosis (or
# lack thereof) is what surfaces.


def test_cdn_route_bundle_response_is_not_mistaken_for_the_api_response(monkeypatch):
    bundle_url = (
        "https://a0.muscache.com/airbnb/static/packages/web/en-CA/frontend/"
        "stays-search/routes/StaysSearchRoute/StaysSearchRoute.846724c230.js"
    )
    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont/homes",
        dom_signals=_healthy_dom(),
        responses=[_FakeResponse(bundle_url, json_raises=True, method="GET")],
        deferred_state_scripts=[],
    )
    with pytest.raises(AirbnbSearchDegraded) as ctx:
        _run_search(page, monkeypatch)
    # Not misattributed to a decode failure on the (irrelevant) bundle asset.
    assert ctx.value.reason_code != "stayssearch_json_decode_failed"
    assert ctx.value.reason_code == "no_stayssearch_response_healthy_search"


def test_cdn_route_bundle_response_does_not_block_the_real_api_capture(monkeypatch):
    payload = _valid_payload(3)
    bundle_url = (
        "https://a0.muscache.com/airbnb/static/packages/web/en-CA/frontend/"
        "stays-search/routes/StaysSearchRoute/StaysSearchRoute.846724c230.js"
    )
    page = _FakePage(
        final_url=f"{SEARCH_HOST}/s/Belmont/homes",
        dom_signals=_healthy_dom(),
        responses=[
            _FakeResponse(bundle_url, json_raises=True, method="GET"),
            _FakeResponse(STAYSSEARCH_URL, payload=payload),
        ],
    )
    _scraper, (status, data) = _run_search(page, monkeypatch)
    assert status == 200
    assert data == payload
