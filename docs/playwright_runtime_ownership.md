# Playwright runtime ownership (WinError 1455 fix)

Incident: `docs/scraper implementation prompts/error log/log-2026-8-7`
Implementation prompt: `docs/scraper implementation prompts/implementation_prompt_playwright_winerror_1455.md`

## Root cause

Two separate defects met in one log line.

### 1. OS memory pressure (the trigger)

`OSError [WinError 1455] The paging file is too small for this operation to
complete` comes out of `CreateProcess`. The Windows commit limit (RAM + page
file) was exhausted, so the OS refused to create another process. Raising the
page file is a legitimate *operator* mitigation, and it is not the fix: a worker
must bound its own resource use and fail cleanly when the host says no.

### 2. Application amplification and leak (the reason the host ran out)

Each `PlaywrightScraper` owned a private `playwright-async-runtime` loop thread
and its own `async_playwright().start()` — that is, its own node driver
subprocess. Drivers therefore scaled with *logical clients*, not with browsers:

| Source | Multiplier |
| --- | --- |
| `build_warmed_browser_client_pool(requested_size=3)` against a single endpoint | 3 clients → 3 drivers |
| `PlaywrightScraper.fork()` in `comp_collection` fast-detail enrichment | one driver per worker thread |
| Pools built independently in `main.py`, `comp_collection.py`, `price_estimator.py` (day-query, benchmark day-query, two spec-repair pools) | one set per phase, per report |
| Endpoint spellings deduped by raw string (`http://…:9222` vs `ws://…:9222/devtools/browser/<id>`) | the same browser counted as several endpoints |

On top of the amplification there was a genuine leak. `_ensure_browser()`
assigned `self._pw` *before* connecting over CDP:

```python
self._pw = await async_playwright().start()   # driver now running
for cdp_url in candidates:
    self._browser = await self._pw.chromium.connect_over_cdp(...)
raise RuntimeError("Could not connect to any CDP endpoint")   # _pw still set, never stopped
```

The next call re-entered on `self._browser is None` and started **another**
driver, leaving the previous one running. Every failed connect cost one live
node process for the life of the worker. Tab semaphores did not bound this —
they cap pages per browser, not drivers per process. Cleanup depended on
`close_browser()` (which swallowed every exception) and on `__del__`.

### 3. The unretrieved task exception (the second half of the log)

Playwright 1.57 `PlaywrightContextManager.__aenter__` does:

```python
loop.create_task(self._connection.run())     # no reference kept
```

`Connection.run()` awaits `transport.connect()`, which on failure sets
`on_error_future` **and** re-raises. `start()` therefore raises the OSError to
the caller *and* the orphan `run()` task finishes holding the same exception,
which nobody retrieves — so asyncio reports `Task exception was never retrieved`
from the task destructor.

Because it was neither retrieved nor classified, the failure reached the report
as the generic `Service is busy. An error occurred during analysis`, after every
date and offset had re-attempted the browser fallback.

## The ownership contract

`worker/scraper/playwright_runtime.py` owns all of it now.

**One runtime per normalized CDP endpoint per worker process.** A runtime owns:
its event-loop thread, the Playwright object, the CDP browser *connection*, any
context it created itself, and the tab gate. It never owns the user's Chrome:
teardown stops the driver (`pw.stop()`), it never calls `browser.close()`. A
context that already existed in the browser is used but not owned, so it is
never closed.

**Leases, not instances.** `acquire_runtime(cdp_url)` returns a `RuntimeLease`
and starts nothing — no thread, no driver. Every `PlaywrightScraper`, including
`fork()` clones, holds one. `release()` is idempotent and reference-counted:
closing one client never disconnects another; the final release stops the
runtime exactly once and drops the registry entry (so the registry cannot grow
as endpoint configuration changes).

**Single-flight, transactional startup.** `ensure_started()` holds a plain
`threading.Lock` (never acquired from inside a coroutine, so never held across an
`await`). Concurrent first users wait and share one start. Startup builds the
loop thread, driver, connection and context in local variables and publishes them
only on success; any exception or cancellation stops the driver exactly once,
joins the loop thread, releases the reserved cap slot, and re-raises a classified
error with `__cause__` intact. A half-started runtime is never re-entered — a
later attempt begins from a clean state.

**Bounded.** `AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES` caps concurrently active
drivers, counting in-flight starts so two endpoints cannot both slip past the
check. Exceeding it raises `BrowserRuntimeUnavailable("runtime_cap_reached")` —
not the resource-exhaustion type, and it does not trip the breaker.

**Tab concurrency is unchanged in shape.** The per-browser tab gate still caps
pages (`AIRBNB_PLAYWRIGHT_MAX_TABS`, default `MAX_SCRAPER_WORKERS`, ceiling 8),
but the gate is now shared by every client on that endpoint rather than
duplicated per client — so N clients on one browser no longer mean N× the tab
budget. `reset_tab_gate()` refuses to re-arm while another client holds slots.
Distinct endpoints still get distinct runtimes and still receive round-robin
work.

