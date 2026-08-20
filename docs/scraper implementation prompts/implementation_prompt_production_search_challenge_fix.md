# Implementation Prompt: Production StaysSearch Challenge / ID-Only DOM Fallback Fix

## Objective

Implement a complete, production-safe fix for the Airbnb comparable-search failure captured in `docs/scraper implementation prompts/log`. The scraper must never convert an Airbnb login/challenge response or an ID-only rendered page into a successful StaysSearch result. It must recover when possible, fail explicitly when recovery is exhausted, avoid contaminating comparable pools with incomplete rows, and emit useful bounded diagnostics.

The fix is complete only when the focused tests, the relevant worker test suite, and a controlled browser-path smoke test pass.

## Production evidence and root cause

The supplied log is an incomplete excerpt of a run on 2026-08-12, ending mid-navigation at line 1474. It does not contain a Python traceback, so do not claim that it proves why the process stopped. The operator inspected the actual browser and reports that it displayed a normal search page, not a login/challenge page. Treat that observation as important evidence. The log and code prove a scraper data-path failure:

- Multiple primary navigations returned HTTP 200 with `html_len=15`, which is not a usable hydrated search page.
- The browser fallback repeatedly logged `reached login/challenge page; StaysSearch is blocked`, even though its final URL remained a normal `/s/.../homes` URL and the captured 425–520 KB document began like Airbnb's normal guest SPA. This is consistent with the operator's visual observation and strongly indicates a false-positive classifier, not a real challenge.
- The code then logged `using rendered-DOM fallback parser ... extracted_listings=18` because no StaysSearch JSON was captured.
- Those 18 rows contained only listing IDs. The parser logged `search_location_missing_raw` with text samples containing only the ID. Empty amenities are allowed and are not evidence of failure.
- The collector consequently reported `ids=18 context=18 priced_rows=0`; the same incomplete candidates were repeated across dates and offsets, producing zero priced daily comps and unnecessary browser work.
- The file contains duplicated console/formatted copies of many events. Preserve this as a separate observability issue; do not mistake duplicates for distinct attempts.

The primary false-positive defect is `_page_looks_challenged()` in `worker/scraper/playwright_scraper.py`. It lowercases `page_url + the entire serialized HTML` and returns true if any substring includes `/login`, `/challenge`, `captcha`, and similar words. Normal Airbnb application HTML can contain a login navigation link, route name, script/application data, hidden dialog, or challenge-related bundle text while the visible page is a healthy search page. Therefore `/login` anywhere in hundreds of kilobytes of HTML is not evidence that the current page is a login page. The production log does not establish which marker matched because the classifier does not return or log the matched evidence.

There are additional defects in `_search_via_browser`:

- `_navigate_and_capture_html()` calls `page.content()` immediately after `wait_until="commit"`. At commit time the document can still be an unhydrated shell; the repeated `html_len=15` is consistent with `<html></html>`-like transient content, not proof of a challenge.
- After hydration waits and scrolling, challenge classification still uses the earlier `nav["html"]` snapshot instead of refreshing visible DOM state.
- `challenge_detected` is sticky across navigations. A false positive on an earlier snapshot is not cleared by later positive evidence of a healthy page.
- The fallback builds `fallback_url` by appending `&search_type=filter_change` when `search_type=` is already present, producing duplicate contradictory parameters (`AUTOSUGGEST` and `filter_change`) instead of replacing the value.
- When `captured_data is None`, the function returns the ID-only rendered fallback before acting on `challenge_reason`. This ordering remains unsafe for a genuinely confirmed challenge, but the incident should not be described as a confirmed challenge.

The missing StaysSearch capture also requires investigation rather than being assumed to mean authentication failure. Inspect whether the current response listener still matches Airbnb's actual operation URL/method, whether the relevant request occurs before/after listener attachment, whether `resp.json()` failures are silently swallowed, whether a captured GraphQL error is discarded, and whether response tasks are fully awaited. Add diagnostics that distinguish “no matching response observed,” “response observed but JSON decoding failed,” “GraphQL errors,” and “payload shape rejected.”

