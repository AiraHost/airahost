# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# General Rules
These rules apply to every task in this project unless explicitly overridden.
Bias: caution over speed on non-trivial work. Use judgment on trivial tasks.

## Rule 1 — Think Before Coding
State assumptions explicitly. If uncertain, ask rather than guess.
Present multiple interpretations when ambiguity exists.
Push back when a simpler approach exists.
Stop when confused. Name what's unclear.

## Rule 2 — Simplicity First
Minimum code that solves the problem. Nothing speculative.
No features beyond what was asked. No abstractions for single-use code.
Test: would a senior engineer say this is overcomplicated? If yes, simplify.

## Rule 3 — Surgical Changes
Touch only what you must. Clean up only your own mess.
Don't "improve" adjacent code, comments, or formatting.
Don't refactor what isn't broken. Match existing style.

## Rule 4 — Goal-Driven Execution
Define success criteria. Loop until verified.
Don't follow steps. Define success and iterate.
Strong success criteria let you loop independently.

## Rule 5 — Use the model only for judgment calls
Use me for: classification, drafting, summarization, extraction.
Do NOT use me for: routing, retries, deterministic transforms.
If code can answer, code answers.

## Rule 6 — Token budgets are not advisory
Per-task: 4,000 tokens. Per-session: 30,000 tokens.
If approaching budget, summarize and start fresh.
Surface the breach. Do not silently overrun.

## Rule 7 — Surface conflicts, don't average them
If two patterns contradict, pick one (more recent / more tested).
Explain why. Flag the other for cleanup.
Don't blend conflicting patterns.

## Rule 8 — Read before you write
Before adding code, read exports, immediate callers, shared utilities.
"Looks orthogonal" is dangerous. If unsure why code is structured a way, ask.

## Rule 9 — Tests verify intent, not just behavior
Tests must encode WHY behavior matters, not just WHAT it does.
A test that can't fail when business logic changes is wrong.

## Rule 10 — Checkpoint after every significant step
Summarize what was done, what's verified, what's left.
Don't continue from a state you can't describe back.
If you lose track, stop and restate.

## Rule 11 — Match the codebase's conventions, even if you disagree
Conformance > taste inside the codebase.
If you genuinely think a convention is harmful, surface it. Don't fork silently.

## Rule 12 — Fail loud
"Completed" is wrong if anything was skipped silently.
"Tests pass" is wrong if any were skipped.
Default to surfacing uncertainty, not hiding it.


## What This Is

AiraHost is an AI revenue advisor for Airbnb hosts. The system has three independent processes that communicate only through Supabase:

1. **Next.js frontend** (Vercel) — creates `queued` pricing report jobs, serves reports
2. **Python scrape worker** — polls Supabase, scrapes Airbnb via Chrome CDP, writes results
3. **Python ML sidecar** — polls for `mlForecast.status = "queued"` in ready reports and runs forecasts

## Common Commands

### Frontend (Next.js)
```bash
npm install
npm run dev          # dev server at localhost:3000
npm run build
npm run lint
npm run test:e2e     # Playwright browser tests
```

### Full Local Stack (Windows PowerShell)
```powershell
.\run_local_stack.cmd -ForceNightly          # Start all services + force one nightly job
.\run_local_stack.cmd -AllNightly            # All eligible listings, respects dedup window
.\run_local_stack.cmd -ForceAllNightly       # All eligible listings, bypass dedup
.\run_local_stack.cmd -SkipSchedule          # Start services only, no job scheduling
.\run_local_stack.cmd -ForceNightly -SkipDataQuality
.\run_local_stack.cmd -ForceNightly -SkipChrome  # Chrome CDP already running
```

### Python Worker
```bash
python -m worker              # Start the scrape worker (polls Supabase queue)
python -m ml_sidecar.worker   # Start the ML sidecar worker
```

### Python Tests
```bash
python -m unittest worker.tests.test_dynamic_pricing   # Run a specific test module
python -m unittest discover -s worker/tests            # Run all worker tests
```

### Data Quality & ML
```bash
python -m ml_sidecar.data_quality --report-id <id> --wait-ready
python .\ml_sidecar\quality_report_html.py --source supabase
python .\ml_sidecar\quality_report_html.py --source supabase --no-snapshot
npm run worker:e2e:scraper   # Scraper smoke test (requires Chrome CDP on :9222)
```

