-- ============================================================
-- AiraHost — Report Generation Time
-- Migration 024: Add generation_time_ms to pricing_reports so
--                the admin dashboard can surface how long each
--                report took to generate.
--
-- Populated by the worker's complete_job() when a report is
-- marked ready. NULL for reports completed before this migration.
-- ============================================================

ALTER TABLE pricing_reports
  ADD COLUMN IF NOT EXISTS generation_time_ms integer;
