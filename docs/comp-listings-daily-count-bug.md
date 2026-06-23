# Daily comparable listings count bug

- Status: fixed
- Date recorded: 2026-06-20
- Area: `worker/scraper`
- Files changed:
  - `worker/scraper/comp_collection.py`
  - `worker/tests/test_collect_search_comps_paging.py`

## Problem

Daily comparable listing collection could return only one priced comp, and
sometimes zero, even when later Airbnb search offsets had enough inventory.

The daily report path asks for a 20-card comparable listings display, but
`collect_search_comps()` returned as soon as any priced listing was found on the
first search page. If offset `0` had one priced listing, the scraper never
queried deeper offsets for that date/window, so downstream filtering and
display often had only one candidate to work with.

## Way to reproduce

Unit-level reproduction:

1. Use a fake Airbnb search client where offset `0` returns one priced listing.
2. Make offset `20` return nineteen more priced listings.
3. Call `collect_search_comps(..., max_cards=20, max_scroll_rounds=1)` without
   explicit `page_offsets`.

Before the fix, the helper returned after offset `0` with one comp. After the
fix, it retries the bounded deeper offsets and returns 20 comps when those
offsets contain enough priced inventory.

Regression tests:

```bash
python -m pytest worker/tests/test_collect_search_comps_paging.py::test_collect_search_comps_default_keeps_paging_until_priced_target worker/tests/test_collect_search_comps_paging.py::test_collect_search_comps_two_night_keeps_paging_until_priced_target -q
```

## Fixes

- Added a bounded priced-comp target in `collect_search_comps()`.
- For default daily paging, the scraper now continues past a sparse first page
  until it either reaches the requested `max_cards` priced comps or exhausts the
  configured offset budget.
- Applied the same behavior to one-night and two-night daily collection so the
  separate daily pools both avoid early return.
- Kept explicit `page_offsets` behavior unchanged: callers that pass exact
  offsets still get the merged result from those offsets.

## Verification

Passed:

```bash
python -m pytest worker/tests/test_collect_search_comps_paging.py::test_collect_search_comps_default_keeps_paging_until_priced_target worker/tests/test_collect_search_comps_paging.py::test_collect_search_comps_two_night_keeps_paging_until_priced_target -q
python -m pytest worker/tests/test_day_query_comp_price_revalidation.py -q
python -m py_compile worker/scraper/comp_collection.py worker/scraper/day_query.py
```

Known unrelated failure surfaced while running the whole paging test file:

```text
worker/tests/test_collect_search_comps_paging.py::test_collect_search_comps_ceils_decimal_two_night_total_before_deriving_nightly
expected 555.5, current code returned 555.18
```

That failure is in decimal two-night total normalization and was not changed by
this comp-count fix.
