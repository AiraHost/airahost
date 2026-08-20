# Implementation prompt: verify CDP ports 9222–9224 and stop the worker from launching replacement browsers

Implement, test, and document a production-safe fix for the worker opening a new browser even though the operator has already started three Chrome instances:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:USERPROFILE\chrome-cdp-profile-9222"
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9223 --user-data-dir="$env:USERPROFILE\chrome-cdp-profile-9223"
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9224 --user-data-dir="$env:USERPROFILE\chrome-cdp-profile-9224"
```

Work directly in this repository. Inspect the current code before changing it, reproduce the behavior deterministically, implement the smallest robust fix, and run focused regression tests. Preserve unrelated user changes in the worktree.

## Observed behavior and repository-specific root-cause hypothesis

Verify this hypothesis rather than accepting it blindly:

- `worker/main.py::_maybe_run_startup_auto_login()` resolves CDP endpoints and calls `_probe_airbnb_login_state()` for each one.
- A probe result of `None` is treated the same as a confirmed logged-out result and invokes `_run_startup_auto_login_for_cdp()`.
- `worker/airbnb_auto_login.py::run_login_flow()` catches every exception from `pw.chromium.connect_over_cdp(...)` and silently falls back to `pw.chromium.launch(...)`.
- Therefore a transient timeout, incorrect endpoint discovery, Playwright driver error, CDP protocol error, or unavailable port can open a new Playwright-managed browser. That new browser is not the operator's Chrome profile and can make it look as if the worker ignored ports 9222–9224.
- Separately, `scripts/run-local-stack.ps1` starts another Chrome on port 9222 unless `-SkipChrome` is passed. Determine whether this is also involved in the reported launch and make its preflight behavior safe and explicit.

Distinguish a Playwright driver helper subprocess from an actual Chrome/Chromium browser process. The worker may need the bounded Playwright driver described in `docs/playwright_runtime_ownership.md`; the bug is launching a replacement browser executable instead of attaching to the externally managed Chrome instances.

## Required behavior

### 1. Establish one canonical CDP configuration

Use the existing endpoint resolution in `worker/scraper/browser_runtime.py`; do not add a second incompatible parser.

- Support explicit `CDP_URLS=http://127.0.0.1:9222,http://127.0.0.1:9223,http://127.0.0.1:9224`.
- When only the default local `CDP_URL=http://127.0.0.1:9222` is set, retain discovery of healthy adjacent ports, but make the configured/discovered distinction observable.
- Normalize and deduplicate equivalent endpoint spellings.
- Ensure startup auth checks, auto-login, browser pools, and normal scraper work all consume the same resolved endpoint list.
- Do not mutate process-global `CDP_URL` to route concurrent work. Pass the endpoint explicitly through function arguments/configuration so one slot cannot race with another.

Document the recommended local configuration in `worker/.env.example` and `worker/README.md` without adding secrets:

```dotenv
CDP_URLS=http://127.0.0.1:9222,http://127.0.0.1:9223,http://127.0.0.1:9224
```

### 2. Add a bounded, diagnostic CDP preflight

Before startup authentication or scraping uses an endpoint, verify each configured port in two stages:

1. Request `http://127.0.0.1:<port>/json/version` with a bounded timeout. Require HTTP 200 and a valid JSON object containing a usable browser/WebSocket identity. Do not log the full WebSocket URL because it contains an opaque browser identifier.
2. Perform a real bounded `connect_over_cdp()` attach and confirm that a browser/context can be inspected. Disconnect cleanly without calling an API that terminates the operator-owned Chrome.

Return a typed result for every endpoint, for example `ready`, `tcp_or_http_unavailable`, `invalid_version_response`, `cdp_attach_failed`, and `ready_but_logged_out`. Keep transport readiness separate from Airbnb authentication state.

Log one concise summary such as:

```text
CDP preflight: 9222=ready 9223=ready 9224=cdp_attach_failed
```

For failures, include a sanitized reason and exception type at debug/diagnostic level. Never log cookies, credentials, tokens, full WebSocket debugger URLs, page HTML, or the complete environment.

Do not repeatedly start and stop independent Playwright runtimes merely to probe the same endpoint. Integrate with the repository's shared runtime/lease ownership where practical, or clearly prove that a short-lived startup probe is cleaned up and cannot multiply driver processes.

### 3. Never silently launch a replacement browser

Change `worker/airbnb_auto_login.py::run_login_flow()` so failure to attach to an externally managed CDP endpoint does **not** call `pw.chromium.launch()` by default.

- CDP attach failure must raise a typed, actionable error containing only the normalized host/port and a sanitized reason.
- Keep externally managed Chrome as the default and expected production/local-worker contract.
- If a standalone Playwright-launched browser is genuinely required for a separate developer tool, put it behind an explicit opt-in configuration whose default is false (for example `AIRAHOST_ALLOW_BROWSER_LAUNCH=false`). Do not enable that flag from the worker startup path.
- If opt-in launch remains, make ownership explicit and close only a browser the current function created. Never close the operator-owned CDP Chrome.
- Do not catch `Exception` around CDP attachment and convert every failure into launch. Preserve exception chaining and distinguish endpoint unavailability from login failure.

### 4. Correct startup auth decision-making

Refactor the startup sequence so these states have different outcomes:

- **CDP ready and logged in:** use the endpoint.
- **CDP ready and confirmed logged out:** auto-login may run against that same endpoint, then re-probe it.
- **CDP unavailable or attach failed:** do not run auto-login and do not launch a browser. Mark that endpoint unhealthy and emit an actionable message telling the operator to verify the Chrome command/profile/port.
- **Some endpoints healthy:** start with the healthy subset and preserve multi-endpoint distribution. Do not route slots to failed endpoints.
- **All explicitly configured endpoints unhealthy:** fail worker startup once with a nonzero exit and a concise error listing ports 9222–9224 and reason codes. Do not enter the queue loop and do not create a cold client that retries indefinitely.

