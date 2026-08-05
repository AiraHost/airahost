# Admin Dashboard — Usage Guide

The admin dashboard lives at **`/admin`** in the main Next.js app (e.g.
`http://localhost:3000/admin`) and shows pricing report activity (volume, generation time,
comparables found) across all users. It reads Supabase server-side with the service role
key, so it sees every user's reports regardless of RLS.

Access is gated by a single shared password held in the `ADMIN_PASSWORD` environment
variable. Visiting `/admin` without a valid session redirects to `/admin/login`.

## One-time setup

### 1. Run the migrations

Apply these in order, after `023_listing_structural_cache.sql`:

- `supabase/migrations/024_report_generation_time.sql` — adds `generation_time_ms` to `pricing_reports`, populated by the worker when a report completes.
- `supabase/migrations/027_close_anon_reports_read.sql` — drops the anon read policy that the old standalone dashboard needed.

(`025_admin_access.sql` added an admin-login/`admin_users` model that is no longer used, and
`026_open_reports_read_drop_admin_users.sql` replaced it with an anon read policy for the
old login-free dashboard. On a fresh setup you can skip both 025 and 026; 027 is safe to run
whether or not they were applied.)

**If you don't have the Supabase CLI linked to this project** (no `supabase/config.toml`,
no `DATABASE_URL`), the fastest way to apply them is to paste the SQL directly into
Supabase Studio:

1. Open your Supabase project → **SQL Editor** → New query
2. Paste and run:

   ```sql
   -- Migration 024
   ALTER TABLE pricing_reports
     ADD COLUMN IF NOT EXISTS generation_time_ms integer;

   -- Migration 027
   DROP POLICY IF EXISTS "Anon can read all reports (admin dashboard)" ON pricing_reports;
   ```

If you do have the CLI linked, `supabase db push` (or running the migration files directly
against `DATABASE_URL` via `psql`) works the same way.

> **Troubleshooting — "We encountered an issue processing your report. Please try again."**
> If this starts happening right after this feature was added, and the worker log shows the
> report actually finished computing (a `Completed in <N>ms` line or similar), migration 024
> hasn't been applied yet. The worker writes `generation_time_ms` on every completed report;
> if the column doesn't exist, Supabase rejects the write and the worker reports it as a
> generic processing failure even though pricing was computed successfully.

### 2. Configure environment variables

In the Next.js `.env` (and in your host's environment for deployments):

```bash
ADMIN_PASSWORD=<a long random password>
```

The dashboard also needs `NEXT_PUBLIC_SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`, which
the app already requires. Restart the Next.js server after changing `ADMIN_PASSWORD` —
changing it also invalidates every existing admin session, since sessions are signed with it.

### 3. Read access — how it works

`/admin` and `GET /api/admin/reports` run server-side and query Supabase with
`SUPABASE_SERVICE_ROLE_KEY`, which bypasses RLS. The service role key is never sent to the
browser, and no anon-key policy on `pricing_reports` is required (migration 027 removes the
one the old dashboard used).

Sign-in posts the password to `POST /api/admin/login`, which compares it to `ADMIN_PASSWORD`
in constant time and sets an httpOnly, SameSite=Lax cookie holding an HMAC-signed token
(signed with `ADMIN_PASSWORD`, valid 12 hours). It is `Secure` in production.

> **Security note:** this is a single shared password with no per-user accounts, rate
> limiting, or audit trail. Use a long random value, don't reuse it elsewhere, and rotate it
> by changing the env var.

## Running it

```bash
npm run dev
```

Open `http://localhost:3000/admin` and enter `ADMIN_PASSWORD`.

## What you'll see

- **Total Reports Generated** — count of all `pricing_reports` rows.
- **Average Generation Time** — average `generation_time_ms` across `ready` reports. Reports
  created before migration 024 won't have this field and are excluded from the average.
- **Active Listings** — count of distinct `input_address` values.
- **Reports table** — most recent 200 reports, each with listing address, comparables found,
  generation time, status, and creation time. Use the **Refresh** button to re-fetch.

## Deploying

Nothing separate to deploy — `/admin` ships with the Next.js app. Set `ADMIN_PASSWORD` in
the host's environment (e.g. Vercel project settings) alongside the existing Supabase
variables. The page is marked `noindex`.
