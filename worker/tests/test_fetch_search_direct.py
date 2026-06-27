"""Tests for the direct-HTTP StaysSearch replay path (fetch_search_direct)."""

import copy
import unittest
from typing import Any, Dict, List

from worker.scraper.parsers import parse_search_response
from worker.scraper.playwright_scraper import PlaywrightScraper


def _make_template() -> Dict[str, Any]:
    return {
        "url": "https://www.airbnb.com/api/v3/StaysSearch/abc123?operationName=StaysSearch",
        "method": "POST",
        "headers": {"content-type": "application/json", "x-airbnb-api-key": "key"},
        "post_data": {
            "operationName": "StaysSearch",
            "variables": {
                "staysSearchRequest": {
                    "rawParams": [
                        {"filterName": "query", "filterValues": ["Old, Place"]},
                        {"filterName": "checkin", "filterValues": ["2026-01-01"]},
                        {"filterName": "checkout", "filterValues": ["2026-01-02"]},
                        {"filterName": "adults", "filterValues": ["1"]},
                    ]
                },
            },
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "deadbeef"}},
        },
    }


def _ok_payload(listing_ids: List[str]) -> Dict[str, Any]:
    return {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [{"listingId": lid} for lid in listing_ids]
                    }
                }
            }
        }
    }


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": copy.deepcopy(json)})
        return self._responses.pop(0)


def _new_scraper() -> PlaywrightScraper:
    return PlaywrightScraper({"USE_HARDCODED_STAYSPDP_TEMPLATE": "0"})


class FetchSearchDirectTest(unittest.TestCase):
    def test_overrides_mutate_raw_params(self):
        scraper = _new_scraper()
        scraper.captured_search_req = _make_template()
        scraper.session = _FakeSession([_FakeResponse(200, _ok_payload(["111", "222"]))])

        result = scraper.fetch_search_direct(
            {
                "query": "Redwood City, California",
                "checkin": "2026-06-27",
                "checkout": "2026-06-28",
                "adults": 5,
                "itemsOffset": 25,
            }
        )
        self.assertIsNotNone(result)
        status, data = result
        self.assertEqual(status, 200)
        self.assertEqual(parse_search_response(data), ["111", "222"])

        sent = scraper.session.calls[0]["json"]
        raw = sent["variables"]["staysSearchRequest"]["rawParams"]
        by_name = {p["filterName"]: p["filterValues"] for p in raw}
        self.assertEqual(by_name["query"], ["Redwood City, California"])
        self.assertEqual(by_name["checkin"], ["2026-06-27"])
        self.assertEqual(by_name["checkout"], ["2026-06-28"])
        self.assertEqual(by_name["adults"], ["5"])
        self.assertEqual(by_name["itemsOffset"], ["25"])

    def test_retries_on_503_then_succeeds(self):
        scraper = _new_scraper()
        scraper.captured_search_req = _make_template()
        scraper.session = _FakeSession(
            [_FakeResponse(503), _FakeResponse(200, _ok_payload(["999"]))]
        )
        result = scraper.fetch_search_direct({"checkin": "2026-06-27", "checkout": "2026-06-28"})
        self.assertIsNotNone(result)
        _status, data = result
        self.assertEqual(parse_search_response(data), ["999"])
        self.assertEqual(len(scraper.session.calls), 2)

    def test_no_template_returns_none(self):
        scraper = _new_scraper()
        scraper.captured_search_req = None
        self.assertIsNone(scraper.fetch_search_direct({"checkin": "2026-06-27"}))

    def test_try_direct_search_falls_back_on_challenge(self):
        scraper = _new_scraper()
        scraper.captured_search_req = _make_template()
        challenge = {"errors": [{"extensions": {"code": "CAPTCHA"}}]}
        scraper.session = _FakeSession([_FakeResponse(200, challenge)])
        # Challenge-like payload -> _try_direct_search returns None (browser fallback).
        self.assertIsNone(scraper._try_direct_search({"checkin": "2026-06-27"}))

    def test_try_direct_search_falls_back_on_empty(self):
        scraper = _new_scraper()
        scraper.captured_search_req = _make_template()
        scraper.session = _FakeSession([_FakeResponse(200, _ok_payload([]))])
        self.assertIsNone(scraper._try_direct_search({"checkin": "2026-06-27"}))

    def test_try_direct_search_returns_results(self):
        scraper = _new_scraper()
        scraper.captured_search_req = _make_template()
        scraper.session = _FakeSession([_FakeResponse(200, _ok_payload(["111"]))])
        result = scraper._try_direct_search({"checkin": "2026-06-27"})
        self.assertIsNotNone(result)
        _status, data = result
        self.assertEqual(parse_search_response(data), ["111"])


if __name__ == "__main__":
    unittest.main()