Finally, the production run occurred on 2026-08-12 but queried check-in/check-out windows from 2026-08-03 through 2026-08-10. These dates were already in the past. Investigate how a production report reached the scraper with past dates (queue delay, stale job, date derivation, or timezone handling). Airbnb may render an ordinary search page while ignoring or normalizing invalid past-date filters, resulting in listings without date-specific prices. This must be validated and fixed at the input/job boundary rather than mislabeled as a challenge.

The minimal payload itself contains only `listingId`. `parse_search_listing_context()` correctly creates default context entries, but defaults such as `is_available=True` make the payload look structurally valid even though it has no authoritative availability, price, location, capacity, or card metadata. `collect_search_comps()` currently treats any 2xx payload as a successful page (`page_ok=True`) and only later filters the candidates as unpriced/incomplete. For fixed-pool paths that permit unpriced fallback, these rows can survive even further. This is a correctness problem, not merely a lack of results.

## Required behavior

### 1. Replace the substring challenge detector with evidence-based page-state classification

Replace `_page_looks_challenged(content_html, page_url) -> bool` with a structured classifier that returns an outcome and evidence, for example `PageState(kind, reason_code, evidence)`. Do not classify from an arbitrary substring in full HTML.

- Treat an actual final URL whose normalized path is `/login`, `/challenge...`, `/checkpoint...`, or another audited Airbnb auth route as strong evidence. Parse the URL path; do not substring-match the full URL or its query.
- Inspect rendered, visible DOM state after hydration. Use specific stable selectors/roles and visible heading/body text for a blocking login modal, CAPTCHA, “verify you are human,” or security checkpoint. Exclude `script`, `style`, templates, comments, hidden elements, ordinary navigation links/buttons such as “Log in,” and serialized application data.
- Require either one authoritative signal (actual auth/challenge URL or explicit challenge response code) or multiple corroborating visible signals before classifying `blocked_or_challenged`.
- Positive healthy evidence must override incidental marker strings: normal search URL, visible search-result card containers, multiple valid room-card anchors, search heading/map/filter UI, or a valid StaysSearch response. Define precedence explicitly.
- Return/log a bounded `reason_code` such as `final_url_login`, `visible_captcha`, or `graphql_auth_error`. Never log a generic challenge claim without recording which safe signal caused it.
- Refresh the DOM/page state after the hydration wait and immediately before classification. Do not use the `wait_until="commit"` snapshot as the final classification input.
- Do not keep `challenge_detected` as a sticky boolean. Reclassify the current page after every navigation. An earlier blocked state may be cleared only by authoritative healthy evidence; record the transition.
- Add a distinct `hydrating_or_shell`, `healthy_search`, `valid_empty`, `blocked`, and `degraded_no_api` outcome. Missing StaysSearch traffic alone is not a challenge.
- Once the latest page is genuinely classified as login, CAPTCHA, challenge, consent/interstitial that prevents search hydration, or another blocked state, never build or return a 200 minimal payload from that page.
- Raise a dedicated typed exception such as `AirbnbSearchBlocked` (preferably in the existing scraper error module or alongside `AirbnbRateLimited`) carrying a short machine-readable reason. Do not include raw HTML, cookies, tokens, or the full URL query in the public exception message.
- Ensure both the primary navigation and `search_filter_change_fallback` update the final classification.
- Keep rate limiting distinguishable from authentication/challenge failures.
- Ensure response-listener tasks have settled before making the terminal decision, without allowing an unbounded wait.

Fix the fallback URL using URL parsing and query reconstruction so there is exactly one `search_type=filter_change`. Do not concatenate a second key.

Add bounded response-capture diagnostics. The response handler must not silently swallow all exceptions: record safe counters/reason codes for URL mismatch, method mismatch, JSON decode failure, GraphQL error, and invalid payload shape. Continue to avoid raw payload/HTML leakage.

### 2. Define and enforce a valid search-result contract

Add a single reusable validator/classifier for StaysSearch results and use it at every acceptance boundary: direct HTTP, captured browser JSON, rendered fallback (if retained), and collection.

