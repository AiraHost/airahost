# Investigation prompt: idle worker begins failing PDP extraction until restart

Investigate a hard-to-reproduce production incident in the AiraHost scraper worker. This is an investigation and observability task first. Do not guess at a fix, change stealth/browser fingerprints, add retries, or restart the affected worker before capturing evidence. Implement a fix only if the evidence identifies a root cause with reasonable confidence.

## Incident signature

After a worker has remained running for several days with little or no report activity, a new report for this listing begins returning this error almost immediately:

```text
PDP individual listing extraction failed (payload + rendered DOM): https://www.airbnb.com/rooms/1644615801258352985
```

Restarting the worker makes the same flow work again.

Two observations are especially important:

1. The error appears much faster than a normal PDP direct-fetch plus rendered-DOM fallback should take.
2. The worker log shows no sign of claiming the corresponding job when the error appears. In the current main loop, a successfully claimed pricing report should produce both a structured `[report_id] claimed (...)` line and `Claimed job <id> (attempt <n>)` before `process_job()` begins.

The reporter suspects stale state. Treat staleness as potentially affecting the worker process itself, not only Chrome or Playwright. A full worker restart simultaneously replaces Python process state, threads/event loops, singleton clients, HTTP pools, cached request templates, admission/circuit state, logging handlers, database clients, and possibly browser connections. Determine which of those reset boundaries actually restores service.

## Important repository evidence to verify

- `worker/tests/test_report_error_message_leak.py` documents a previous incident involving this exact error text. Current behavior is intended to degrade unusable PDP extraction to an empty spec plus warnings rather than copy that internal scraper text into `pricing_reports.error_message`.
- `worker/scraper/target_extractor.py` contains the payload and browser-DOM target extraction chain.
- `worker/scraper/playwright_scraper.py` contains the prioritized PDP paths: standalone PDP API, captured-template direct replay, direct listing HTML enrichment, and Playwright/CDP browser capture.
- `worker/scraper/playwright_runtime.py` owns long-lived Playwright/CDP runtime state and reconnect/reset behavior.
- `worker/main.py` owns job polling, claim logs, heartbeat, failure persistence, worker version, lane, and environment.
- `worker/core/db.py` owns the atomic claim RPC and its legacy-signature fallback.

The exact historical message appearing without a matching claim log may indicate that the visible error and inspected log did not originate from the same process, revision, job attempt, environment, lane, or time. Prove or disprove that before focusing narrowly on Airbnb detection.

## Goal

Produce an evidence-backed root-cause report and the minimum safe diagnostic instrumentation needed to capture the failure during a multi-day idle soak. The investigation must explain both:

- why PDP extraction fails until restart; and
- why the error can become visible without the expected claim log.

A hypothesis that explains only one observation is incomplete.

## Read before changing code

Read these files and their immediate callers/tests:

- `worker/main.py` (startup identity, polling, claim, `process_job`, `_execute_analysis`, error persistence, heartbeat, shutdown)
- `worker/core/db.py` and every migration defining `claim_pricing_report`
- `worker/scraper/target_extractor.py`
- `worker/scraper/playwright_scraper.py`
- `worker/scraper/playwright_runtime.py`
- `worker/scraper/browser_runtime.py`
- `worker/scraper/cdp_preflight.py`
- `worker/scraper/airbnb_pdp_api/airbnb_crawler.py`
- `worker/scraper/airbnb_pdp_api/get_hash.py`
- `worker/scraper/scrape_events.py` and logging setup/runbook
- `worker/tests/test_report_error_message_leak.py`
- tests covering PDP priority/fallback, runtime reset, CDP attach, job claim, heartbeat, lanes/environments, and structured logging
- deployment/service definitions and worker launch scripts, including restart policy, working directory, environment loading, log destinations, and the Chrome/CDP launch/profile lifecycle
- frontend/API code that creates, retries, reads, or displays `pricing_reports.error_message`

Search the whole repository and deployed artifact for the exact error string. Determine which revisions can still write it and which database fields/UI paths can display an old value.

## Investigation order

### 1. Establish event provenance

