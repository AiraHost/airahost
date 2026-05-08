# AiraHost

> AI Revenue Advisor for Airbnb hosts.
> Understand your market. Price smarter. Earn more.

## Quick Start

```bash
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

The demo report (`/r/demo`) uses a deterministic pricing engine. Real reports require a running Python worker with Chrome CDP.

## One-command Local Nightly + ML Flow

For local end-to-end testing, use the stack launcher from the repo root:

```powershell
.\run_local_stack.cmd -ForceNightly
```

This starts:

- Chrome CDP on port `9222`
- Next.js dev server
- nightly Python worker (`WORKER_ENV=local`, `WORKER_LANE=nightly`)
- ML sidecar worker
- a data-quality monitor window
- one forced nightly job for the default Airbnb room `1305899249107196055`

The default nightly target can be changed without editing code:

```powershell
.\run_local_stack.cmd -AirbnbRoomId <airbnb_room_id> -ForceNightly
.\run_local_stack.cmd -ListingId <saved_listing_uuid> -ForceNightly
```

Optional switches:

```powershell
.\run_local_stack.cmd -ForceNightly -SkipDataQuality
.\run_local_stack.cmd -ForceNightly -SkipChrome
.\run_local_stack.cmd -SkipSchedule
```

PowerShell requires the `.\` prefix for local `.cmd` files. Use `.\run_local_stack.cmd`, not `run_local_stack.cmd`.

## Setup Supabase

1. Copy `.env.example` to `.env.local` and fill in your Supabase credentials
2. Run `supabase/migrations/001_initial.sql` against your database
3. Run `supabase/migrations/002_worker_queue.sql` to add worker queue + cache tables
4. Run `supabase/migrations/003_saved_listings.sql` to add saved listings + listing history tables
5. In Supabase Auth settings, set Site URL and redirect URL to include `http://localhost:3000/auth/callback`
6. Restart the dev server

For local development, remember that there are two separate environment settings:

- Worker environment: set `WORKER_ENV=local` in `worker/.env`
- Next.js app environment: set `WORKER_TARGET_ENV=local` in `.env.local`

## Setup Worker

The worker scrapes Airbnb via Playwright CDP for real pricing data.

1. Start Chrome with remote debugging:
- Windows
```powershel
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:USERPROFILE\chrome-cdp-profile"
```
- MacOS
```
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-cdp-profile"
```
2. Copy `worker/.env.example` to `worker/.env` and fill in credentials
3. Install Python dependencies: `pip install -r worker/requirements.txt`
4. Run: `python -m worker.main`

See `worker/README.md` for 24/7 operation via NSSM.

## Setup ML Sidecar Worker

ML forecasts are queued inside the existing `pricing_reports.result_summary`
payload and processed by a separate Python process, so the deployed frontend
does not need Python or local access to the ML machine.

No database migration is required for the local ML queue mode.

1. Install ML dependencies: `pip install -r ml_sidecar/requirements.txt`
2. Configure `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in `.env`, `.env.local`, or `ml_sidecar/.env`
3. Run the sidecar worker on the backend/ML machine:

```powershell
python -m ml_sidecar.worker
# or
run_ml_sidecar_worker.cmd
```

The dashboard's ML button marks the latest ready report as
`result_summary.mlForecast.status = "queued"`. The sidecar worker claims that
payload, runs `ml_sidecar.batch_pipeline`, and writes the result back to
`pricing_reports.result_summary.mlForecast`.

## Nightly Data Quality Checks

The local stack launcher automatically starts a data-quality monitor after it schedules a nightly report. The monitor waits for that specific report to become `ready`, then runs:

```powershell
python -m ml_sidecar.data_quality --report-id <pricing_report_id> --wait-ready
```

Outputs:

```text
ml_sidecar/reports/data_quality_latest.json
ml_sidecar/reports/data_quality_issues_latest.csv
```

Open the issue CSV when a run looks suspicious:

```powershell
notepad .\ml_sidecar\reports\data_quality_issues_latest.csv
```

The checker validates:

- `pricing_reports.result_calendar` was ingested into `market_price_observations`
- stay dates match between the report and observations
- calendar prices match observation prices
- required ML target and raw fields are present
- the training dataframe has required ML columns
- the feature matrix has no missing numeric features, NaN, or non-finite values

If the JSON status is `pass`, the latest nightly data is considered safe enough for retraining. If it is `fail`, inspect the CSV before trusting the model output.

## Nightly Automatic ML Retraining

Nightly runs now automatically queue an ML forecast after the report is completed and market observations are ingested. This does not require a new database table.

The nightly worker writes this into the existing report JSON:

```json
{
  "mlForecast": {
    "status": "queued",
    "trainingScope": "global",
    "forceRetrain": true,
    "queuedBy": "nightly_worker"
  }
}
```

The ML sidecar worker polls existing ready reports for `result_summary.mlForecast.status = "queued"`, retrains with the latest `market_price_observations`, writes artifacts under `ml_sidecar/reports/`, then updates `mlForecast.status` to `ready` or `error`.

Controls:

```env
NIGHTLY_QUEUE_ML_FORECAST=1
NIGHTLY_ML_FORCE_RETRAIN=1
NIGHTLY_ML_TRAINING_SCOPE=global
ML_SIDECAR_FORCE_RETRAIN=1
```

For split-machine deployment, run these separately:

```powershell
# Frontend machine
npm run start

