# Robust CDP Connection and Self-Healing Chrome Scraper

Implement self-healing mechanisms and fast responsiveness checks to prevent long hangs and failures when connecting Playwright to Chrome via the Chrome DevTools Protocol (CDP).

## Problem & Root Cause

1. **Unresponsive/Frozen Chrome (Ports 9222 and 9224 - `Timeout 15000ms exceeded`)**: 
   Under high system memory usage (only 1.2 GB virtual memory is currently free out of 64 GB on the host), the system is thrashing pagefile storage. Chrome's network/WebSocket thread establishes the TCP connection, but the main browser thread is stuck/frozen. Playwright waits for the handshake, and since there is no response, it blocks for the full 15 seconds per port before timing out.
2. **Crashed/Not Running Chrome (Port 9223 - `connect ECONNREFUSED`)**:
   Chrome on port 9223 was completely crashed/not running, refusing connections immediately.
3. **No Recovery**:
   Because all three candidates (9222, 9223, 9224) failed, the scraper raised a `RuntimeError`. Since `_is_recoverable_browser_failure()` didn't classify connection failures as recoverable, the worker immediately failed the job instead of attempting to reset the Playwright session, self-heal, or retry.

## User Review Required

> [!NOTE]
> The auto-restart mechanism will use WMIC/PowerShell queries to identify and terminate dead or unresponsive Chrome processes bound to local remote-debugging ports, and then restart them in a minimized window using the standard executable path. This only runs for local addresses (e.g., `127.0.0.1`, `localhost`).

## Proposed Changes

### Scraping Worker

#### [MODIFY] [playwright_scraper.py](file:///c:/Users/Aira/Desktop/airahost/worker/scraper/playwright_scraper.py)

1. **Import `socket` and `subprocess`**:
   Add imports at the top of the file.

2. **Add `_is_port_open`, `_is_responsive_cdp`, and `_restart_local_chrome` helpers**:
   - `_is_port_open(host, port)`: Quick socket connection test (0.5s timeout) to see if port is open.
   - `_is_responsive_cdp(cdp_url)`: Perform a fast HTTP GET request on `/json/version` (1.0s timeout) to verify that Chrome's remote debugging is responsive.
   - `_restart_local_chrome(cdp_url)`: Detect local endpoints, query/terminate unresponsive Chrome processes on the port using WMIC/PowerShell, start a fresh Chrome instance minimized, and poll `/json/version` up to 5 seconds for readiness.

3. **Update `_ensure_browser()`**:
   - Before attempting `connect_over_cdp`, check the candidate URL with `_is_responsive_cdp`.
   - If not responsive, trigger `_restart_local_chrome`.
   - If it becomes ready, or was already responsive, proceed to `connect_over_cdp`.
   - If a candidate is completely dead and fails to restart, log a warning and immediately skip to the next candidate without waiting for the 15-second Playwright timeout.

4. **Update `_is_recoverable_browser_failure()`**:
   - Add `"connect_over_cdp"`, `"could not connect to any cdp endpoint"`, `"econnrefused"`, and `"timeout 15000ms exceeded"` to the markers tuple.
   - This ensures connection failures trigger session resets and retries, allowing the scraper to self-heal.

---

## Verification Plan

### Automated Tests
- Run `python -m pytest worker/tests/test_playwright_browser_recovery.py` to ensure general recovery tests pass.
- We will write a manual verification script or check live status to test connection behavior.

### Manual Verification
1. Kill one of the active Chrome processes (e.g., port 9223).
2. Run a scraper test or trigger a manual pricing task.
3. Verify in logs that the scraper detects port 9223 is dead, runs the auto-restart command, successfully connects to the fresh instance, and completes the task.
