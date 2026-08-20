# Report — Airbnb 503 mitigation and end-to-end scraper observability

Implements `implementation_prompt_airbnb_503_and_centralized_logging.md`.

---

## 1. Assumptions

Stated plainly, because several of them shaped the design:

1. **No live calibration was authorized or run.** Every default below is a
   conservative engineering choice, not a measurement. Nothing in this work
   claims a safe Airbnb rate limit. §4 says what must be validated in the
   deployment environment.
2. **Deployment topology is not declared anywhere in the repo.** The admission
   policy is therefore process-local with an explicit, documented static
   partition for multi-instance deployments (§5). A shared DB-backed lease was
   deliberately not built — it would need a migration and a coordination story
   nobody has specified.
3. **There is no raw-HTTP-HTML *search* fallback in this codebase.** The search
   cascade is direct GraphQL → Playwright. A raw-HTML path exists only for
   *PDP* amenity enrichment (`_fetch_listing_page_html_direct`). The event
   vocabulary keeps `raw_http_html` and `rendered_html` distinct rather than
   inventing a search-side HTML stage that does not exist.
4. **`refresh_session()` is a no-op in browser-only mode.** The
   `session_refresh` request class exists and is enforced, but currently has no
   outbound requests flowing through it.
5. **Pricing, comparable-selection, empty-inventory and report contracts are
   unchanged.** `classify_search_payload` and `classify_page_state` were not
   altered; the only addition to `search_result_contract.py` is a read-only
   evidence-path helper.

---

## 2. Request-path inventory

Every outbound Airbnb request path found in `worker/`, and the admission class
that now covers it.

| # | Path | Code | Class | Covered | Notes |
|---|---|---|---|---|---|
| 1 | Direct StaysSearch (GraphQL over HTTP) | `playwright_scraper.fetch_search_direct` | `search` | ✅ | Was: private 3-attempt loop with fixed sleeps. Now: admission + `Retry-After` + shared retry budget. |
| 2 | Browser StaysSearch (Playwright navigation + response capture) | `playwright_scraper._run_browser_search` → `_search_via_browser` | `browser_navigation` | ✅ | Circuit is checked *before* a page is opened. |
| 3 | Direct StaysPdpSections replay | `playwright_scraper.fetch_pdp_price_direct` | `pdp` | ✅ | Was: private 3-attempt loop. |
| 4 | Standalone PDP API client | `_try_pdp_api_listing_details` → `airbnb_pdp_api.airbnb_crawler.fetch` | `pdp` | ✅ | **Was entirely outside the limiter** — its own `requests.Session`, invisible to the aggregate ceiling. Now wrapped, and its `AntiBotError` is treated as authoritative block evidence. |
| 5 | Listing-page HTML GET (SSR amenities) | `_fetch_listing_page_html_direct` | `pdp` | ✅ | **Was entirely outside the limiter.** Emitted as `source=raw_http_html`. |
| 6 | Browser PDP navigation | `_get_listing_details_browser` → `_get_listing_details_via_browser` | `browser_navigation` | ✅ | |
| 7 | Generic browser navigation | `playwright_scraper.browse_url_html` | `browser_navigation` | ⚠️ Partial | Goes through the same browser runtime and tab cap, but is not individually admitted. Accounted for by the strict `AIRBNB_MAX_INFLIGHT_BROWSER=1` ceiling. |
| 8 | Target-extractor DOM navigations | `target_extractor` `page.goto` via `_AsyncPageSyncAdapter` | `browser_navigation` | ⚠️ Partial | Same: shares the browser runtime and its tab cap, not individually admitted. Same accounting. |
| 9 | Session refresh | `playwright_scraper.refresh_session` | `session_refresh` | n/a | No-op in browser-only mode; class reserved and enforced. |
| 10 | CDP endpoint probe (`/json/version`) | `target_extractor.check_cdp_endpoint`, `browser_runtime` | — | n/a | Localhost, not Airbnb. |
| 11 | Geocoding / alerts / auto-price HTTP | `core/geocoding.py`, `core/geocode_details.py`, `alerts.py`, `core/auto_price_assignment.py` | — | n/a | Not Airbnb hosts. |

