# Admin Dashboard — Usage Guide

The admin dashboard is a standalone app (`admin-dashboard/`) for viewing pricing report
activity (volume, generation time, comparables found) across all users. It is separate
from the main Next.js site and the Python worker — it only reads from Supabase.

There is no login screen — opening the app goes straight to the dashboard.

## One-time setup

### 1. Run the new migrations

Apply these in order, after `023_listing_structural_cache.sql`:

- `supabase/migrations/024_report_generation_time.sql` — adds `generation_time_ms` to `pricing_reports`, populated by the worker when a report completes.
- `supabase/migrations/026_open_reports_read_drop_admin_users.sql` — lets the (login-free) dashboard read all `pricing_reports` rows with the anon key.

(`025_admin_access.sql` added an admin-login/`admin_users` model that's no longer used —
026 drops it. You can skip 025 entirely on a fresh setup; 026 is safe to run whether or not
025 was ever applied.)

**If you don't have the Supabase CLI linked to this project** (no `supabase/config.toml`,
no `DATABASE_URL`), the fastest way to apply them is to paste the SQL directly into
Supabase Studio:

1. Open your Supabase project → **SQL Editor** → New query
2. Paste and run:

   ```sql
   -- Migration 024
   ALTER TABLE pricing_reports
     ADD COLUMN IF NOT EXISTS generation_time_ms integer;

   -- Migration 026
   DROP POLICY IF EXISTS "Admins can read all reports" ON pricing_reports;
   DROP TABLE IF EXISTS admin_users;

   CREATE POLICY "Anon can read all reports (admin dashboard)"
     ON pricing_reports FOR SELECT
     TO anon
     USING (true);
   ```

If you do have the CLI linked, `supabase db push` (or running the migration files directly
against `DATABASE_URL` via `psql`) works the same way.

> **Troubleshooting — "We encountered an issue processing your report. Please try again."**
> If this starts happening right after this feature was added, and the worker log shows the
> report actually finished computing (a `Completed in <N>ms` line or similar), migration 024
> hasn't been applied yet. The worker writes `generation_time_ms` on every completed report;
> if the column doesn't exist, Supabase rejects the write and the worker reports it as a
> generic processing failure even though pricing was computed successfully.
>
> **Troubleshooting — dashboard loads but the table/KPIs are empty.** If Supabase shows
> `generation_time_ms` populated on recent reports but the dashboard shows nothing, migration
> 026 hasn't been applied. Without it, `pricing_reports` RLS blocks the dashboard's
> unauthenticated anon-key requests (`auth.uid()` is null with no login), so every query
> returns zero rows with no error.

### 2. Configure environment variables

```bash
cd admin-dashboard
cp .env.example .env
```

Fill in `.env` with your Supabase project's URL and anon key (Project Settings → API in
Supabase Studio).

### 3. Read access — how it works

Migration 026 adds a policy letting the anon key read every row of `pricing_reports`
(the dashboard has no login, so `auth.uid()` is always null — it can't rely on
`auth.uid() = user_id` like the main site does).

> **Security tradeoff:** the anon key is meant to be embeddable in any browser, so this
> policy makes every user's report data (addresses, pricing, generation stats) readable by
> anyone holding that key — not just this dashboard. Only acceptable because the dashboard
> is kept off the public internet (internal network, VPN, host-level password protection,
> etc.). If it ever needs to be public, replace this with a small authenticated API that uses
> `SUPABASE_SERVICE_ROLE_KEY` server-side instead of querying Supabase directly from the
> browser.

## Running it

```bash
cd admin-dashboard
npm install   # first time only
npm run dev
```

Open the printed local URL (e.g. `http://localhost:5173`) — the dashboard loads directly.

## What you'll see

- **Total Reports Generated** — count of all `pricing_reports` rows.
- **Average Generation Time** — average `generation_time_ms` across `ready` reports. Reports
  created before migration 024 won't have this field and are excluded from the average.
- **Active Listings** — count of distinct `input_address` values.
- **Reports table** — most recent 200 reports, each with listing address, comparables found,
  generation time, status, and creation time. Use the **Refresh** button to re-fetch.

## Deploying

`admin-dashboard` is a plain Vite app (`npm run build` → static `dist/`). Since there's no
login gate, only deploy it somewhere access-controlled at the network/host level (internal
network, VPN, password-protected hosting) — anyone who can load the page can see all report
data. Set `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` as build-time environment variables
on whatever host you use.
