"""Tests for the direct-HTTP StaysSearch replay path (fetch_search_direct)."""

import copy
import unittest
from typing import Any, Dict, List

from worker.scraper.parsers import parse_search_response
from worker.scraper.playwright_scraper import PUBLIC_AIRBNB_API_KEY, PlaywrightScraper


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
        scraper.hardcoded_search_req = None
        self.assertIsNone(scraper.fetch_search_direct({"checkin": "2026-06-27"}))

    def test_hardcoded_template_used_when_no_capture(self):
        # Browser-free cold start: no live capture, fall back to the hardcoded
        # template so comparable search still runs (stl-scraper technique).
        scraper = _new_scraper()
        scraper.captured_search_req = None
        scraper.hardcoded_search_req = _make_template()
        scraper.session = _FakeSession([_FakeResponse(200, _ok_payload(["555"]))])
        result = scraper.fetch_search_direct({"checkin": "2026-06-27", "checkout": "2026-06-28"})
        self.assertIsNotNone(result)
        _status, data = result
        self.assertEqual(parse_search_response(data), ["555"])

    def test_template_generalized_strips_pinned_location_and_realigns_cursor(self):
        # A recorded template carries the captured search's placeId/session/flex
        # params and a page-N cursor. Replaying for a different location/offset
        # must strip those (placeId wins over query at Airbnb) and rebuild the
        # cursor, else every replay echoes the captured city/page.
        scraper = _new_scraper()
        tmpl = _make_template()
        sr = tmpl["post_data"]["variables"]["staysSearchRequest"]
        sr["cursor"] = "OLD_PAGE_2_CURSOR"
        sr["rawParams"].extend(
            [
                {"filterName": "placeId", "filterValues": ["AUSTIN_PLACE_ID"]},
                {"filterName": "federatedSearchSessionId", "filterValues": ["sess-123"]},
                {"filterName": "flexibleTripLengths", "filterValues": ["one_week"]},
            ]
        )
        scraper.captured_search_req = tmpl
        scraper.session = _FakeSession(
            [_FakeResponse(200, _ok_payload(["1"])), _FakeResponse(200, _ok_payload(["2"]))]
        )

        # First page: pinned params dropped, cursor reset to None.
        scraper.fetch_search_direct({"query": "Seattle, Washington"})
        sent = scraper.session.calls[0]["json"]["variables"]["staysSearchRequest"]
        by_name = {p["filterName"]: p["filterValues"] for p in sent["rawParams"]}
        self.assertNotIn("placeId", by_name)
        self.assertNotIn("federatedSearchSessionId", by_name)
        self.assertNotIn("flexibleTripLengths", by_name)
        self.assertEqual(by_name["query"], ["Seattle, Washington"])
        self.assertIsNone(sent["cursor"])

        # Deep offset: cursor encodes the requested items_offset.
        scraper.fetch_search_direct({"query": "Seattle, Washington", "itemsOffset": 40})
        sent2 = scraper.session.calls[1]["json"]["variables"]["staysSearchRequest"]
        import base64
        import json as _json

        decoded = _json.loads(base64.b64decode(sent2["cursor"]))
        self.assertEqual(decoded["items_offset"], 40)

    def test_public_api_key_header_forced(self):
        scraper = _new_scraper()
        # Template carries a stale/wrong api key; the sent request must override it.
        template = _make_template()
        template["headers"]["x-airbnb-api-key"] = "stale-key"
        scraper.captured_search_req = template
        scraper.session = _FakeSession([_FakeResponse(200, _ok_payload(["111"]))])
        scraper.fetch_search_direct({"checkin": "2026-06-27"})
        sent_headers = scraper.session.calls[0]["headers"]
        self.assertEqual(sent_headers["x-airbnb-api-key"], PUBLIC_AIRBNB_API_KEY)

    def test_try_direct_search_falls_back_on_challenge(self):
        scraper = _new_scraper()
        scraper.captured_search_req = _make_template()
        challenge = {"errors": [{"extensions": {"code": "CAPTCHA"}}]}
        scraper.session = _FakeSession([_FakeResponse(200, challenge)])
        # Challenge-like payload -> _try_direct_search returns None (browser fallback).
        self.assertIsNone(scraper._try_direct_search({"checkin": "2026-06-27"}))

    def test_try_direct_search_trusts_empty_result_instead_of_falling_back(self):
        scraper = _new_scraper()
        scraper.captured_search_req = _make_template()
        empty_payload = _ok_payload([])
        scraper.session = _FakeSession([_FakeResponse(200, empty_payload)])
        result = scraper._try_direct_search({"checkin": "2026-06-27"})
        self.assertEqual(result, (200, empty_payload))

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