**Shutdown.** `shutdown_all()` runs from `worker.main.main()` and from `atexit`:
every lease released, every driver stopped, no `playwright-async-runtime` thread
left.

## Error contract

`worker/scraper/scraper_errors.py`:

- `BrowserRuntimeUnavailable` — public message `browser runtime unavailable`,
  code `browser_runtime_unavailable`.
- `BrowserRuntimeResourceExhausted` (subclass) — public message
  `browser runtime unavailable: system virtual memory exhausted`, code
  `browser_runtime_resource_exhausted`.

`structured_fields()` carries only `reasonCode`, `operation`, `endpoint`
(host:port), `activeRuntimes`, `runtimeCap`, `attempt`. No cookies, tokens, query
URLs, HTML, or environment.

`is_process_spawn_resource_exhaustion()` walks `__cause__`/`__context__` and
matches only `OSError` with `winerror == 1455` (`ERROR_COMMITMENT_LIMIT`,
including the `args[3]` form when a wrapper drops `.winerror`) or the message
`paging file is too small`. CDP auth failures, disconnects, navigation timeouts,
anti-bot challenges, rate limits, parse errors and arbitrary `OSError`s are
explicitly *not* classified.

**Breaker.** The first classified exhaustion opens a process-wide breaker for
`AIRAHOST_BROWSER_RUNTIME_BREAKER_SECONDS` (default 120). While it is open,
`ensure_started()` fails fast without touching Playwright — so later dates,
offsets, pool slots and nested fallback layers cannot turn one exhausted host
into a retry storm. It expires on its own; `reset_resource_breaker()` clears it.

**Propagation.** `_run_browser_search` re-raises `BrowserRuntimeUnavailable`
without retrying or wrapping. `day_query` re-raises it alongside
`AirbnbSearchBlocked` instead of degrading it to a per-day error.
`build_warmed_browser_client_pool` raises it when every endpoint failed on
resource exhaustion, rather than returning a cold client that would repeat the
same spawn. `worker/main.py` fails the job once with the public message and the
structured fields. Live-price capture — documented non-fatal, it only enriches
an already-built calendar — degrades to `livePriceStatus:
browser_runtime_unavailable` instead of discarding a finished report.

## Async-task hygiene

Each runtime loop installs a task factory that attaches a done-callback to every
task the loop creates *except* the coroutines this module submitted itself
(those already deliver their exception to the calling thread through the
concurrent future — reporting them again is the duplicate traceback the incident
log showed). The callback retrieves the exception and logs one sanitized debug
line. On a failed `async_playwright().start()` the runtime additionally cancels
and gathers the tracked tasks with `return_exceptions=True`. Loop shutdown
cancels and drains pending tasks before closing.

Nothing is suppressed: there is no global asyncio exception handler, no text
filter on `Task exception was never retrieved`, and asyncio logging stays
enabled. Only supported Playwright API is used — no `site-packages` patching and
no private internals.

## Configuration

| Variable | Default | Range | Meaning |
| --- | --- | --- | --- |
| `AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES` | `2` | 1–8 | Concurrently active Playwright driver subprocesses. Invalid or out-of-range values are clamped and logged. |
| `AIRAHOST_BROWSER_RUNTIME_BREAKER_SECONDS` | `120` | 1–3600 | How long the fail-fast window stays open after a classified exhaustion. |
| `AIRBNB_PLAYWRIGHT_MAX_TABS` | `MAX_SCRAPER_WORKERS` | 1–8 | Unchanged. Now a per-endpoint budget rather than per client. |

### Migration notes

- **No config change is required.** Existing `CDP_URL` / `CDP_URLS` settings work
  as before.
- If you run **three** CDP browsers and want all three used, set
  `AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES=3`. With the default of 2 the pool logs
  `N CDP endpoints configured but AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES=2; using the
  first 2` and drops the extras.
- Pool *client* counts are unchanged (callers size their thread pools from
  `len(pool)`); only the driver count behind them changed. A pool whose only
  endpoint is unhealthy still shrinks to the healthy endpoints.
- Increasing the Windows page file remains an optional operator mitigation for a
  host under general memory pressure. It is not needed for correctness — the
  worker now bounds its own drivers and reports exhaustion as a typed failure.

## Optional Windows/CDP smoke test (deferred)

Not run: no authenticated local CDP browser was configured in this environment,
and the prompt forbids live/E2E runs without one. To run it, start Chrome with
`--remote-debugging-port=9222`, then repeatedly acquire and release logical
clients while watching `playwright.cmd`/`node.exe` process counts and
`playwright-async-runtime` thread counts. Expected: one driver per endpoint,
counts back to baseline after shutdown, a healthy browser fallback still works,
and no unretrieved-task warning or duplicated failure output in the log.
