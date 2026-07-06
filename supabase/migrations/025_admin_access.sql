-- ============================================================
-- AiraHost — Admin Dashboard Access
-- Migration 025: Add admin_users table + RLS policy so a flagged
--                admin account can read all pricing_reports rows
--                (not just their own) for the admin dashboard.
--
-- To grant admin access, insert the user's auth.users id:
--   insert into admin_users (user_id) values ('<uuid>');
-- ============================================================

CREATE TABLE IF NOT EXISTS admin_users (
  user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE admin_users ENABLE ROW LEVEL SECURITY;

-- Admins can read all pricing_reports rows (in addition to the existing
-- "Users can read own reports" policy from 001_initial.sql — Postgres RLS
-- policies are OR'd together).
CREATE POLICY "Admins can read all reports"
  ON pricing_reports FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM admin_users WHERE user_id = auth.uid()
    )
  );

-- Admins can read their own admin_users row (needed so the dashboard can
-- check "am I an admin?" after login).
CREATE POLICY "Admins can read own admin_users row"
  ON admin_users FOR SELECT
  USING (auth.uid() = user_id);