For one affected report, build a single UTC timeline from database row history, API/frontend request logs, every worker log destination, structured JSONL, service-manager logs, and Chrome/CDP logs. Record:

- report ID, listing ID, `created_at`, `updated_at`, `worker_claimed_at`, `completed_at`, status, worker attempts, claim token, heartbeat, lane, target environment, and saved-listing/report linkage;
- worker instance ID, host, PID, process start time, executable and working directory, loaded commit/version, lane/environment, CDP endpoint, log file path, and JSONL sink path;
- every UI/API poll or mutation that could expose or preserve `error_message`;
- whether the observed text was newly written for this attempt or was stale from a prior attempt/report.

Do not correlate by wall-clock proximity alone. Correlate by report ID, claim token, attempt number, instance ID, and database timestamps. Check clock skew and log rotation/retention.

Explicitly test these provenance hypotheses:

- another worker instance claimed and failed the job;
- the operator inspected the wrong lane, environment, host, container, service, stdout stream, or rotated log;
- a stale or old-revision process is still running alongside the expected process;
- the frontend displayed an old `error_message`, cached report, or previously failed report;
- a scheduler/API path wrote the error without the scraper worker;
- the claim succeeded in the database but logging was filtered, buffered, routed elsewhere, or failed;
- the legacy claim RPC ignored lane/environment and allowed an unexpected worker to claim;
- log lines were lost during rotation while the database write survived;
- max-attempt or stale-running recovery changed the row without entering the ordinary scrape path.

### 2. Measure where the fast failure occurs

Instrument monotonic elapsed time around every boundary, not just the final exception:

`job_created -> claim_rpc_started -> claim_rpc_returned -> claimed_log_emitted -> process_job_started -> scraper_created -> pdp_api_started/finished -> direct_pdp_started/finished -> direct_html_started/finished -> browser_admission_started/acquired -> cdp_connect_started/finished -> page_created -> navigation_started/response/dom_ready -> extraction_finished -> fail_job_started/finished -> API/UI_observed_error`

Each terminal error must include the ordered attempted-path summary and elapsed time. This must reveal whether the failure is genuinely a rapid scraper short-circuit, a pre-scrape job error, or merely a rapid read of already-persisted state.

### 3. Audit stale and restart-sensitive state in the worker and browser

Inventory every process-lifetime and browser-profile-lifetime object. For each, record creation time, last success/use, health signal, reset path, thread/loop ownership, and whether restart clears it:

- Supabase client/session and HTTP connection pool;
- scraper singleton/client and `requests.Session` pools;
- captured StaysPdp template, API key/hash, cookies, CSRF tokens, headers, locale/currency, user agent, and caches;
- PDP API client initialization and cookie warming;
- adaptive admission controller, cooldown/circuit state, retry budgets, locks, semaphores, and permits;
- Playwright runtime registry, async event-loop thread, browser/context/page handles, CDP websocket, pending tasks, and target/session IDs;
- externally managed Chrome process, profile lock, service worker/cache storage, cookies, local storage, memory, handles, tabs, renderer processes, and automatic Chrome updates;
- DNS/TLS keep-alive state, proxy/NAT mappings, host sleep/resume, network-interface changes, and system clock changes;
- log handlers/queues and database heartbeat threads.

Look for time-of-check/time-of-use bugs, permanently cached failures, stale closed handles reported as healthy, poisoned connection pools, unreleased admission slots, open circuits that never half-open, stopped event loops, background-thread death, and state reset only in constructors/startup.

Do not use “the browser became stale” as a catch-all explanation. Separately test whether the stale object belongs to:

- the worker process: dead background thread, stopped async loop, wedged future, singleton corruption, cached exception/failure result, stale Supabase client, exhausted executor, leaked lock/semaphore, logging handler failure, or process resource exhaustion;
- the scraper client: expired cookies/templates/API key material, poisoned `requests.Session`, connection-pool state, cached classifier/circuit result, or scraper instance reused beyond its safe lifetime;
- the Playwright/CDP bridge: disconnected websocket, closed browser/context/page retained in a registry, orphaned driver, invalid target/session ID, or runtime health check that returns a false positive;
- Chrome/profile state: expired auth, accumulated service-worker/cache/profile state, renderer degradation, profile lock, browser update mismatch, or server-side session risk state;
- database/job state: stale-running lease, claim-token mismatch, old attempt/error data, legacy RPC behavior, or a row observed before the current worker ever claimed it.