At minimum classify outcomes as:

- `usable_results`: well-formed result list with enough per-card evidence for the requested workflow;
- `valid_empty`: authoritative, well-formed empty StaysSearch page with no GraphQL challenge/errors;
- `blocked_or_challenged`;
- `degraded_or_malformed`.

For price-dependent comparable collection, an ID-only row is not usable. A row must have an ID plus authoritative availability and a positive parseable price for the requested dates. Structural fields may remain optional where existing behavior permits them, but they must not be invented from defaults. Amenities are explicitly optional: `[]` is valid and must not cause a row, target listing, or report to fail validation.

Do not interpret absence of an `available` field as proof of availability. Change parser/default semantics to represent unknown availability as `None` (or an equivalent explicit state), then make collection reject unknown availability for priced/date-specific candidates. Preserve explicit unavailable/minimum-stay behavior.

An empty deep-offset direct response may continue to be treated as end-of-results only if it is structurally valid and error-free, preserving the behavior covered by `test_direct_search_empty_offset.py`. An empty first-page direct response may still trigger browser recovery.

### 3. Remove or harden the ID-only DOM fallback

Preferred solution: do not use the current minimal ID-only DOM payload as a successful search result for any pricing/comparable workflow.

If a rendered-DOM fallback is retained, replace it with card extraction that returns and validates, for every accepted row:

- listing ID;
- displayed price and its qualifier (nightly versus stay total);
- explicit availability evidence for the requested dates;
- title/location and other structural fields when present;
- provenance such as `source=rendered_dom` and a confidence/validation flag.

The extraction must be scoped to actual search-result card containers, not arbitrary `/rooms/` anchors anywhere in the document. It must reject login/challenge pages, stale/background cards, navigation links, wish-list content, and rows without a price. Do not fabricate a GraphQL-shaped payload unless all downstream semantics are preserved. A typed internal result model is preferable to hidden synthetic defaults.

If full card extraction is outside the intended scope, remove the fallback and fail explicitly. Correct failure is preferable to a successful report built from unverifiable data.

### 4. Recovery and retry policy

Integrate the typed failure with `search_listings_with_overrides` and the existing browser/session recovery path.

- On the first blocked/challenged attempt, close the affected page, invalidate stale direct-search/template state as appropriate, refresh browser-to-session cookies, and invoke the existing safe browser reset/auth recovery mechanism. Do not recursively retry without a strict bound.
- Retry at most the existing configured attempt budget, with short jitter/backoff. Do not multiply retries once per offset after the session is already known to be blocked.
- Add a per-client/session circuit breaker: after a definitive blocked state exhausts recovery, subsequent searches in the same report should fail fast with the same typed error until a successful auth/session health check resets it.
- Do not treat challenge failures as normal empty inventory or pagination exhaustion.
- At the report boundary, convert exhausted recovery into the project’s sanitized actionable scraper error (for example, “Airbnb search session is blocked; authentication refresh required”), while preserving the internal reason in structured logs. Do not return a zero-comp “successful” report for this condition.

### 5. Collector defense in depth

Update `worker/scraper/comp_collection.py` so malformed/degraded pages cannot contaminate the pool even if an upstream caller regresses.

- Validate `search_data` before setting `page_ok=True` or merging IDs/context.
- Track separate counters for `unknown_availability`, `missing_price`, `malformed_rows`, `blocked_pages`, and valid empty pages.
- Do not store ID-only candidates as `fallback_unpriced_comps`.
- Only preserve unpriced fallback candidates when they came from a valid authoritative payload and meet a documented non-pricing use case. Date-specific daily pricing must never accept them.
- Stop deeper pagination on authoritative empty results or a repeated valid page. Do not label a blocked/degraded response as “result set exhausted.”
- Ensure one failed page does not silently erase valid candidates already obtained from earlier pages, but a session-wide challenge must still propagate rather than masquerade as partial success unless product policy explicitly supports and labels partial reports.

### 6. Safe, non-duplicated observability

