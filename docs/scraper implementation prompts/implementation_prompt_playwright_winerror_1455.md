# Implementation prompt: prevent Playwright WinError 1455 and unretrieved connection tasks

Implement, test, and document a production-safe fix for the failure recorded in:

`docs/scraper implementation prompts/error log/log-2026-8-7`

Work directly in this repository. First reproduce the failure deterministically with mocks/fault injection, then identify the complete lifecycle and concurrency root cause, implement the smallest robust architectural fix, and run the relevant automated tests. Do not treat increasing the Windows paging-file size as the application fix. That may be an operator mitigation, but the worker must bound resource usage, clean up failed starts, and surface an actionable error without emitting `Task exception was never retrieved`.

## Incident signature

The production log shows:

- direct StaysSearch returned empty results and fell back to the browser;
- `playwright.async_api.async_playwright().start()` attempted to create the Playwright driver subprocess;
- Windows `CreateProcess` failed with `OSError: [WinError 1455] The paging file is too small for this operation to complete`;
- Playwright's internal `Connection.run()` task completed with the same exception but was not retrieved;
- asyncio logged the exception twice (`ERROR:asyncio` and the application's timestamped asyncio logger).

This is both a resource-management failure and an async-lifecycle/observability failure. The implementation must address both.

## Repository-specific hypothesis to verify

Do not accept this hypothesis without tracing all call paths and proving it with tests/instrumentation:

- `PlaywrightScraper._ensure_browser()` starts a Playwright driver for every scraper instance.
- `PlaywrightScraper.fork()` deliberately creates a fresh runtime, driver, loop thread, and browser connection.
- `build_warmed_browser_client_pool()` can create up to three clients, and when only one CDP endpoint exists it currently creates the requested number of clients all pointing to that same endpoint.
- `worker/main.py`, `worker/scraper/comp_collection.py`, and `worker/scraper/price_estimator.py` can each construct pools. Tabs already have semaphores, but a tab limit does not bound the number of Playwright driver subprocesses or runtime threads.
- Failed warmups are skipped, but if every warmup fails the pool returns a cold client, deferring the same process-spawn failure to the browser fallback path.
- `_ensure_browser()` assigns `self._pw` before CDP connection is established and does not explicitly stop/clear a partially started runtime when all CDP candidates fail.
- `AirbnbClient.close_browser()` and other cleanup facades swallow exceptions, which can hide incomplete cleanup.

Measure or count runtime starts, active driver subprocess abstractions, event-loop threads, clients per endpoint, contexts, and tabs under representative pool workflows. Establish whether pools overlap or churn during one report and whether cleanup always runs on success, ordinary failure, cancellation, timeout, and warmup failure.

## Required design outcomes

### 1. Bound Playwright runtimes independently from tab concurrency

Introduce a clear runtime ownership model. Prefer one shared Playwright runtime/driver connection per normalized CDP endpoint within the worker process, with reference-counted or lease-based ownership and concurrency-safe initialization. Multiple logical clients targeting the same CDP endpoint must not each spawn a separate Playwright driver merely to open concurrent tabs.

If repository constraints make a shared runtime unsafe, implement a process-wide bounded runtime manager instead and explain the tradeoff. In either design:

- normalize equivalent endpoint spellings before using them as keys;
- prevent duplicate initialization races with a lock/single-flight mechanism;
- maintain the existing per-browser tab semaphore or an equivalent cap;
- never hold a blocking `threading.Lock` across an `await`;
- define ownership of the event-loop thread, Playwright object, browser connection, context, and tab gate;
- make acquire/release idempotent and safe under concurrent close calls;
- do not close the user's externally managed CDP browser;
- do close owned contexts, Playwright driver connections, and loop threads when the final lease is released or the worker shuts down;
- ensure a failed or cancelled initializer cannot leave a poisoned registry entry that blocks later recovery;
- prevent unbounded registry growth when endpoint configuration changes.

Do not solve this by globally serializing every browser operation. Existing safe tab concurrency and distribution across genuinely distinct CDP endpoints should remain available.

### 2. Make browser-pool sizing reflect real endpoints and resource limits

Review `worker/scraper/browser_runtime.py` and every pool caller.

- Do not create several independent browser runtimes for the same single endpoint solely because `requested_size` is larger than the number of endpoints.
- Represent concurrency against one browser with tab leases, not duplicate runtime clients, unless there is a proven repository requirement for separate logical clients. If separate clients remain necessary, they must share the endpoint runtime safely.
- Preserve round-robin distribution across distinct healthy endpoints.
- Add an explicit, bounded configuration for maximum active Playwright runtimes. Validate and clamp invalid values, choose a conservative default, and log the requested versus effective pool/runtime size.
- If one endpoint fails to warm, retain healthy endpoints. If all endpoints fail with a resource-exhaustion error, do not return a cold client that immediately retries the same spawn. Propagate a typed, sanitized browser-resource-unavailable error to the report boundary.
- Avoid multiplicative retries across pool slots, dates, offsets, or nested fallback layers. Resource exhaustion should trip a short-lived process/session circuit breaker or equivalent fail-fast state, with a bounded recovery policy.

### 3. Make startup transactional and cleanup deterministic

Refactor `_ensure_browser()` and the runtime manager so startup has commit/rollback semantics:

1. Create/start the Playwright runtime into local variables.
2. Connect to a CDP candidate.
3. Publish the live runtime/browser state only after successful initialization.
4. On any exception or cancellation, await/perform all feasible cleanup, clear state, release registry capacity, and re-raise a classified exception with exception chaining.

The implementation must tolerate failure at each stage: event-loop thread creation, `async_playwright().start()`, driver subprocess creation, CDP connection, context selection/creation, cookie warmup, and page creation.

Never call `async_playwright().start()` again on a half-started instance. A later bounded recovery attempt must begin from a known clean state. Cleanup must not depend on `__del__`; retain `__del__` only as a last-resort safeguard if needed.

Audit `_run_async()` timeout/cancellation behavior. If `future.result(timeout=...)` times out, cancel and drain the submitted coroutine in a bounded way so it cannot continue initializing a browser after the synchronous caller has failed. Avoid creating coroutine objects that are never awaited when setup itself fails before submission.

### 4. Classify Windows process-spawn resource exhaustion

Add a narrow classifier and typed error for browser runtime resource exhaustion. It must recognize the original error through `__cause__`/`__context__`, including:

- `OSError` with `winerror == 1455`;
- equivalent `errno`/message forms where the platform wrapper does not retain `winerror`;
- optionally other clearly documented Windows process-creation resource codes only if they require the same response.

Do not classify CDP authentication failures, browser disconnects, navigation timeouts, anti-bot challenges, rate limits, parsing errors, or arbitrary `OSError`s as resource exhaustion.

Expose a sanitized operational message such as `browser runtime unavailable: system virtual memory exhausted`. Preserve the exception chain and safe structured fields (`reason_code`, operation, endpoint host/port or slot, active runtime count, configured cap, attempt) for logs. Do not log cookies, tokens, full query URLs, raw HTML, or full environment contents.

At the orchestration/report boundary, fail the affected job once with the existing project error contract. Do not return a misleading empty-search success, repeatedly fall back to the browser, or mark the error as an Airbnb challenge.

### 5. Retrieve every async task exception and remove duplicate noise

The fix must ensure there is no `Task exception was never retrieved` for application-created tasks or for Playwright startup failures that the application can control.

- Keep strong references to application-created tasks until completion.
- Await/gather tasks during failure and shutdown with `return_exceptions=True` where appropriate.
- For fire-and-forget tasks, install a done callback that retrieves and routes exceptions through sanitized structured logging.
- Do not install a global exception handler that merely suppresses all asyncio errors.
- Do not blanket-filter the text `Task exception was never retrieved`.
- Ensure the runtime loop drains/cancels pending tasks before closing.
- Investigate the duplicate asyncio emission. Configure logging so one failure produces one primary application event plus, at most, one intentionally distinct diagnostic event. Do not globally disable asyncio logging.

Because the observed task is created inside Playwright, test the supported startup/cleanup sequence against the installed Playwright API. Do not patch `site-packages` or depend on private Playwright internals in production code.

### 6. Preserve scraper behavior

- Direct HTTP search remains the preferred fast path.
- A legitimate first-page empty result may still invoke one browser fallback according to the existing search contract.
- Deep-offset valid-empty handling remains unchanged.
- Existing browser disconnect recovery remains bounded to one retry where currently intended.
- Distinct CDP endpoints continue to distribute work.
- Tab slots are always released, even when navigation, context, or runtime failures occur.
- Closing one logical client must not tear down a shared runtime still leased by another client.
- Worker shutdown must release every lease and leave no `playwright-async-runtime` threads.

## Files to inspect and likely modify

- `worker/scraper/playwright_scraper.py`
- `worker/scraper/browser_runtime.py`
- `worker/scraper/airbnb_client.py`
- `worker/scraper/scraper_errors.py` and the report error-sanitization boundary
- pool callers in `worker/main.py`, `worker/scraper/comp_collection.py`, and `worker/scraper/price_estimator.py`
- logging configuration if it causes duplicate asyncio records
- focused tests under `worker/tests/`, especially `test_browser_runtime_pool.py` and `test_playwright_browser_recovery.py`

Keep changes scoped to Playwright runtime lifecycle, resource bounds, and error propagation. Do not weaken search-result validity, availability, pricing, geographic, capacity, or anti-bot protections.

## Deterministic reproduction and required automated tests

Unit and regression tests must not require live Airbnb, a real CDP browser, actual memory exhaustion, or modification of the host paging file. Use fakes/mocks around Playwright startup, subprocess-facing behavior, browser/context objects, and logging.

Add tests proving all of the following:

1. Fault-injecting `OSError(22, ..., winerror=1455)` at Playwright startup reproduces the classified failure path.
2. A nested/chained WinError 1455 is recognized; an unrelated `OSError` is not.
3. Failed startup publishes no `_pw`, browser, context, live registry entry, runtime lease, or orphan loop thread.
4. A partial start followed by CDP failure calls `stop()` exactly once and clears state.
5. Cancellation and synchronous timeout during initialization cancel/drain the coroutine and leave no later background connection.
6. A later allowed retry after cleanup can initialize successfully; retries are strictly bounded.
7. Concurrent first use by many callers for the same normalized CDP endpoint invokes Playwright startup exactly once.
8. Several logical clients for one endpoint share one runtime while retaining correct tab limiting.
9. Distinct endpoints create no more than the configured maximum runtimes and still receive distributed work.
10. Closing one of multiple leases does not stop the runtime; closing the final lease stops it exactly once.
11. Concurrent or repeated close calls are idempotent and do not underflow references or over-release semaphores.
12. A failed initializer wakes all waiters with the classified error and does not deadlock them.
13. An all-slot resource-exhaustion warmup propagates the typed error and does not append a cold fallback client.
14. One failed endpoint plus one healthy endpoint yields only usable healthy capacity without retry multiplication.
15. Once resource exhaustion is known, later browser fallbacks in the same bounded breaker window fail fast without additional Playwright starts; the documented reset/expiry permits recovery.
16. The browser fallback surfaces the typed operational error, not empty results, challenge, or generic parsing failure.
17. The job/report failure path is invoked exactly once and receives only the sanitized public message/code.
18. Captured logs contain one primary resource-exhaustion event and no duplicate traceback record from application logging configuration.
19. With a test loop exception handler capturing contexts and after forced garbage collection/loop shutdown, no context contains `Task exception was never retrieved` or `coroutine was never awaited`.
20. Normal Playwright startup, direct-search success, browser disconnect recovery, tab gate behavior, and pool cleanup remain green.
21. After a full pool workflow and shutdown, no runtime registry entries, active leases, open tab counts, or `playwright-async-runtime` threads remain.

Avoid assertions based only on sleeps. Use events/barriers and explicit counters for concurrency tests. Give every thread/future a bounded join/timeout so a regression fails rather than hanging the suite.

## Suggested test commands

Adapt exact file names to the implementation, but run at least:

```powershell
python -m pytest worker/tests/test_browser_runtime_pool.py worker/tests/test_playwright_browser_recovery.py -q
python -m pytest worker/tests -q -m "not live and not e2e"
```

Also run the repository's configured formatter/linter/type checks for modified files. Do not run live/E2E tests unless the required authenticated CDP environment is explicitly available. Record exact commands, pass/fail counts, skips, and environment-dependent checks not run.

## Optional controlled Windows smoke test

If an authenticated local CDP browser is explicitly configured, run a non-destructive smoke test that repeatedly acquires/releases logical clients and tabs while recording process/thread counts. Do not deliberately exhaust physical memory, reduce the paging file, or change OS virtual-memory settings.

Confirm that:

- one endpoint uses one bounded Playwright driver runtime;
- distinct configured endpoints respect the runtime cap;
- counts return to baseline after shutdown;
- a healthy browser fallback still works;
- logs contain neither an unretrieved-task warning nor duplicated failure output.

## Acceptance criteria

- The original WinError 1455 path is reproducible deterministically without exhausting the machine.
- Playwright driver/runtime count is explicitly bounded and does not scale with every logical client, date, offset, or pool slot.
- Concurrent clients targeting one CDP endpoint do not race to spawn duplicate Playwright drivers.
- Startup is transactional; every partial failure and cancellation is cleaned up.
- Resource exhaustion stops retry storms and reaches the report boundary once as a typed, sanitized operational failure.
- No cold fallback client is returned after all pool warmups fail from resource exhaustion.
- No `Task exception was never retrieved`, un-awaited coroutine warning, orphan runtime thread, leaked lease, or duplicate asyncio traceback remains in the regression tests.
- Existing direct HTTP behavior, valid-empty semantics, multi-endpoint distribution, browser recovery, and tab concurrency remain intact.
- Increasing the Windows paging file is documented only as an optional operational mitigation, not required for correctness.

## Deliverable

Provide:

- the implementation and regression tests;
- a concise root-cause report distinguishing OS memory pressure from application amplification/leak behavior;
- the runtime ownership and shutdown contract;
- configuration defaults and migration notes;
- files changed;
- exact test/lint/type-check commands and results;
- any live Windows/CDP validation intentionally deferred.