Decide and document behavior when an endpoint is reachable but logged out and credentials are missing. It must fail or degrade explicitly; it must not manufacture a fresh browser profile.

Ensure startup checks happen before the worker claims a report job. A CDP configuration failure must not turn queued jobs into misleading empty-search, Airbnb-challenge, or generic parsing failures.

### 5. Make `run-local-stack.ps1` idempotent

When `-SkipChrome` is not provided, probe port 9222 before calling `Start-Process`:

- If a valid CDP endpoint is already listening, reuse it and print that it was detected.
- If the port is occupied by a non-CDP service, fail with an actionable error rather than launching Chrome into a conflict.
- If no CDP endpoint is listening, retain the script's existing behavior of starting Chrome, then wait for `/json/version` with a bounded deadline and fail clearly if readiness never arrives.
- Preserve `-SkipChrome`; when it is used, print that Chrome launch is intentionally skipped and let the worker's three-port preflight be authoritative.

Do not terminate, restart, or modify any existing Chrome process as part of this fix.

## Files to inspect and likely modify

- `worker/main.py`
- `worker/airbnb_auto_login.py`
- `worker/scraper/browser_runtime.py`
- `worker/scraper/playwright_runtime.py`
- `worker/scraper/scraper_errors.py`
- `scripts/run-local-stack.ps1`
- `worker/.env.example`
- `worker/README.md`
- focused tests under `worker/tests/`

Also search all production worker paths for `chromium.launch`, `launch_persistent_context`, `subprocess.Popen`, `Start-Process`, and direct Chrome executable invocation. Classify each occurrence as production, development utility, or test. No production worker fallback may launch a browser implicitly.

## Required deterministic tests

Tests must not require live Airbnb or actually launch Chrome. Use local fake HTTP responses and Playwright fakes/mocks. Add regression coverage proving:

1. Ports 9222, 9223, and 9224 with valid `/json/version` responses and successful CDP attaches are all reported ready.
2. An HTTP 200 response that is not valid CDP version JSON is rejected.
3. HTTP success followed by `connect_over_cdp` failure is classified as `cdp_attach_failed`.
4. A failed CDP attachment never calls `chromium.launch()` under default configuration.
5. A failed probe (`None`/unavailable) does not invoke auto-login.
6. A confirmed logged-out endpoint invokes auto-login only against that exact endpoint and is re-verified afterward.
7. Endpoint routing does not depend on temporary mutation of `os.environ["CDP_URL"]`.
8. One failed endpoint plus two healthy endpoints starts the worker with only the healthy endpoints.
9. All explicitly configured endpoints failing causes one startup failure before queue polling, with no browser launch and no retry storm.
10. Probe/attach exceptions are sanitized and retain their exception chain.
11. Disconnecting the probe or auto-login does not terminate externally managed Chrome.
12. The PowerShell launcher does not call `Start-Process` when a valid CDP endpoint already exists.
13. Existing endpoint discovery, normalization, shared-runtime, pool distribution, and browser recovery tests remain green.

Avoid timing-only tests and arbitrary sleeps. Use explicit events/counters and bounded timeouts.

## Optional local smoke validation

If ports 9222–9224 are already available, run a read-only validation before and after implementation:

```powershell
9222..9224 | ForEach-Object {
  try {
    $v = Invoke-RestMethod -Uri "http://127.0.0.1:$_/json/version" -TimeoutSec 2
    [pscustomobject]@{ Port = $_; Ready = $true; Browser = $v.Browser }
  } catch {
    [pscustomobject]@{ Port = $_; Ready = $false; Browser = $null }
  }
}
```

Then perform a non-destructive Playwright CDP attach to every ready endpoint, open/close a temporary page if safe, and disconnect. Record whether each stage succeeded. Do not navigate away from, close, restart, or alter the operator's existing tabs and browsers. Do not run this smoke test when the ports are unavailable; automated tests remain mandatory.

## Suggested verification commands

Adapt test module names to the implementation:

```powershell
python -m pytest worker/tests/test_cdp_preflight.py worker/tests/test_periodic_airbnb_auth_check.py worker/tests/test_browser_runtime_pool.py -q
python -m pytest worker/tests -q -m "not live and not e2e"
```

Run the repository's formatter/linter/type checks for modified Python and PowerShell files if configured. Report exact commands, pass/fail counts, skips, and any live CDP validation intentionally deferred.

## Acceptance criteria

- The worker proves it can connect to configured ports 9222–9224 before claiming work.
- Readiness and Airbnb login state are separate, explicit states.
- A connection failure never silently launches Chrome/Chromium under default configuration.
- Healthy endpoints remain usable when one port is bad; total endpoint failure stops startup cleanly and once.
- Auto-login operates only on an already attachable external browser and never changes endpoint through a global-environment race.
- The local stack script reuses an existing valid CDP browser instead of opening another one.
- No operator-owned browser is closed or restarted.
- Logs identify the failing port and safe reason code without leaking secrets.
- Existing scraper behavior, shared runtime limits, endpoint distribution, and cleanup remain intact.

## Deliverable

Provide:

- implementation and regression tests;
- a concise root-cause report identifying which path launched the new browser;
- the final CDP startup/ownership contract;
- configuration and migration notes;
- files changed;
- exact test/lint results;
- per-port live smoke results, or an explicit statement that live validation was deferred.