- Replace the 4,000-character HTML preview warning with bounded structured fields: classification, status, final host/path (query removed or redacted), HTML length, presence of known markers, attempt number, offset, and correlation/report ID.
- Never log raw page HTML, cookies, CSRF/auth tokens, or full URLs containing user/date/query data at warning level.
- Add one summary event per search attempt with `source`, `outcome`, `result_count`, `priced_count`, `offset`, and elapsed time.
- Investigate logging configuration and remove duplicate handler/propagation output so each event is emitted once in production. Add a focused logging configuration test if feasible.
- Keep enough information to distinguish `valid_empty`, `blocked`, `malformed`, and `usable` without inspecting raw HTML.

### 7. Treat an explicit 404 for the user's input listing as an immediate terminal input error

The repository already has `_preflight_listing_exists()` in `worker/scraper/price_estimator.py` and unit coverage in `worker/tests/test_listing_preflight.py`. It detects an HTTP 404 and narrowly recognized Airbnb not-found pages. However, `run_scrape()` currently converts that result into `([], _empty_transparent("scrape", "No such listing"))`. The job orchestrator interprets it as an ordinary no-results scrape, launches a second Playwright daily retry, and may then run the target-listing-only fallback. This violates the required behavior.

Implement the following end-to-end contract:

- Add a dedicated terminal exception, preferably a `ReportInputError` subtype such as `InputListingNotFound`, for confirmed missing user input listings. Keep it distinct from transient navigation, authentication/challenge, rate-limit, malformed-page, and date-unavailable failures.
- The canonical public message must be exactly `input listing not found` (matching case and wording). Internal structured diagnostics may contain the room ID, status, detection source, and correlation/report ID, but never raw HTML or secrets.
- On positive 404 evidence from `_preflight_listing_exists()`, raise the typed terminal error immediately. Do not return an empty transparent result.
- Propagate this error through `run_scrape`, benchmark/URL-mode orchestration, and `_execute_analysis`. Any broad `except Exception` used by benchmark mode must re-raise this terminal input error instead of falling back to criteria or URL scraping.
- The first confirmed 404 must prevent target extraction retries, comparable searches, daily queries, Playwright location-search retry, target-listing-only fallback, live-price capture, pool seeding, and report caching/writes other than marking the job failed.
- Call the existing `_fail`/`fail_job` path once with `error_message="input listing not found"`, a stable internal error code such as `input_listing_not_found`, and return immediately. The report must end in the normal failed/error state, not ready/success with an empty calendar.
- Apply this rule specifically to the user's input/target listing URL. A 404 from a comparable listing is not a terminal input error; reject that comparable and continue according to existing policy. If the product supports separate benchmark URLs, do not mislabel an optional secondary comparable/benchmark 404 as the user's input listing being absent.
- Keep 404 detection conservative. A literal HTTP 404 is authoritative. A 200 response is considered not-found only when it matches the existing narrow Airbnb not-found title/heading/“listing no longer available” evidence. Embedded JavaScript containing generic `404` text must not trigger the terminal error. Navigation exceptions, timeouts, challenge pages, and 5xx responses remain inconclusive/transient and must not be reported as `input listing not found`.
- Ensure all entry paths that can process the user's listing honor the same rule. In particular, do not let observation/cache reuse produce a fresh successful report after a newly performed canonical input preflight has confirmed 404. Avoid redundant probes within one job by retaining the preflight result in job-scoped state.

### 8. Reject or deliberately normalize past-date production jobs before scraping

Trace the report date lifecycle from API/database input through queue claim and worker execution. The incident ran on 2026-08-12 while requesting 2026-08-03 through 2026-08-10.

- Define the authoritative business timezone for deciding whether check-in is in the past; do not rely accidentally on the worker host timezone.
- At the earliest job boundary, validate `checkin < checkout` and reject a check-in earlier than the permitted current business date with a typed input/stale-job error and clear public message approved by existing product conventions.
- If nightly jobs are intentionally rolled forward rather than failed, make that policy explicit, deterministic, and tested; update both dates together while preserving stay length. Never silently send past dates to Airbnb.
- Record original dates, effective dates, business timezone, and reason code in structured debug metadata without exposing unrelated user data.
- Ensure stale jobs do not enter target extraction, StaysSearch, fallback pagination, or caching under an obsolete cache key.