Paths 7 and 8 are the honest gap: they are browser navigations reached through
adapter layers where per-call admission would mean threading a controller
through the DOM-extraction code. They are bounded — one browser navigation slot
in aggregate — but a burst of target extraction is not individually smoothed.
This is the "explicitly documented as not controllable, accounted for by a
stricter browser budget" option the prompt allows.

---

## 3. Event schema

Newline-delimited JSON, one object per line, `worker/logs/worker.jsonl` (or
stdout via `WORKER_EVENT_LOG_STDOUT=1` for an external agent). Console logging is
unchanged and stays human-readable.

Always present: `ts` (UTC ISO-8601, ms, `Z`), `schema_version`, `event`,
`severity`, `logger`.

Present when a scope is open: `trace_id`, `worker_instance_id`, `report_id`,
`target_listing_id`, `search_id`, `operation`, `listing_id`, `checkin`,
`checkout`, `offset`.

Per-attempt: `attempt_id`, `attempt_number`, `request_class`, `source`,
`endpoint_host`, `endpoint_path` (query dropped, persisted-query hash replaced
with `<hash>`), `graphql_operation`, `status`, `elapsed_ms`, `limiter_wait_ms`,
`permitted_rate_per_sec`, `permitted_concurrency`, `circuit_state`, `outcome`,
`reason_code`, `result_count`.

Transitions and control: `fallback_from`, `fallback_reason`, `cooldown_seconds`,
`backoff_source`, `backoff_seconds`, `direction`, `previous_rate_per_sec`,
`previous_concurrency`, `retries_used`, `retry_budget_per_operation`,
`retry_budget_per_report`, `evidence_paths`.

Artifacts: `artifact_id`, `artifact_path`, `artifact_sha256`,
`artifact_original_bytes`, `artifact_stored_bytes`, `artifact_truncated`,
`artifact_format`, `capture_reason`, `artifact_decode_error`.

Event names: `direct_http_started`, `direct_http_succeeded`,
`direct_http_degraded`, `direct_http_blocked`, `direct_http_overloaded`,
`raw_http_html_started`, `raw_http_html_succeeded`, `raw_http_html_failed`,
`playwright_started`, `playwright_captured_json`, `playwright_failed`,
`fallback_selected`, `cooldown_started`, `circuit_opened`, `circuit_half_open`,
`circuit_closed`, `limit_adjusted`, `retry_scheduled`,
`retry_budget_exhausted`, `artifact_captured`, `artifact_capture_failed`,
`admission_configured`, `calibration_event`.

### ID semantics

- `trace_id` — one per report/job, opened in `process_job`.
- `search_id` — one per logical search, opened in
  `search_listings_with_overrides`. Stable across the direct attempt and the
  Playwright escalation.
- `attempt_id` — one per network attempt, never reused across retries.

Propagated with `contextvars`, not mutable globals. `contextvars` do not cross
thread boundaries, so `scrape_trace.propagate()` binds the submitting thread's
context onto every callable handed to a pool (`concurrent_runner`,
`comp_collection`, `day_query`, `price_estimator`).

---

## 4. Configuration and the evidence behind it

**There is no live measurement behind these numbers, and none is claimed.**

The previous defaults (`AIRBNB_MIN_REQUEST_INTERVAL_MS=250` → 4 req/s,
`AIRBNB_MAX_INFLIGHT_REQUESTS=8`) were hypotheses that coincided with recurring
503s. The new defaults are lower, and the runtime adapts down from them:

| Setting | Old | New | Reasoning |
|---|---|---|---|
| Start rate ceiling | 4/s | **2/s** | Halved from a configuration observed to coincide with 503s. Arbitrary but conservative, and adaptive from there. |
| Aggregate in-flight | 8 | **4** | Same reasoning. |
| Search in-flight | — | **2** | Search is the highest-volume class; a sub-cap keeps it from consuming the whole aggregate. |
| PDP in-flight | — | **3** | Cheaper and faster than search; slightly more headroom. |
| Browser in-flight | — | **1** | Playwright is a recovery path, not a parallel bypass. Also covers inventory rows 7–8. |
| Decrease factor | — | **0.5** | Standard AIMD. |
| Recovery | — | **20 successes over ≥30s, +0.25 req/s, +1 concurrency** | Recovery must be much slower than backoff, or the worker oscillates back into the limit. |
| Retry budget | 3 attempts/call, unbounded across threads | **2/operation, 20/report** | The unbounded aggregate was the real defect: N concurrent threads × 3 attempts. |
| Circuit | none (search-session breaker only) | **5 consecutive signals → 120s, doubling to 900s** | Fail fast rather than escalate. |

