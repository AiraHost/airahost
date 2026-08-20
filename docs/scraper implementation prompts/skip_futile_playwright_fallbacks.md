# Implementation prompt: skip futile StaysSearch Playwright fallbacks

Implement a narrowly scoped change in the worker so that selected direct `StaysSearch` failure reasons return an empty search result immediately instead of escalating to Playwright.

## Evidence from `worker/logs/worker.jsonl`

The log was actively growing while analyzed on 2026-08-14, so treat the counts as a snapshot, not permanent constants. Correlating `fallback_selected` events and downstream Playwright events shows:

- `empty_result_set`: roughly 140+ fallbacks. Only 7 observed a `playwright_captured_json`; the overwhelming majority failed, principally with `stayssearch_json_decode_failed` (134 Playwright failures in the snapshot), with a few browser-runtime/target-closed failures as well. This is also semantically an authoritative, error-free empty direct response.
- `direct_http_failed`: 10 observed fallbacks, all in the sampled failure burst; none produced a successful Playwright capture and the browser path failed with `TargetClosedError`/`BrowserRuntimeUnavailable`.
- Do **not** add `graphql_auth_error` to the skip set based on this log. Its eight fallback traces did not contain a conclusive downstream outcome in the snapshot.
- Do **not** add `direct_search_unavailable`: several of those browser attempts captured JSON successfully.
- Do **not** generalize this change to PDP. The log has only one direct-PDP transport failure cascade followed by two browser failures, which is insufficient evidence that a PDP fallback reason “often” fails.

## Required behavior

1. Inspect `worker/scraper/playwright_scraper.py`, especially `_try_direct_search()` and `search_listings_with_overrides()`, plus `worker/scraper/search_result_contract.py`, before choosing the smallest clean implementation.
2. Introduce an explicit, centralized skip-fallback policy for exactly these StaysSearch reasons:
   - `empty_result_set`
   - `direct_http_failed`
3. When the direct result is a valid empty payload (`empty_result_set`), return that original `(status_code, payload)` for **all offsets**, including `itemsOffset == 0`. Remove the current first-page-only Playwright escalation. Do not synthesize a different payload when the authoritative direct empty payload is available.
4. When the direct attempt returns no response and the reason is `direct_http_failed`, return a canonical empty StaysSearch result immediately. Use an existing result-builder/contract helper if one exists. If none exists, add one small helper whose payload is accepted by the existing search parsing/counting consumers. Do not return bare `None`, and do not invent fake listings or pagination metadata.
5. For either skipped reason, do not call `_run_browser_search()`, do not emit `fallback_selected`, and do not consume browser-navigation admission or retry budget.
6. Preserve the existing behavior for every other reason, including anti-bot/auth responses, malformed/degraded payloads other than the two named reasons, `direct_search_unavailable`, and unexpected exceptions.
7. Preserve observability. Emit a structured event indicating that browser fallback was intentionally skipped and an empty result returned. Prefer an existing event type if its semantics fit; otherwise add a clearly named event such as `fallback_skipped`. Include at least `operation=stays_search`, `source=direct_json`, the reason code, and an empty result count. Do not falsely emit `direct_http_succeeded` for `direct_http_failed`.
8. Keep PDP behavior unchanged.

## Tests

Add or update focused tests proving:

- A valid first-page direct empty payload returns unchanged and never invokes the browser path.
- A valid deeper-page direct empty payload still returns unchanged and never invokes the browser path.
- `direct_http_failed` returns the canonical empty result and never invokes the browser path.
- Neither skip case emits `fallback_selected` or consumes browser retry/admission work; the intentional-skip event contains the correct reason.
- `graphql_auth_error` still falls back to Playwright.
- `direct_search_unavailable` still falls back to Playwright.
- A representative PDP direct failure retains its existing fallback chain.
- Existing search-result contract and pagination tests continue to pass.

Run the narrow scraper/search tests first, then the complete worker test suite if practical. Report the files changed, exact result shape chosen for the synthesized empty result, tests run, and any compatibility risk you found.
