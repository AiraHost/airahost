"""
_try_direct_search must trust a well-formed empty page at ANY pagination
offset, including the first page, instead of falling back to a browser
navigation. Airbnb re-serves page 1 to the browser for out-of-range offsets,
so a deep-offset fallback costs a multi-second goto and yields no new
listings — multiplied across the deep-offset sweep this dominated report
generation time. Log evidence (docs/scraper implementation prompts/
skip_futile_playwright_fallbacks.md) showed the same is true for a first-page
empty result: the overwhelming majority of browser escalations for
`empty_result_set` failed outright, and the direct empty payload is itself an
authoritative, error-free response.
"""

import worker.scraper.playwright_scraper as pws


_EMPTY_PAYLOAD = {
    "data": {
        "presentation": {
            "staysSearch": {"results": {"searchResults": []}}
        }
    }
}


def _make_scraper(payload, status=200):
    scraper = object.__new__(pws.PlaywrightScraper)
    scraper.captured_search_req = {"url": "https://x", "post_data": {}}
    scraper.hardcoded_search_req = None
    scraper.fetch_search_direct = lambda overrides: (status, payload)
    return scraper


def test_empty_results_at_deep_offset_are_trusted_as_end_of_results():
    scraper = _make_scraper(_EMPTY_PAYLOAD)
    result = scraper._try_direct_search({"itemsOffset": 40})
    assert result == (200, _EMPTY_PAYLOAD)


def test_empty_results_on_first_page_are_also_trusted():
    scraper = _make_scraper(_EMPTY_PAYLOAD)
    assert scraper._try_direct_search({}) == (200, _EMPTY_PAYLOAD)
    assert scraper._try_direct_search({"itemsOffset": 0}) == (200, _EMPTY_PAYLOAD)


def test_empty_results_with_graphql_errors_fall_back_even_at_deep_offset():
    payload = dict(_EMPTY_PAYLOAD)
    payload["errors"] = [{"message": "something went wrong"}]
    scraper = _make_scraper(payload)
    assert scraper._try_direct_search({"itemsOffset": 40}) is None
