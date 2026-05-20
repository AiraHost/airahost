# AriaHost Worker

Long-running Python process that polls Supabase for queued pricing reports and processes them locally.

## Architecture

```
Vercel (frontend)                    Local Machine (worker)
┌──────────────┐                     ┌──────────────────────┐
│ POST /api/   │  insert queued      │  python -m worker    │
│   reports    │ ──────────────►     │                      │
│              │   pricing_reports   │  poll → claim → run  │
│ GET /api/r/  │ ◄──────────────    │  → write results     │
│   {shareId}  │   read results      │                      │
└──────────────┘                     └──────────────────────┘
        │                                     │
        └──────────── Supabase DB ────────────┘
```

## Quick Start

### 1. Install dependencies

We recommend using the provided Conda environment file to keep dependencies cleanly isolated:

```powershell
conda env create -f ../conda_environment_mac.yml
conda activate airahost
playwright install chromium
```

On some macOS setups, Homebrew Python stays ahead of the Conda env in `PATH`
even after activation. If that happens, verify:

```powershell
which python
python -c "import sys; print(sys.executable)"
```

Expected output should point at:

```powershell
/opt/miniconda3/envs/airahost/bin/python
```

The `airahost` env now includes an activation hook that prepends
`$CONDA_PREFIX/bin` so the env Python wins consistently after
`conda activate airahost`.

### 2. Configure environment

```powershell
copy .env.example .env
# Edit .env with your Supabase credentials
```

Required variables:
| Variable | Description |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Service role key (**keep private!**) |
| `CDP_URL` | Chrome DevTools Protocol endpoint (default: `http://127.0.0.1:9222`) |

### 3. Run the Supabase migration

Apply `supabase/migrations/002_worker_queue.sql` to your Supabase database:
- Go to **Supabase Dashboard → SQL Editor** → paste and run the migration.

### 4. Start Chrome with CDP (for scraping mode)

The worker connects to your **locally running Chrome** via CDP so it can use your logged-in Airbnb session.

```powershell
# Close all Chrome windows first, then:
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:USERPROFILE\chrome-cdp-profile"
```

> After launching Chrome with CDP, log into Airbnb **once** in this profile. The session persists across restarts.

To verify CDP is running, open `http://127.0.0.1:9222/json` in another browser — you should see a JSON array.

### 5. Start the worker (manual)

```powershell
# From the repo root:
python -m worker.main
```

You should see:
```
2026-02-13 10:00:00 [INFO] worker: AriaHost Worker starting (version=worker-0.1.0)
2026-02-13 10:00:00 [INFO] worker:   poll=5s, stale=15min, max_attempts=3
```

Logs are written to **both** the console and `worker/logs/worker.log` (rotating, 5 MB x 5 files).

Press **Ctrl+C** to stop gracefully — the worker will finish the current job before exiting.

---

### 6. Run scraper E2E smoke test (interactive + nightly)

From repo root:

```powershell
python -m worker.e2e_scraper_smoke
```

Optional overrides:

```powershell
python -m worker.e2e_scraper_smoke --listing-url https://www.airbnb.ca/rooms/1408676238256636483 --nights 3
```

The command creates one synthetic interactive job and one synthetic nightly job,
runs both through the real worker dispatcher, and exits non-zero when scraper
health checks fail.

## Running as a Windows Service (24/7)

### Option A: NSSM (recommended)