## Files to inspect and likely modify

- `worker/scraper/playwright_scraper.py`
- `worker/scraper/scraper_errors.py`
- `worker/scraper/parsers.py`
- `worker/scraper/comp_collection.py`
- `worker/scraper/airbnb_client.py` if the typed result/error contract must cross the facade
- the report orchestration/error sanitization path in `worker/main.py` and/or `worker/core/errors.py`
- focused tests under `worker/tests/`

Keep the change scoped to search-result integrity and recovery. Do not relax capacity, geographic, availability, price-normalization, or comparable-scoring rules.

## Required automated tests

Add deterministic unit/regression tests with fake Playwright page/response objects and fixture payloads. No unit test may require live Airbnb access.

1. A page classified as challenged that contains 18 `/rooms/` anchors raises `AirbnbSearchBlocked` and never returns a minimal 200 payload.
2. A 15-byte/empty shell without captured StaysSearch is classified as degraded, not valid empty and not success.
3. A challenge on the first attempt invokes bounded recovery once; a healthy second attempt succeeds.
4. Two blocked attempts raise the typed terminal error and open no third browser page.
5. The session circuit breaker prevents a cascade of browser navigations across later dates/offsets and resets after a verified healthy/authenticated result.
6. A genuine, error-free empty deep-offset direct payload remains a valid end-of-results response.
7. A GraphQL error/challenge payload at a deep offset is not treated as valid empty.
8. Parser semantics preserve unknown availability as unknown; collection rejects unknown availability for price-dependent comps.
9. An ID-only synthetic/search payload produces no accepted priced comps and is not retained as unpriced fallback.
10. A normal captured StaysSearch payload with valid IDs, availability, and prices retains current parsing and price normalization behavior.
11. Explicit unavailable and minimum-stay rows retain current ghost-pricing protections.
12. Valid earlier pages plus a later authoritative empty page return the valid accumulated candidates and stop paging.
13. A blocked page increments `blocked_pages` and never increments valid-empty/exhausted counters.
14. Diagnostic logging contains no HTML preview, query string, cookie, CSRF token, or duplicate event emission.
15. An HTTP 404 for the user's input listing raises the terminal typed error and exposes exactly `input listing not found` to the report failure path.
16. A narrowly recognized Airbnb 200 “page not found”/“listing no longer available” page has the same terminal behavior.
17. After a confirmed input-listing 404, spies prove there are zero calls to target extraction, search, daily retry, target-only fallback, live-price capture, pool seeding, and cache write, and `fail_job` is called exactly once.
18. Benchmark-mode broad exception handling propagates the terminal input-listing error instead of falling back; optional comparable/secondary benchmark 404s retain their non-terminal policy.
19. A valid page containing generic `404` text in embedded JavaScript is not classified missing.
20. Timeout, challenge, 429, and 5xx preflight outcomes do not produce the `input listing not found` message.
21. Empty target or comparable amenities remain valid and do not affect payload usability, availability, or price acceptance.
22. A normal Airbnb search-page fixture containing `/login` links, login route data, and words such as `captcha` inside scripts is classified healthy when visible search-result evidence exists.
23. Actual final URLs under `/login`, `/challenge`, and `/checkpoint` classify blocked with the correct reason code.
24. A visible CAPTCHA/security checkpoint classifies blocked, while a hidden template or script containing the same text does not.
25. An initial `html_len=15` commit snapshot transitions to healthy after mocked hydration and is not permanently marked challenged.
26. A first-navigation false/blocked signal followed by authoritative healthy evidence does not remain sticky.
27. The filter-change fallback URL contains exactly one `search_type` parameter whose value is `filter_change`.
28. Response capture tests separately cover no matching response, JSON decode failure, GraphQL auth error, and valid payload; none is silently conflated with a visual challenge.
29. A production job whose check-in is before the authoritative business date is stopped or deliberately rolled forward according to the chosen policy before any Airbnb call.
30. Timezone-boundary tests cover the worker host date differing from the business date and prove deterministic validation.

