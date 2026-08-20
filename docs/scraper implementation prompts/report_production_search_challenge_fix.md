# Implementation Report: Production StaysSearch Challenge / ID-Only DOM Fallback Fix

Date: 2026-08-13

## 1. Root cause

Three independent defects compounded into the incident.

**A. False-positive challenge classification.** `_page_looks_challenged(html, url)`
lowercased `page_url + the entire serialized HTML` and returned `True` if any of
`/login`, `/challenge`, `/checkpoint`, `captcha`, `security check`, ... appeared
anywhere in it. Airbnb's ordinary guest SPA carries a "Log in" nav link, a
serialized route table naming `/login` and `/challenge`, and captcha vocabulary
inside script bundles. Every healthy 400–500 KB search page therefore matched.
The classifier returned a bare bool, so the log could not say which marker fired.

**B. The ID-only rendered-DOM fallback turned that verdict into fake success.**
When no StaysSearch response was captured, `_search_via_browser` scraped every
`/rooms/<id>` anchor in the document and returned them as an HTTP 200
StaysSearch payload. Ordering:

> challenge detected → arbitrary room anchors extracted → synthetic 200 payload

Those rows held only `listingId`. `parse_search_listing_context()` filled the
rest from defaults — including `is_available=True` — so the payload looked
structurally valid. `collect_search_comps()` set `page_ok=True` for any 2xx, and
the rows survived into `fallback_unpriced_comps`. Hence
`ids=18 context=18 priced_rows=0`, repeated for every date and offset.

**C. Contributing defects.** `page.content()` was read immediately after
`wait_until="commit"` (the repeated `html_len=15` — an unhydrated shell, not a
challenge); classification then reused that stale snapshot; `challenge_detected`
was sticky across navigations; the filter-change fallback appended a second
`search_type`, sending Airbnb both `AUTOSUGGEST` and `filter_change`; the
response handler swallowed every exception, so "no traffic", "decode failed",
"auth error", and "bad shape" were indistinguishable.

**Two further findings the prompt asked to investigate:**

- *Duplicated log events*: `worker/core/auto_price_assignment.py` called
  `logging.basicConfig()` at import time. `main.py` imports it before installing
  its own root handlers, so basicConfig's stderr handler was added first and
  every record was emitted twice — once unformatted, once formatted.
- *Past dates*: the run occurred 2026-08-12 but queried 2026-08-03 → 2026-08-10.
  Nothing validated the window. Airbnb normalizes past-date filters away and
  still renders a search page, so the scrape returned listings with no
  date-specific prices — indistinguishable downstream from a sparse market.

The log does **not** prove why the process stopped, and the operator's
observation of a normal search page is consistent with (A): this was a
false-positive classifier plus a fallback that manufactured success, not a
confirmed challenge.

## 2. Result / error contract

New module `worker/scraper/page_state.py` — `PageState(kind, reason_code, evidence)`
over kinds `healthy_search`, `valid_empty`, `blocked_or_challenged`,
`hydrating_or_shell`, `degraded_no_api`. Explicit precedence:

1. Authoritative block — parsed URL **path** on an audited auth route
   (`/login`, `/challenge`, `/checkpoint`, ...), HTTP 401/403, GraphQL auth error.
2. Authoritative health — a parsed StaysSearch payload.
3. Rendered results grid — cards inside real result containers.
4. Corroborated block — two independent *visible* signals, or one on a page with
   no search chrome. Never a single incidental marker.
5. Weaker health — cards plus search chrome or a search URL path.
6. Shell / degraded.

Visible-text extraction strips `script`, `style`, `noscript`, `template`,
comments, and hidden elements; the live path (`collect_dom_signals`) reads
computed visibility via a `TreeWalker` so a hidden login modal contributes
nothing. Missing StaysSearch traffic alone is never a challenge.

New module `worker/scraper/search_result_contract.py` — one classifier
(`usable_results` / `valid_empty` / `blocked_or_challenged` /
`degraded_or_malformed`) applied at direct HTTP, captured browser JSON, and
collection. `row_is_priced()` is the single pricing rule: explicit
`is_available is True` **plus** a positive parsed price. Amenities are never
consulted; `[]` stays valid.

New exceptions:

| Type | Module | Meaning |
|---|---|---|
| `AirbnbSearchBlocked(reason_code)` | `scraper_errors` | session logged out/challenged; carries only a bounded reason code |
| `AirbnbSearchDegraded(reason_code)` | `scraper_errors` | no usable result; retryable, explicitly not a block |
| `InputListingNotFound` | `core.errors` | confirmed 404 on the user's listing; public message fixed at `input listing not found` |
| `StaleReportJobError` | `core.errors` | check-in already past in business time |

`AirbnbRateLimited` stays distinct and retryable; a 429/503 body is no longer
captured as a payload.

Parser semantics changed: `is_available` now defaults to `None` (unknown).
Price parsing is gated on "not explicitly unavailable" so the parser still
records what the card displayed; enforcing availability is collection's job.

## 3. Recovery and circuit breaker

`_run_browser_search()` is shared by both search entrypoints:

- Attempt budget 2 (`SEARCH_ATTEMPT_BUDGET`). On the first `AirbnbSearchBlocked`,
  `_recover_blocked_search_session()` drops the captured direct-search template,
  re-warms browser cookies, refreshes the session, and resets the CDP
  connection; then one retry with jitter.
- A second block trips a **session circuit breaker**
  (`_search_blocked_reason`). Every later search in the report — across all
  dates and offsets — fails fast with the same typed error and opens no browser
  page. Any successful search (direct or browser) clears it.
- A blocked page never proceeds to the filter-change nudge.
- `run_day_query()` re-raises `AirbnbSearchBlocked` instead of degrading it to a
  per-day error, so the block reaches the report boundary.