# Scrape worker machine
python -m worker.main

# ML machine
python -m ml_sidecar.worker
```

The machines communicate only through Supabase. The frontend never directly starts Python in production.

## Vercel Deployment

Add these environment variables in the Vercel dashboard:

- `NEXT_PUBLIC_SUPABASE_URL`
- `NEXT_PUBLIC_SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`

## Project Structure

```
src/
  app/               # Next.js App Router pages + API routes
  components/        # Shared UI components
  core/              # Deterministic pricing engine (demo reports only)
  lib/               # Schemas, Supabase client, utilities
worker/
  main.py            # Long-running Python worker (polls Supabase queue)
  scraper/           # Playwright CDP-based Airbnb scraper
    target_extractor.py   # Extract listing specs from Airbnb pages
    comparable_collector.py # Collect comparable listings from search
    day_query.py          # Day-by-day 1-night price queries
    price_estimator.py    # Orchestrates scraping pipeline
  core/              # Discount calc, caching, DB helpers, pricing engine
supabase/
  migrations/        # SQL migration files (001, 002, 003)
docs/
  PROJECT_MEMORY.md  # Full project context for development
  openapi.yaml       # API specification
```

## Architecture

```
Frontend (Vercel)                  Local Worker (Python)
┌──────────────┐                   ┌──────────────────────┐
│ POST /api/   │  queued job       │  python -m worker    │
│   reports    │ ──────────────►  │                      │
│              │  pricing_reports  │  poll → claim → run  │
│ GET /api/r/  │ ◄──────────────  │  → write results     │
│   {shareId}  │  read results     │                      │
└──────────────┘                   └──────────────────────┘
        │                                   │
        └────────── Supabase DB ────────────┘
```

The frontend creates reports as `queued` jobs. A local Python worker polls the queue, scrapes Airbnb via Chrome CDP (day-by-day 1-night queries for accurate nightly prices), and writes results back to Supabase. If scraping fails, the job is marked as `error` with a user-facing message.

## Scraping Pipeline

1. **Target extraction** -- Navigate to the listing URL, extract specs (location, bedrooms, amenities, etc.) from DOM, JSON-LD, meta tags, and breadcrumbs
2. **Day-by-day queries** -- For each night in the date range, query Airbnb search with 1-night stays to get accurate nightly prices (not inflated total-trip prices)
3. **Comparable filtering** -- Filter search results by similarity to the target listing (property type, capacity, amenities)
4. **Price recommendation** -- Weighted median from top-K similar comparables per day
5. **Interpolation** -- For sampled ranges (>14 nights), interpolate unqueried days from nearest anchors
6. **Discount application** -- Apply weekly/monthly/non-refundable discounts per the user's policy

## Pages

| Route          | Description                       |
| -------------- | --------------------------------- |
| `/`            | Landing page                      |
| `/tool`        | Multi-step listing analysis form  |
| `/r/{shareId}` | Shareable revenue report          |
| `/r/demo`      | Seeded demo report                |
| `/login`       | Email/password auth               |
| `/dashboard`   | Saved listings + report history   |

## API

| Method | Path                         | Description                  |
| ------ | ---------------------------- | ---------------------------- |
| POST   | `/api/reports`               | Create a pricing report      |
| GET    | `/api/reports/{id}`          | Get report by ID             |
| GET    | `/api/r/{shareId}`           | Get report by share link     |
| GET    | `/api/listings`              | Get current user's listings  |
| POST   | `/api/listings`              | Create a saved listing       |
| GET    | `/api/listings/{id}`         | Get listing + linked reports |
| PATCH  | `/api/listings/{id}`         | Update saved listing         |
| DELETE | `/api/listings/{id}`         | Delete saved listing         |
| POST   | `/api/listings/{id}/rerun`   | Re-run queued analysis       |
| POST   | `/api/track-market`          | Subscribe to market alerts   |

See `docs/openapi.yaml` for full API specification.

## Tech Stack

- **Frontend:** Next.js 16 (App Router), TypeScript, Tailwind CSS v4, Zod
- **Database:** Supabase (PostgreSQL + RLS + Auth)
- **Worker:** Python 3.14, Playwright CDP, Supabase client
- **Deployment:** Frontend on Vercel, worker on local Windows machine via NSSM