Record whether a component is created once at worker startup, once per report, once per request, or externally. Verify its documented and actual lifetime. Long idle time may expose expiry, dead connections, suspended threads, and clock assumptions even when no scraper request occurs.

### 4. Classify the actual PDP responses

For each PDP method, capture bounded, sanitized diagnostic metadata and artifacts using the existing artifact/event infrastructure:

- method/source, sanitized host/path and GraphQL operation;
- HTTP status, content type, response size, latency, redirect chain, final URL, and safe Airbnb request IDs;
- classifier result and evidence paths;
- whether the payload contains sections/amenities/price, GraphQL errors, login/challenge evidence, an unavailable/deleted listing result, malformed JSON, or a different schema;
- DOM title, final URL, ready state, key selector counts, and screenshot/HTML artifact references for browser fallback;
- exception type and sanitized exception chain.

Never put cookies, authorization values, API keys, signed query strings, full raw payloads, or full HTML into ordinary logs. Preserve artifacts only through the bounded/redacted diagnostic store.

Determine whether listing `1644615801258352985` is special by comparing it with at least one known-good listing under the same stale process and the same listing under a fresh process. Do not conclude that Airbnb blocked the worker from a single listing failure.

### 5. Test stale-session, fingerprint, and detection hypotheses carefully

First document the exact deployed browser launch command, Chrome version, Playwright version, persistent profile, context options, user agent, locale/timezone, webdriver-related properties, init scripts, and any stealth package. Compare stale-versus-fresh values in the same deployment.

Possible browser/detection causes include expired authentication/session material, server-side risk state, inconsistent user agent/client hints, an updated Chrome binary with an old long-running process, fingerprint drift, accumulated profile state, challenge pages, or a degraded CDP context. Distinguish these from local runtime corruption.

Do not add or tune stealth/evasion behavior as an experiment against production. The reported hypothesis is stale state, not stealth mode. Do not rotate identities/proxies, defeat CAPTCHAs, or retry through authoritative challenges. A legitimate block should produce a stable blocked reason and cooldown, not masquerade as extraction failure.

### 6. Reproduce with bounded soak and fault injection

Build deterministic tests with fakes for restart-sensitive failures, then an opt-in low-volume soak harness. The harness must support:

- baseline PDP checks, a configurable idle interval, then the same checks without process restart;
- separate probes of direct PDP API, captured-template replay, direct HTML, CDP health, browser navigation, and DOM extraction;
- periodic local health snapshots that do not query Airbnb during the idle period;
- strict caps on duration, requests, concurrency, retries, and artifact storage;
- machine-readable output including process/browser identity and timing;
- comparison of soft component reset, CDP reconnect, new page/context, browser restart, and full worker restart, stopping as soon as the smallest recovery boundary is identified.

Normal tests and CI must make no live Airbnb calls. The live soak must be explicitly enabled and must stop on challenge, 429, 503, or the configured error threshold.

Use fault injection to cover at least:

- stale/closed CDP browser and context handles;
- stopped or wedged Playwright event-loop thread;
- expired/corrupt cookies or captured PDP request template;
- poisoned direct-HTTP session/keep-alive connection;
- admission circuit stuck open and semaphore/permit leakage;
- Supabase claim success followed by logger failure/routing mismatch;
- two workers with different revisions/lane/env/log destinations;
- stale `error_message` displayed before a new claim;
- process sleep/resume and Chrome version mismatch where feasible.

## Required observability changes

If current telemetry cannot prove provenance, add the smallest durable instrumentation before waiting for recurrence:

1. Emit a startup event with a non-secret worker instance ID, host, PID, process start time, code version/commit, executable, working directory, worker lane/environment, CDP endpoint label, Chrome/Playwright versions, and log sink identity.
2. Emit `claim_rpc_started`, `claim_rpc_returned`, and `job_claimed` events with report ID, claim token hash, attempt, lane/environment, instance ID, and monotonic timing. Flush error/claim events or otherwise prove delivery semantics.
3. Persist worker instance/version and a compact terminal reason code in report debug metadata on every completion/failure, guarded by the claim token.
4. Emit one terminal event for every PDP method with source, outcome, reason, duration, and artifact reference.
5. Add a cheap local health snapshot for runtime thread liveness, CDP connectivity, browser/context/page counts, session/template age, circuit state, permits, memory/handles, and last successful PDP timestamp. It must not make an Airbnb request merely because the worker is idle.
6. On UI/API report reads, make it possible to distinguish a current pending attempt from an old persisted error. Do not erase forensic state before it is captured.

Use stable reason codes. Logging failures must not fail a report.

## Decision matrix

Use controlled comparisons to locate the smallest state boundary that restores service:

| Recovery action | What recovery would implicate |
|---|---|
| Retry same method with no reset | transient upstream/network response |
| Recreate HTTP session only | stale/poisoned connection pool or cached HTTP state |
| Refresh template/cookies only | expired request template/auth/session material |
| Recreate page only | stale page/target state |
| Reconnect Playwright to existing Chrome | stale CDP/runtime handle or loop ownership |
| Create a new browser context | poisoned context storage/fingerprint/session state |
| Restart Chrome, retain worker | browser process/profile/runtime degradation |
| Restart worker, retain Chrome | worker-local singleton/thread/cache/admission state |
| Full restart only | interaction between worker and browser, or uncontrolled deployment state |

Treat this table as diagnostic guidance, not proof by itself; corroborate with telemetry and repeatability.

## Tests and acceptance criteria

Add deterministic coverage proving:

1. Every claimed job is traceable from claim RPC through its terminal database mutation using report ID, attempt, and worker instance.
2. A terminal PDP failure records every attempted method and elapsed time; no supposedly rendered-DOM failure can occur without a browser-attempt event unless it is explicitly classified as stale/legacy data.
3. Claim/log sink failures do not create an untraceable report mutation.
4. Stale persisted `error_message` cannot be mistaken for the current queued/running attempt in the API/UI contract.
5. Browser/runtime health detects closed handles, dead loop threads, and CDP disconnects, and the narrow reset path actually replaces stale state.
6. Session/template/cookie age and refresh behavior are observable and bounded.
7. Admission slots and circuit state recover correctly after long idle periods and monotonic-clock changes.
8. Multiple workers with different lane/environment/version values remain attributable; legacy claim-RPC fallback is visible and tested.
9. Diagnostic artifacts and logs remain sanitized.
10. Existing PDP extraction, job claim, report error-message, browser-runtime, and structured-logging tests continue to pass.

Run focused deterministic tests first, then `python -m unittest discover -s worker/tests`. Clearly report failures and skips. Do not use a successful restart as the acceptance test.

## Deliverables

- A UTC incident timeline for at least one affected report, or a precise list of missing evidence if the incident predates new instrumentation.
- A request/control-flow diagram from report creation through claim, PDP methods, database failure write, and UI observation.
- A ranked hypothesis table containing evidence for, evidence against, a discriminating test, and current confidence for every plausible cause category.
- An inventory of process-lifetime and browser-lifetime state and what resets each item.
- The minimal diagnostic instrumentation and deterministic tests needed for the next recurrence.
- An opt-in, bounded idle-soak procedure and machine-readable result format.
- If root cause is proven, a narrowly scoped fix, regression tests, rollback plan, and explanation of why restart helped.
- If root cause is not proven, say so plainly. Do not present a generic “stale browser” diagnosis or a speculative stealth change as the fix. Identify which worker/browser state remains unproven.

Before making changes, summarize the known facts, contradictions, code/deployment provenance, ranked hypotheses, and exact evidence needed to distinguish them. After each significant step, state what changed, what was verified, and what remains unknown.