## Environment Setup

Two separate `.env` files are required:

- `worker/.env` (copy from `worker/.env.example`) — Python worker; set `WORKER_ENV=local` for local dev
- `.env.local` (copy from `.env.example`) — Next.js; set `WORKER_TARGET_ENV=local` for local dev

Chrome must be running with remote debugging for real scraping:
```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:USERPROFILE\chrome-cdp-profile"
```

Supabase migrations must be run in order: `001_initial.sql` → `002_worker_queue.sql` → `003_saved_listings.sql` → `004_user_pricing_preferences.sql` → `005_listing_date_defaults_and_report_link.sql`.

## Architecture

### Database Schema (Supabase)
- `pricing_reports` — job queue and results (`status`: `queued` | `ready` | `error`); `result_summary` and `result_calendar` are JSONB payloads written by the scrape worker
- `saved_listings` — user's saved Airbnb listings with linked report history
- `market_price_observations` — ingested calendar data from nightly scrapes, used as ML training data
- `market_tracking_preferences` — market alert subscriptions

### Worker Scraping Pipeline (`worker/scraper/`)
1. `target_extractor.py` — extracts listing specs (location, bedrooms, amenities) from Airbnb PDP via DOM, JSON-LD, meta tags
2. `day_query.py` — day-by-day 1-night Airbnb search queries for accurate nightly prices (avoids inflated total-trip prices)
3. `comp_collection.py` + `parsers.py` — HTTP search parsing and comparable mapping
4. `similarity.py` — scores and filters comparables by property type, capacity, amenities
5. `pricing_engine.py` — weighted median from top-K similar comparables per day
6. Interpolation for sampled ranges (>14 nights), then discount application

The scrape worker uses `WORKER_LANE` to distinguish `interactive` (user-triggered) from `nightly` (scheduled) jobs. Nightly jobs auto-queue an ML forecast after completion by setting `result_summary.mlForecast.status = "queued"`.

### Key Worker Modules (`worker/core/`)
- `db.py` — Supabase client (service role key, bypasses RLS); contains `_load_supabase_client_symbols` workaround to prevent the local `supabase/` folder from shadowing the installed package
- `dynamic_pricing.py` — time-based and demand-based price multipliers
- `discounts.py` — weekly/monthly/non-refundable discount application
- `similarity.py` — comp scoring algorithm
- `nightly_strategy.py` / `report_policy.py` — nightly scheduling logic
- `concurrent_runner.py` — threaded day-query concurrency

### Frontend API Routes (`src/app/api/`)
- `POST /api/reports` — creates a `queued` pricing report job
- `GET /api/r/[shareId]` — fetches a report by share link (public)
- `GET /api/reports/[id]` — fetches by ID (authenticated)
- `GET/POST/PATCH/DELETE /api/listings/[id]` — saved listing CRUD
- `POST /api/listings/[id]/rerun` — re-queues a job
- `GET /api/internal/nightly/schedule` — internal nightly scheduler endpoint

### Frontend Lib (`src/lib/`)
- `schemas.ts` — Zod schemas for `ListingInput`, `DiscountPolicy`, `ReportSummary`, `CalendarDay` — the canonical type definitions shared between frontend and API
- `supabaseServer.ts` / `supabase.ts` — server vs. client Supabase instances
- `reportPolicy.ts` — mirrors Python `report_policy.py` for nightly scheduling rules

### Demo vs. Real Reports
- `/r/demo` uses `src/core/pricingCore.ts` — a deterministic mock engine with no Supabase or Python dependency
- All real reports require the Python worker to be running with Chrome CDP

### ML Sidecar (`ml_sidecar/`)
- `worker.py` — polls `pricing_reports` for `mlForecast.status = "queued"`, runs `batch_pipeline.py`
- `data_quality.py` — validates nightly ingestion: calendar prices match observations, ML feature columns are populated, no NaN/non-finite values
- `quality_report_html.py` — generates `ml_sidecar/reports/nightly_quality_report.html`; use `--source supabase` for live data, `--source artifacts` for local files

## Auth

Supabase Auth with email/password. The Next.js middleware (`src/middleware.ts`) redirects unauthenticated users away from `/dashboard` and `/profile`. Reports are RLS-protected; the worker uses `SUPABASE_SERVICE_ROLE_KEY` to bypass RLS.
