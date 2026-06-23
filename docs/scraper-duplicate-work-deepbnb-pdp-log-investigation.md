# Scraper duplicate work and backend retry investigation

- Status: fixed
- Date recorded: 2026-06-21
- Area: `worker/scraper`

## Problem

The newest worker log showed repeated comp-set scraping for the same dates and
Deepbnb traffic even though the current scraper path should stay Playwright
backed.

For report `6fac408f-1fae-4457-b7c8-d0cfb990e07b`, the log contained:

- 198 comp search starts during the day-query phase.
- 398 `worker.scraper.deepbnb_backend` lines, including 198 search fetches and
  2 PDP fetches.
- Each daily date had duplicate offset `0` searches for both one-night and
  two-night comp collection before scanning deeper offsets.
- PDP HTML fallback logged `attempt 1/5`; one listing reached attempts 2/5
  through 5/5.

The first requested day also appears in the fixed-pool phase by design. That
one extra fixed-pool anchor is expected; the wasted work was the repeated
offset `0` daily search inside the deep-paging retry.

## Way to reproduce

1. Run a 7-night Mode A pricing report where the first search page has fewer
   than the target number of priced comps.
2. Search `worker/logs/worker.log` for:

```text
query_nights=1 offset=0
query_nights=2 offset=0
retrying deeper offsets=
deepbnb_backend
Playwright PDP HTML read attempt
```

Before the fix, the daily path fetched offset `0`, then retried deeper offsets
with a list that also started at `0`. Deepbnb was also enabled by default.

## Fixes

- `collect_search_comps()` now carries the first-page results into the deep
  paging pass and starts deep offsets at the next page.
- The pricing scrape path explicitly sets `USE_DEEPBNB_BACKEND=False`.
- `AirbnbClient` defaults `AIRBNB_USE_DEEPBNB_BACKEND` to disabled.
- Standalone live-price capture defaults `AIRBNB_USE_DEEPBNB_FOR_LIVE_PRICE`
  to disabled.
- Playwright PDP HTML fallback now reads the DOM once (`1/1`) instead of
  polling up to five times.

## Verification

Passed:

```bash
python -m py_compile worker/scraper/comp_collection.py worker/scraper/airbnb_client.py worker/scraper/playwright_scraper.py worker/scraper/price_estimator.py worker/scraper/target_extractor.py worker/tests/test_airbnb_client_deepbnb_defaults.py worker/tests/test_collect_search_comps_paging.py
python -m pytest worker/tests/test_airbnb_client_deepbnb_defaults.py worker/tests/test_collect_search_comps_paging.py::test_collect_search_comps_default_keeps_paging_until_priced_target worker/tests/test_collect_search_comps_paging.py::test_collect_search_comps_two_night_keeps_paging_until_priced_target worker/tests/test_playwright_browser_recovery.py -q
```