Extend the most relevant existing suites rather than creating redundant test infrastructure, especially:

- `worker/tests/test_fetch_search_direct.py`
- `worker/tests/test_direct_search_empty_offset.py`
- `worker/tests/test_collect_search_comps_paging.py`
- `worker/tests/test_search_context_price_availability.py`
- `worker/tests/test_playwright_browser_recovery.py`
- `worker/tests/test_report_error_message_leak.py`
- `worker/tests/test_listing_preflight.py`
- add or extend an orchestration-level test around `_execute_analysis`/URL mode to prove terminal propagation and absence of fallbacks
- add a focused page-state classifier suite using sanitized healthy and challenged HTML/DOM fixtures
- add date-boundary/orchestration tests for stale queued jobs and the configured business timezone

## Integration and smoke testing

After unit tests pass:

1. Run the complete non-live worker test suite.
2. Run a controlled browser test with a healthy authenticated CDP session and confirm search results include priced rows and no DOM fallback is used.
3. Run a controlled challenged/logged-out fixture or isolated test profile and confirm bounded recovery followed by the sanitized terminal error; confirm no report is marked successful and no ID-only comps are persisted.
4. Run a sparse-market pagination case and confirm valid empty deep offsets stop quickly without browser fallback.
5. Inspect a produced log and confirm one event per action, redacted URLs, no raw HTML, and clear outcome counters.

Suggested commands (adapt paths/environment markers to repository conventions):

```powershell
python -m pytest worker/tests/test_fetch_search_direct.py worker/tests/test_direct_search_empty_offset.py worker/tests/test_collect_search_comps_paging.py worker/tests/test_search_context_price_availability.py worker/tests/test_playwright_browser_recovery.py worker/tests/test_report_error_message_leak.py worker/tests/test_listing_preflight.py -q
python -m pytest worker/tests -q -m "not live and not e2e"
```

Do not run live/E2E tests unless the required CDP/auth environment is explicitly configured. Record exact commands, pass/fail counts, skips, and any environment-dependent test not run.

## Acceptance criteria

- A known login/challenge page can never return HTTP 200 search success through the DOM fallback.
- A normal visible search page is never labeled challenged merely because full HTML contains `/login`, CAPTCHA vocabulary in scripts, or other dormant application routes.
- Challenge logs always include a safe, specific reason code and the classifier supports healthy recovery after hydration/navigation.
- ID-only rows can never enter a priced comparable pool or unpriced report fallback.
- Valid empty pages remain distinct from blocked/malformed pages and retain efficient deep-offset termination.
- Recovery is bounded and a session-wide block does not trigger a navigation storm across all report dates and offsets.
- Exhausted recovery produces a sanitized actionable report failure, not a misleading zero-comp success.
- Existing valid StaysSearch parsing, discounted/total price normalization, unavailable-date protection, capacity enforcement, geographic filtering, and pagination tests remain green.
- Production logs are bounded, redacted, non-duplicated, and expose outcome/counter data sufficient to diagnose the next incident.
- The implementation includes a brief code comment explaining the original failure ordering: challenge detected -> arbitrary room anchors extracted -> synthetic 200 payload.
- Empty amenities remain a supported, valid state and are never used alone to classify a response as degraded.
- A confirmed 404 for the user's input listing stops the job immediately and returns exactly `input listing not found`, with no scraper retries or pricing fallbacks.
- Past dates cannot reach Airbnb scraping silently; the selected reject-or-roll-forward policy is enforced before browser/direct requests and is timezone-tested.

## Deliverable

Provide the code changes, tests, and a short implementation report containing:

- the final root cause;
- the result/error contract chosen;
- recovery and circuit-breaker behavior;
- files changed;
- test commands and results;
- any intentionally deferred live validation or DOM-card extraction work.
