# Memory Leak and Out-of-Memory (OOM) Analysis

This document provides a detailed investigation of the system crash, explaining why the PowerShell consoles closed, and identifying the memory leaks in the Python scraping worker codebase.

---

## 1. Why Did the PowerShell Windows Close?

When Windows reaches its system-wide **Commit Limit** (physical RAM + pagefile is fully exhausted):
1. **Memory Allocation Failures**: Operating system calls to allocate memory fail for all running processes.
2. **PowerShell and Python Crashes**: 
   - The Python interpreter does not recover gracefully from system-level memory allocation failures; it crashes immediately with a fatal crash or unhandled exception.
   - The PowerShell process itself (`powershell.exe`) is a .NET application. Under severe memory exhaustion, PowerShell fails to allocate memory for internal structures, stream management, or console hosting, causing it to crash and close the window.
3. **Cascading Exit**: Because the scraping tasks and Chrome were contesting for the remaining system memory, they all failed allocations and crashed simultaneously, closing the PowerShell windows.

---

## 2. Identified Memory Leaks in Python Code

Our code review revealed **critical resource and thread leaks** in the scraping/worker modules:

### Leak A: Catastrophic Thread and Client Leak in `capture_target_live_price` Fallback
In [target_extractor.py](file:///c:/Users/Aira/Desktop/airahost/worker/scraper/target_extractor.py), when the initial scraper fails to fetch listing prices, it executes a fallback path that retries using Playwright:

```python
# worker/scraper/target_extractor.py
if (_nightly is None and _total is None):
    try:
        if playwright_client is None:
            from worker.scraper.airbnb_client import AirbnbClient
            playwright_client = AirbnbClient(
                {
                    "CHECKIN": _checkin,
                    "CHECKOUT": _checkout,
                    "ADULTS": _adults,
                    "USE_DEEPBNB_BACKEND": False,
                }
            )
        # Scrape attempt...
```

* **The Issue**: A new `AirbnbClient` (which creates a new `PlaywrightScraper` object) is instantiated inside the local variable `playwright_client`. However, **`playwright_client.close_browser()` is never called** anywhere in this function, nor was there a `finally` block to guarantee cleanup.
* **Why it Leaks Threads/Memory**: 
  - When `PlaywrightScraper` is created, it spawns a background runtime thread (`playwright-async-runtime`) which runs a loop forever (`loop.run_forever()`).
  - Because this background thread's target is a method bound to the `PlaywrightScraper` instance (`self._run_loop_forever`), the active thread holds a strong reference to the scraper instance.
  - Python's Garbage Collector cannot clean up the `PlaywrightScraper` instance because of this circular reference. The custom destructor `__del__` is never called, leaking the active thread, the Playwright node driver process, and its associated heap memory.
  - If a 30-day report experiences a few day-query scraper failures, up to 30 unclosed browser clients and threads are spawned in a single report analysis, compounding over time.

---

### Leak B: Unclosed Local Client inside `capture_target_live_price`
Similarly, at the beginning of `capture_target_live_price` in [target_extractor.py](file:///c:/Users/Aira/Desktop/airahost/worker/scraper/target_extractor.py):

```python
if client is None:
    from worker.scraper.airbnb_client import AirbnbClient
    client = AirbnbClient(...)
```
If a caller runs this function without passing an existing client, a new client is instantiated but was never explicitly closed using `client.close_browser()`, causing the same loop/thread leak.

---

### Leak C: Tab Leaks in `airbnb_auto_login.py`
In [airbnb_auto_login.py](file:///c:/Users/Aira/Desktop/airahost/worker/airbnb_auto_login.py):
```python
page = context.new_page()
```
When running the auto-login flow on an existing CDP browser connection, a new page/tab is opened via `context.new_page()`. However, `page.close()` is never called in the function, leaving tabs open in the Chrome browser session.

---

## 3. Chrome-side DevTools Protocol (CDP) Memory Leaks

Even if the Python code perfectly closes every tab, Chrome DevTools Protocol (CDP) exhibits persistent memory leaks over long periods:
* **Debugging Overhead**: When Python attaches via CDP to a running Chrome instance and registers listeners (e.g. `page.on("response", ...)`), Chrome's internal DevTools agent caches network requests, console logs, and heap states.
* **Heap Accumulation**: Over thousands of automated page navigations on heavy SPAs like Airbnb, Chrome's memory usage grows continuously.
* **Outcome**: Eventually, Chrome's rendering engine hits its memory ceiling, resulting in the **"Aw, Snap! Out of Memory"** crash pages shown in your image.

---

## 4. Implemented Fixes

### Fix 1: Properly close client instances in `capture_target_live_price` (Implemented)
We have updated `capture_target_live_price` in [target_extractor.py](file:///c:/Users/Aira/Desktop/airahost/worker/scraper/target_extractor.py) to declare a local `_cleanup()` helper function. This helper is invoked at **every single exit and return point** of the function to safely close the locally created `client` and any `playwright_client` fallback instance.

```python
    locally_created_client = False
    playwright_client = None

    def _cleanup():
        if locally_created_client and client is not None:
            try:
                client.close_browser()
            except Exception:
                pass
        if playwright_client is not None:
            try:
                playwright_client.close_browser()
            except Exception:
                pass
```

---

### Fix 2: Schedule Periodic Chrome Restarts (Rolling Restarts)
To prevent the browser-side CDP cache leak, we have created a rolling restart script: [restart_chrome_rolling.ps1](file:///c:/Users/Aira/Desktop/airahost/scripts/restart_chrome_rolling.ps1).

This script performs the following steps:
1. Loops through your configured Chrome ports (`9222`, `9223`, `9224`).
2. Finds the specific running Chrome instance bound to that debugging port by inspecting process command lines.
3. Gracefully terminates only that instance.
4. Starts a new Chrome instance on the same port with its corresponding user data profile directory.
5. Waits 10 seconds for the newly started browser to bind to the port and warm up before proceeding to the next instance.

#### Preservation of Service Availability
Because Chrome takes less than 2 seconds to start, and we only restart **one instance at a time** with a delay, service is preserved:
- At any given moment, 2 out of the 3 instances remain fully active.
- If a worker is running a task and experiences a brief disconnect when its target port restarts, Playwright's built-in recovery mechanism will wait and seamlessly reconnect without failing the job.

#### Execution Setup via Windows Task Scheduler
To set this up:
1. Open **Windows Task Scheduler** on the host machine.
2. Create a daily task running at an off-peak hour (e.g. 3:00 AM).
3. Set the Action to **Start a program**:
   - **Program/script**: `powershell.exe`
   - **Arguments** (to execute script and redirect all output/errors to a log file):
     ```text
     -ExecutionPolicy Bypass -Command "& 'C:\Users\Aira\Desktop\airahost\scripts\restart_chrome_rolling.ps1' *>&1 | Out-File -FilePath 'C:\Users\Aira\Desktop\airahost\scripts\chrome_restart.log' -Encoding utf8"
     ```

#### Verification & Logging
To verify that the task runs and monitors correctly:

1. **Test the script manually**:
   - In Task Scheduler, select your task, right-click, and choose **Run**.
   - You should see your Chrome windows close one-by-one and restart minimized in the taskbar.
   - Open the log file at `C:\Users\Aira\Desktop\airahost\scripts\chrome_restart.log` to inspect the output.
   - **Log File Behavior**: The log file is **overwritten** (not appended) on every run, keeping only the latest restart log.
   - **Sample Log Output**:
     ```text
     =========================================
     Script Execution Time: 2026-06-23 02:15:00
     =========================================
     =========================================
     Restarting Chrome instance on port 9222...
     =========================================
     Found Chrome process (PID: 12344). Terminating...
     Starting Chrome on port 9222 with data-dir C:\Users\Aira\chrome-cdp-profile-9222...
     Waiting 10 seconds for Chrome port 9222 to bind and warm up...
     =========================================
     Restarting Chrome instance on port 9223...
     =========================================
     ...
     Rolling restart of all Chrome instances completed successfully!
     =========================================
     ```

2. **Verify Task Scheduler Execution via History**:
   - By default, Task Scheduler logging may be disabled. Open the Task Scheduler window, click **Task Scheduler (Local)** in the left panel, and select **Enable All Tasks History** in the right actions panel.
   - Once enabled, select your task and click the **History** tab in the center bottom panel. You will see a chronological list of executions, start times, stop times, and exit status codes (e.g., `0x0` indicating a successful exit).

3. **Check Windows Event Logs (Deeper Diagnostics)**:
   - Run the Windows **Event Viewer** (`eventvwr.msc`).
   - Navigate to: `Applications and Services Logs` -> `Microsoft` -> `Windows` -> `TaskScheduler` -> `Operational`.
   - Here you will find native system events logged for every scheduled trigger, start, stop, and failure details.