**What must be validated in the deployment environment:** the actual sustainable
rate and concurrency. Run the bounded calibration (§5 of the runbook) in the
target environment and treat the result as a snapshot, not a limit. If it
reports `no_healthy_envelope_observed`, do not raise anything.

---

## 5. Multi-instance operation

Process-local policy. Set `AIRBNB_ADMISSION_INSTANCES` to the number of
concurrently running worker processes and each takes a proportional share of
the rate and concurrency ceilings. Static partition — an idle instance does not
lend its share. **A deployment that cannot declare its instance count must run a
single worker instance.** Logged at startup and covered by
`test_declared_instances_partition_the_budget`.

---

## 6. Sample trace (real output, redacted by the pipeline itself)

Produced by driving the direct path with a 503 (carrying `Retry-After: 1`) then
a challenge payload, with the browser path stubbed. The template's URL carried
`?key=SUPERSECRET…` and a `cookie` header; neither appears anywhere below.
Abridged to the fields that tell the story:

```jsonl
{"ts":"…11.340Z","event":"direct_http_started","trace_id":"trc-2360e802…","report_id":"a3f1c0de-…","target_listing_id":"1104839200","search_id":"sch-334f3b94…","attempt_id":"att-cc520788…","attempt_number":1,"operation":"stays_search_direct","source":"direct_json","checkin":"2026-07-04","checkout":"2026-07-05","offset":25,"limiter_wait_ms":0,"permitted_rate_per_sec":2.0,"permitted_concurrency":4,"circuit_state":"closed","endpoint_host":"www.airbnb.com","endpoint_path":"/api/v3/StaysSearch/<hash>"}
{"ts":"…11.340Z","event":"limit_adjusted","search_id":"sch-334f3b94…","request_class":"search","status":503,"outcome":"overload","direction":"decrease","permitted_rate_per_sec":1.0,"permitted_concurrency":2,"previous_rate_per_sec":2.0,"previous_concurrency":4}
{"ts":"…11.341Z","event":"cooldown_started","search_id":"sch-334f3b94…","status":503,"outcome":"overload","cooldown_seconds":1.0,"backoff_source":"retry_after","circuit_state":"closed"}
{"ts":"…11.341Z","event":"direct_http_overloaded","severity":"WARNING","attempt_id":"att-cc520788…","attempt_number":1,"status":503,"reason_code":"http_503","retry_after_present":true}
{"ts":"…11.341Z","event":"retry_scheduled","attempt_number":1,"reason_code":"http_503","backoff_seconds":1.0}
{"ts":"…12.342Z","event":"direct_http_started","search_id":"sch-334f3b94…","attempt_id":"att-f46ae1f7…","attempt_number":2,"permitted_rate_per_sec":1.0,"permitted_concurrency":2}
{"ts":"…12.343Z","event":"artifact_captured","artifact_id":"art-2b8fbb31e177…","artifact_path":"2026-08-13/art-2b8fbb31e177….json","artifact_sha256":"f5e266c8b050…0f4b","artifact_original_bytes":156,"artifact_stored_bytes":156,"artifact_truncated":false,"artifact_format":"json","capture_reason":"direct_search_blocked","reason_code":"graphql_auth_error","evidence_paths":["errors[0].extensions.code"]}
{"ts":"…12.343Z","event":"limit_adjusted","reason_code":"graphql_auth_error","outcome":"blocked","direction":"decrease","permitted_rate_per_sec":0.5,"permitted_concurrency":1,"previous_rate_per_sec":1.0,"previous_concurrency":2}
{"ts":"…12.343Z","event":"cooldown_started","reason_code":"graphql_auth_error","outcome":"blocked","cooldown_seconds":1.731,"backoff_source":"exponential_full_jitter"}
{"ts":"…12.343Z","event":"direct_http_blocked","severity":"WARNING","search_id":"sch-334f3b94…","status":200,"outcome":"blocked","reason_code":"graphql_auth_error","evidence_paths":["errors[0].extensions.code"],"artifact_id":"art-2b8fbb31e177…","artifact_path":"2026-08-13/art-2b8fbb31e177….json"}
{"ts":"…12.344Z","event":"fallback_selected","search_id":"sch-334f3b94…","fallback_from":"direct_json","source":"playwright_capture","fallback_reason":"graphql_auth_error"}
{"ts":"…14.087Z","event":"playwright_started","search_id":"sch-334f3b94…","attempt_id":"att-82e1402b…","attempt_number":1,"request_class":"browser_navigation","limiter_wait_ms":1743,"circuit_state":"closed"}
{"ts":"…14.087Z","event":"playwright_captured_json","search_id":"sch-334f3b94…","status":200,"outcome":"success","reason_code":"stayssearch_captured","result_count":18,"elapsed_ms":6120}
```