- `main.py` converts it to `"Airbnb search session is blocked; authentication
  refresh required."` with `reasonCode` in structured debug — a failed report,
  never a zero-comp success.

## 4. Files changed

**New**
- `worker/scraper/page_state.py` — evidence-based page classifier
- `worker/scraper/search_result_contract.py` — payload/row contract
- `worker/core/report_dates.py` — business-timezone date policy

**Modified**
- `worker/scraper/playwright_scraper.py` — `_page_looks_challenged` replaced by
  `_classify_html_page_state`; `_search_via_browser` rewritten (post-hydration
  non-sticky classification, bounded response settling, capture diagnostics,
  fixed fallback URL, no synthetic payload); ID-only DOM fallback helpers
  deleted with an explanatory comment; shared recovery/breaker path;
  `_try_direct_search` uses the shared contract
- `worker/scraper/scraper_errors.py` — `AirbnbSearchBlocked`, `AirbnbSearchDegraded`
- `worker/scraper/parsers.py` — unknown availability is `None`
- `worker/scraper/comp_collection.py` — payload validated before `page_ok`;
  unknown availability rejected for priced comps; unpriced fallback drawn only
  from price-missing rows with real structure; counters for
  `blocked_pages` / `malformed_pages` / `valid_empty_pages` /
  `unknown_availability` / `missing_price` / `malformed_rows`
- `worker/scraper/day_query.py` — blocked sessions propagate
- `worker/scraper/price_estimator.py` — confirmed input-listing 404 raises
  `InputListingNotFound` instead of returning an empty transparent result
- `worker/core/errors.py` — `InputListingNotFound`, `StaleReportJobError`
- `worker/core/auto_price_assignment.py` — removed `logging.basicConfig()`
- `worker/main.py` — date gate before cache lookup and any Airbnb call;
  idempotent, tagged log handlers; `_fail_input_listing_not_found` and
  `_fail_search_blocked`; benchmark mode re-raises the terminal input error

**Tests** — 5 new files, 3 extended (see §6).

## 5. Date policy

Reject, per your decision. `validate_report_dates()` runs at the top of
`_execute_analysis`, before the cache lookup and before any scrape:
check-in must not precede today's date in `REPORT_BUSINESS_TIMEZONE`
(default `UTC`, never the worker host's local zone). `REPORT_STALE_GRACE_DAYS`
(default 0) exists for deployments serving markets west of their business
timezone. Original dates, business timezone, business today, grace, and reason
code go into structured debug. Nightly jobs are unaffected: the scheduler
derives `startDate` as local *tomorrow* in the listing's timezone.

## 6. Tests

New: `test_page_state_classifier.py` (17), `test_search_result_contract.py` (11),
`test_browser_search_blocked_recovery.py` (10), `test_search_via_browser_capture.py` (13),
`test_collect_search_comps_integrity.py` (12), `test_report_date_policy.py` (11),
`test_input_listing_not_found_orchestration.py` (10), `test_worker_logging_config.py` (3).

Extended: `test_listing_preflight.py`, `test_search_context_price_availability.py`
(two availability assertions updated to the new unknown semantics).

All run against fake Playwright page/response objects and fixture payloads; none
requires live Airbnb access.

```
python -m pytest worker/tests -q -k "not live and not e2e"
  -> 742 passed, 6 failed, 28 deselected
```

### The 6 failures are pre-existing and unrelated

All six fail identically on the unmodified tree — verified by reverting the
parser edits and re-running, and by confirming none touches modified code:

- `test_search_context_price_availability.py::test_parse_pdp_response_prefers_nightly_breakdown_over_fee_inclusive_primary_total`
- `test_search_context_price_availability.py::test_parse_pdp_response_supports_amount_before_nights_breakdown_shape`
- `test_search_context_price_availability.py::test_parse_pdp_response_prefers_one_night_price_after_discount_over_primary_price`
- `test_price_extraction.py::test_client_pdp_extraction_uses_price_after_discount_not_primary_price`

  One PDP defect behind all four: `parse_pdp_response` returns
  `primaryLine.price` (769.0) where the test expects the
  `explanationData` "Price after discount" amount (684.0). PDP price
  extraction, not search-result integrity.

- `test_playwright_browser_recovery.py::test_pdp_reconnects_once_after_browser_context_closes`
- `test_playwright_browser_recovery.py::test_pdp_does_not_retry_non_browser_failures`

  Both assert an exact `_run_async` op sequence that no longer matches:
  `get_listing_details` now routes through `_try_direct_listing_details`, which
  emits a `warm_session_cookies` op first. `_run_async` is monkeypatched in
  these tests, so no code I changed executes in them.

These were left alone: fixing PDP price extraction is outside the stated scope
("keep the change scoped to search-result integrity and recovery"), and both
areas deserve their own change with their own reasoning.

## 7. Deferred

- **Live browser smoke tests (integration items 2, 3, 5)** — not run. They need
  an authenticated Chrome CDP session on :9222 and a controlled logged-out
  profile, which is not configured here. Unit coverage stands in: blocked pages
  raise and return no payload, recovery is bounded, the breaker prevents a
  navigation storm, and the attempt log is asserted to contain no HTML, query
  string, cookie, or duplicate event.
- **Sparse-market pagination (item 4)** — covered deterministically by
  `test_valid_first_page_plus_authoritative_empty_page_returns_candidates_and_stops`
  and the existing `test_direct_search_empty_offset.py`; not re-run live.
- **Full rendered-DOM card extraction** — deliberately not built. The prompt's
  preferred option was taken: the ID-only fallback is removed and the search
  fails explicitly. Correct failure beats a report built from unverifiable data.
