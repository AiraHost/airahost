-- ============================================================
-- AiraHost — Simplify Admin Dashboard Access
-- Migration 026: Superseded 025's admin_users/admin-login model.
--                The admin dashboard has no login screen, so
--                auth.uid() is always null and the admin-gated
--                policy from 025 never actually granted access.
--
-- Reverts 025 (drops admin_users + its pricing_reports policy) and
-- replaces it with a policy that lets the anon key read all
-- pricing_reports rows, which is what the no-login dashboard
-- actually needs.
--
-- Safe to run even if 025 was never applied (all drops are IF EXISTS).
--
-- SECURITY NOTE: the anon key is embeddable in any browser. This
-- policy makes every user's report data (addresses, pricing,
-- generation stats) readable by anyone holding that key, not just
-- the admin dashboard. Only acceptable because the dashboard is
-- kept off the public internet (internal network / VPN / host-level
-- password protection).
-- ============================================================

DROP POLICY IF EXISTS "Admins can read all reports" ON pricing_reports;
DROP TABLE IF EXISTS admin_users;

CREATE POLICY "Anon can read all reports (admin dashboard)"
  ON pricing_reports FOR SELECT
  TO anon
  USING (true);