Things worth noticing in that trace:

- One `search_id` spans all thirteen events; three distinct `attempt_id`s.
- `Retry-After: 1` was honoured (`backoff_source: retry_after`); the
  block used jittered exponential backoff instead.
- The escalation to Playwright waited `limiter_wait_ms: 1743` — the block
  cooldown gated the browser, rather than the browser being an escape hatch
  around it.
- The blocked payload is on disk with a SHA-256, and `evidence_paths` names
  which field triggered the verdict without copying its value.

---

## 7. Before / after

**Baseline data is not available and has not been fabricated.** There is no
production telemetry from before this change: the whole point of the work is
that the previous logging could not answer these questions. The measurement
below is structural (what the code can now do), not empirical.

| Metric | Before | After |
|---|---|---|
| Request rate | Not measurable — no per-request events | Recorded per attempt; ceiling 2/s, current value in every `direct_http_started` |
| Peak concurrency | Not measurable | Capped at 4 aggregate, per-class sub-caps; `permitted_concurrency` on every attempt |
| 429/503 count | Not queryable | `jq 'select(.event=="direct_http_overloaded")'` |
| Fallback count | Not queryable | `jq 'select(.event=="fallback_selected")'`, grouped by `fallback_reason` |
| Playwright count | Not queryable | `jq 'select(.event=="playwright_started")'` |
| p50/p95 latency | Not recorded | `elapsed_ms` per attempt; percentiles computed by the calibration tool |
| Report completion time | Wall clock only | Unchanged; per-attempt `elapsed_ms` + `limiter_wait_ms` now decompose it |
| Uncontrolled request paths | 2 (PDP API client, listing-page HTML) | 0 fully uncontrolled; 2 partially (inventory rows 7–8) |
| Retries across threads | Unbounded in aggregate | 2/operation, 20/report |

Populate the empirical columns after the first production run using the runbook
queries; do not backfill the "before" column.

---

## 8. Tests

New: `test_admission_policy.py` (26), `test_scrape_trace_events.py` (13),
`test_scrape_artifacts.py` (15), `test_airbnb_calibration.py` (13).
Rewritten: `test_rate_limiter.py` (5) — now pins that the legacy shim delegates
to the single controller instead of holding private state.

Acceptance criteria → tests:

| # | Criterion | Test |
|---|---|---|
| 1 | Classes share the aggregate ceiling | `AggregateCeilingTest.test_search_pdp_and_browser_share_one_ceiling` |
| 2 | Starts smoothed; concurrency capped | `StartSmoothingTest`, `AggregateCeilingTest.test_per_class_cap_is_enforced_under_the_aggregate` |
| 3 | `Retry-After` delta + HTTP-date; invalid falls back | `RetryAfterTest` (6 tests) |
| 4 | Multiplicative decrease, jittered cooldown, bounded retries, slow additive recovery | `AdaptiveLimitTest` (5 tests) |
| 5 | Circuit prevents stampede; one half-open probe | `CircuitBreakerTest` (5 tests) |
| 6 | Retry budgets per operation and per report | `RetryBudgetTest`, `FallbackTransitionTest.test_exhausted_report_budget_stops_retrying` |
| 7 | Outcomes remain distinct | `OutcomeClassificationTest.test_outcomes_remain_distinct` |
| 8 | Stable `search_id`, unique `attempt_id` | `TraceContextTest.test_search_id_is_stable_while_attempt_ids_are_unique` |
| 9 | Ordered, queryable transition sequence with context | `FallbackTransitionTest.test_direct_failure_then_playwright_emits_an_ordered_sequence` |
| 10 | Blocked JSON saved, valid JSON, referenced, replays to same reason | `BlockedPayloadReplayTest` (3 tests) + the artifact assertions in criterion 9's test |
| 11 | Redaction, truncation, retention, quota, atomic-write failure, disabled | `RedactionTest`, `BoundsTest`, `DisabledAndFailureTest` (12 tests) |
| 12 | No secrets or bodies in logs | `LogHygieneTest.test_no_secrets_or_bodies_reach_the_event_log` |
| 13 | Existing blocked/integrity tests still pass | Full suite, §9 |
| 14 | Calibration stops at every bound; no network in unit tests | `SafetyBoundTest` (7 tests), `OptInTest` |

---

## 9. Test run — results as they actually are

`python -m unittest discover -s worker/tests` — **103 run, 103 passed, 0 failed,
0 skipped.**

`python -m pytest worker/tests -q` (the repo also has pytest-style tests that
`unittest discover` does not collect) — **875 passed, 7 failed, 2 skipped.**

The 7 failures are **pre-existing and unrelated** to this work:

```
test_browser_workload_distribution_e2e.py::test_self_price_capture_does_not_backfill_observed_price_from_later_date
test_daily_comp_coverage_e2e.py::test_fixed_pool_expands_geo_when_baseline_pool_is_short
test_daily_comp_coverage_e2e.py::test_run_scrape_uses_dynamic_day_query_when_fixed_yield_is_under_ten
test_price_extraction.py::test_client_pdp_extraction_uses_price_after_discount_not_primary_price
test_search_context_price_availability.py::test_parse_pdp_response_prefers_nightly_breakdown_over_fee_inclusive_primary_total
test_search_context_price_availability.py::test_parse_pdp_response_supports_amount_before_nights_breakdown_shape
test_search_context_price_availability.py::test_parse_pdp_response_prefers_one_night_price_after_discount_over_primary_price
```

All seven are price-extraction/comp-pool assertions in `parsers.py` and
`price_estimator.py`, neither of which this work modifies beyond adding a
context-propagation wrapper at three thread-pool submit sites. Verified by
neutralising `scrape_trace.propagate()` to the identity function and re-running
the three e2e failures — identical failures, so propagation is not the cause.
They are not fixed here (out of scope) but are flagged for follow-up.

Live/e2e tests remain opt-in and were not used as verification.

---

## 10. Files

New:
- `worker/core/scrape_trace.py` — trace/search/attempt IDs, retry budget, thread propagation
- `worker/core/scrape_events.py` — event helper, redaction, JSONL sink
- `worker/core/admission.py` — the single adaptive admission policy
- `worker/core/scrape_artifacts.py` — redacted, bounded diagnostic artifact store
- `worker/core/airbnb_calibration.py` — bounded opt-in calibration
- `docs/runbooks/scraper_observability_runbook.md`
- 4 new test modules

Modified:
- `worker/core/rate_limiter.py` — now a shim over the single controller
- `worker/scraper/playwright_scraper.py` — all request paths admitted + instrumented
- `worker/scraper/search_result_contract.py` — added `auth_error_evidence_paths()` (read-only; classification unchanged)
- `worker/core/concurrent_runner.py`, `worker/scraper/comp_collection.py`, `worker/scraper/day_query.py`, `worker/scraper/price_estimator.py` — context propagation into pool threads
- `worker/main.py` — installs the event sink, opens the report trace scope, runs artifact housekeeping
- `worker/tests/test_rate_limiter.py` — rewritten for the shim
