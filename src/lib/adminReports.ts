export type AdminReportStatus = "queued" | "ready" | "error";

export interface CompsSummary {
  collected?: number;
  afterFiltering?: number;
  usedForPricing?: number;
}

export interface AdminResultSummary {
  compsSummary?: CompsSummary;
  comparableListings?: unknown[];
  [key: string]: unknown;
}

export interface PricingReportRow {
  id: string;
  created_at: string;
  input_address: string | null;
  status: AdminReportStatus;
  generation_time_ms: number | null;
  result_summary: AdminResultSummary | null;
}

/** Columns the admin dashboard reads from pricing_reports. */
export const ADMIN_REPORT_COLUMNS =
  "id, created_at, input_address, status, generation_time_ms, result_summary";

/** Most recent reports shown in the table. */
export const ADMIN_REPORT_LIMIT = 200;

/** Best-effort comparables count from whichever field the report actually populated. */
export function comparablesFound(row: PricingReportRow): number | null {
  const summary = row.result_summary;
  if (!summary) return null;
  const comps = summary.compsSummary;
  if (comps?.usedForPricing !== undefined) return comps.usedForPricing;
  if (comps?.collected !== undefined) return comps.collected;
  if (Array.isArray(summary.comparableListings)) return summary.comparableListings.length;
  return null;
}