[NSSM (Non-Sucking Service Manager)](https://nssm.cc/) wraps any executable as a proper Windows service with automatic restart, logging, and clean shutdown.

#### Step 1 — Install NSSM

Download from https://nssm.cc/download and extract. Add the folder containing `nssm.exe` to your `PATH`, or reference it directly.

Alternatively, if you have [Chocolatey](https://chocolatey.org/):
```powershell
choco install nssm
```

Or [Scoop](https://scoop.sh/):
```powershell
scoop install nssm
```

#### Step 2 — Find your Python path

```powershell
where python
# Example output: C:\Users\Spencer\AppData\Local\Programs\Python\Python312\python.exe
```

Note this full path — you'll need it below.

#### Step 3 — Register the service

```powershell
nssm install HostRevenueWorker
```

This opens a GUI. Fill in:

| Field | Value |
|---|---|
| **Path** | `C:\Users\Spencer\AppData\Local\Programs\Python\Python312\python.exe` (your full python path) |
| **Startup directory** | `C:\Users\Spencer\OneDrive\文件\GitHub\ariahost` (repo root) |
| **Arguments** | `-m worker.main` |

Then click the **Environment** tab and add (one per line):
```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...your-service-role-key
CDP_URL=http://127.0.0.1:9222
```

> Alternatively, skip the Environment tab if your `.env` file is already configured — the worker reads it automatically.

Click **Install service**.

#### Step 4 — Configure service recovery (optional but recommended)

```powershell
# Restart the service automatically if it crashes
nssm set HostRevenueWorker AppRestartDelay 5000

# Redirect stdout/stderr to log files managed by NSSM
nssm set HostRevenueWorker AppStdout C:\Users\Spencer\OneDrive\文件\GitHub\ariahost\worker\logs\nssm-stdout.log
nssm set HostRevenueWorker AppStderr C:\Users\Spencer\OneDrive\文件\GitHub\ariahost\worker\logs\nssm-stderr.log
nssm set HostRevenueWorker AppStdoutCreationDisposition 4
nssm set HostRevenueWorker AppStderrCreationDisposition 4
nssm set HostRevenueWorker AppRotateFiles 1
nssm set HostRevenueWorker AppRotateBytes 5242880
```

#### Step 5 — Start / Stop / Status

```powershell
# Start the worker
nssm start HostRevenueWorker

# Check status
nssm status HostRevenueWorker

# Stop gracefully (sends Ctrl+C, then waits)
nssm stop HostRevenueWorker

# Restart
nssm restart HostRevenueWorker

# View/edit configuration
nssm edit HostRevenueWorker

# Remove the service entirely
nssm remove HostRevenueWorker confirm
```

#### Step 6 — Verify it's running

Check the log:
```powershell
Get-Content worker\logs\worker.log -Tail 20
```

Or check Windows Services:
```powershell
Get-Service HostRevenueWorker
```

### Option B: Task Scheduler

If you prefer not to install NSSM:

1. Open **Task Scheduler** → Create Basic Task
2. Name: `HostRevenueWorker`
3. Trigger: **When the computer starts**
4. Action: **Start a program**
   - Program: full path to `python.exe`
   - Arguments: `-m worker.main`
   - Start in: `C:\Users\Spencer\OneDrive\文件\GitHub\ariahost`
5. Properties → check **Run whether user is logged on or not**
6. Settings → check **If the task fails, restart every 1 minute** (up to 3 retries)

---

## Logging

The worker writes logs to **two destinations** simultaneously:

| Destination | Details |
|---|---|
| **Console** (`stdout`) | Real-time output when running manually or via NSSM |
| **`worker/logs/worker.log`** | Rotating file: 5 MB per file, keeps 5 backups (`worker.log.1`, `.2`, etc.) |

Log format:
```
2026-02-13 10:05:23 [INFO] worker: Claimed job abc-123 (attempt 1)
2026-02-13 10:05:24 [INFO] worker: [abc-123] Mode 2 (mock): 742 Evergreen Terrace
2026-02-13 10:05:24 [INFO] worker: [abc-123] Completed in 45ms (mock-v1.0.0)
```

The `worker/logs/` directory is created automatically on first run.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_URL` | (required) | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | (required) | Service role key for DB access |
| `CDP_URL` | `http://127.0.0.1:9222` | Chrome DevTools Protocol URL |
| `WORKER_POLL_SECONDS` | `5` | Seconds between queue polls |
| `WORKER_STALE_MINUTES` | `15` | Minutes before a running job is considered stale |
| `WORKER_MAX_ATTEMPTS` | `3` | Max retry attempts per report |
| `WORKER_HEARTBEAT_SECONDS` | `10` | Heartbeat interval while processing |
| `WORKER_MAX_RUNTIME_SECONDS` | `180` | Hard timeout per job |
| `WORKER_VERSION` | `worker-0.1.0` | Version string for debug tracking |
| `MAX_SCROLL_ROUNDS` | `12` | Max scroll iterations when collecting comps |
| `MAX_CARDS` | `80` | Max comparable listings to collect |
| `SCRAPE_RATE_LIMIT_SECONDS` | `1.0` | Sleep between external requests |
| `DAY_QUERY_MAX_WORKERS` | `2` | Parallel workers for day-by-day scrape queries (min 1, max 8) |
| `BENCHMARK_DAY_QUERY_MAX_WORKERS` | `2` | Parallel workers for benchmark-first day queries (min 1, max 8) |
| `FIXED_POOL_MAX_WORKERS` | `3` | Parallel workers for fixed-pool setup anchor searches (min 1, max 8) |
| `FIXED_POOL_PAGES` | `6` | Search pages scanned per fixed-pool anchor before ranking by similarity |
| `FIXED_POOL_PER_ANCHOR` | `15` | Top-N most similar comps kept per fixed-pool anchor |
| `FIXED_POOL_GLOBAL_LIMIT` | `15` | Final fixed compset cap; higher-similarity comps replace the current lowest |
| `AIRBNB_DISABLE_MAP_SEARCH` | `0` | Set `1` to disable map search payload path |
| `AIRBNB_ENABLE_AI_SEARCH` | `0` | Set `1` to force `aiSearchEnabled=true` in search payload |

## Processing Modes

### Mode 1: Scrape (preferred)
When the report includes a `listingUrl` (via `input_listing_url` or `input_attributes.listingUrl`), the worker connects to Chrome via CDP and:
1. Opens the target listing → extracts specs (bedrooms, baths, location, etc.)
2. Searches nearby comparable listings
3. Scores comparables by similarity
4. Computes weighted median price → recommends nightly rate
5. Generates per-day calendar with discounts applied

### Mode 2: Mock (fallback)
When no listing URL is available (address + attributes only), the worker uses a deterministic hash-based algorithm to generate realistic pricing data. This keeps the product usable while real scraping isn't possible.

### Caching
Results are cached by a hash of (address + attributes + dates + discount policy). Cache TTL is 24 hours. Cache hits skip processing entirely.

---

## Running on Other Operating Systems

### macOS — launchd

Create `~/Library/LaunchAgents/com.ariahost.worker.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ariahost.worker</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>-m</string>
        <string>worker.main</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/ariahost</string>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/ariahost-worker.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ariahost-worker.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>SUPABASE_URL</key>
        <string>https://your-project.supabase.co</string>
        <key>SUPABASE_SERVICE_ROLE_KEY</key>
        <string>your-key</string>
    </dict>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.ariahost.worker.plist
launchctl start com.ariahost.worker
```

### Linux — systemd

Create `/etc/systemd/system/ariahost-worker.service`:
```ini
[Unit]
Description=AriaHost Pricing Worker
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/ariahost
ExecStart=/usr/bin/python3 -m worker.main
Restart=always
RestartSec=10
EnvironmentFile=/path/to/ariahost/worker/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable ariahost-worker
sudo systemctl start ariahost-worker
sudo journalctl -u ariahost-worker -f  # view logs
```

---

## Troubleshooting

### Worker can't connect to Supabase
- Verify `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in `.env`
- The service role key is different from the anon key — find it in **Supabase Dashboard → Settings → API**

### Scraper returns no results
- Ensure Chrome is running with `--remote-debugging-port=9222`
- Visit `http://127.0.0.1:9222/json` in a browser to verify CDP is active
- Log into Airbnb in the CDP Chrome profile
- Check if Airbnb is showing captchas (may need to solve manually in the CDP Chrome window)

### Jobs stuck in "running" state
- If a worker crashes, jobs are automatically reclaimed after `WORKER_STALE_MINUTES` (default 15 min)
- To manually reset:
  ```sql
  UPDATE pricing_reports SET status='queued', worker_claim_token=NULL WHERE status='running';
  ```

### Worker keeps retrying the same job
- Check `worker_attempts` column — after `WORKER_MAX_ATTEMPTS` (default 3), the job is marked as `error`
- Common causes: CDP not running, invalid listing URL, Airbnb blocking

### NSSM service won't start
- Run `nssm edit HostRevenueWorker` to verify the Python path and startup directory
- Check `worker/logs/nssm-stderr.log` for Python traceback errors
- Make sure all pip dependencies are installed for the same Python that NSSM is pointing to
- If using a venv, set Path to the venv's `python.exe` (e.g. `C:\...\venv\Scripts\python.exe`)

### Logs not appearing
- Check that `worker/logs/` directory exists (created automatically on first run)
- If running via NSSM, also check `worker/logs/nssm-stdout.log` and `nssm-stderr.log`

## Security

- `SUPABASE_SERVICE_ROLE_KEY` must **NEVER** be committed to git or exposed to the frontend
- The worker bypasses RLS by design — it needs to read/write all pricing reports
- Keep the `.env` file in `.gitignore` (already covered by the repo's `.env*` rule)
