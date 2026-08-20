export type ReportStatus = "queued" | "ready" | "error";

export interface CompsSummary {
  collected?: number;
  afterFiltering?: number;
  usedForPricing?: number;
}

export interface ResultSummary {
  compsSummary?: CompsSummary;
  comparableListings?: unknown[];
  [key: string]: unknown;
}

export interface PricingReportRow {
  id: string;
  created_at: string;
  input_address: string | null;
  status: ReportStatus;
  generation_time_ms: number | null;
  result_summary: ResultSummary | null;
}

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
