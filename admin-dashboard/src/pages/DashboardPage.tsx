import { useEffect, useState, useCallback } from "react";
import { Building2, FileText, RefreshCw, Timer } from "lucide-react";
import { supabase } from "../lib/supabase";
import type { PricingReportRow } from "../lib/types";
import { Sidebar } from "../components/Sidebar";
import { KpiCard } from "../components/KpiCard";
import { ReportsTable } from "../components/ReportsTable";

export function DashboardPage() {
  const [reports, setReports] = useState<PricingReportRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadReports = useCallback(async () => {
    const { data, error: fetchError } = await supabase
      .from("pricing_reports")
      .select("id, created_at, input_address, status, generation_time_ms, result_summary")
      .order("created_at", { ascending: false })
      .limit(200);

    return {
      data: (data as PricingReportRow[] | null) ?? [],
      errorMessage: fetchError ? fetchError.message : null,
    };
  }, []);

  const refresh = useCallback(() => {
    setLoading(true);
    void loadReports().then(({ data, errorMessage }) => {
      setReports(data);
      setError(errorMessage);
      setLoading(false);
    });
  }, [loadReports]);

  useEffect(() => {
    void loadReports().then(({ data, errorMessage }) => {
      setReports(data);
      setError(errorMessage);
      setLoading(false);
    });
  }, [loadReports]);

  const totalReports = reports.length;
  const readyReports = reports.filter((r) => r.status === "ready" && r.generation_time_ms !== null);
  const avgGenerationMs =
    readyReports.length > 0
      ? readyReports.reduce((sum, r) => sum + (r.generation_time_ms ?? 0), 0) / readyReports.length
      : null;
  const activeListings = new Set(reports.map((r) => r.input_address).filter(Boolean)).size;

  return (
    <div className="flex min-h-screen bg-[radial-gradient(circle_at_top,_var(--color-base-900),_var(--color-base-950)_65%)]">
      <Sidebar />

      <main className="scrollbar-thin flex-1 overflow-y-auto p-8">
        <div className="mb-8 flex items-center justify-between">
          <div>
            <h1 className="font-display text-2xl font-semibold tracking-tight text-base-100">
              Overview
            </h1>
            <p className="mt-1 text-sm text-base-400">Pricing report activity across all users</p>
          </div>
          <button
            onClick={refresh}
            disabled={loading}
            className="glass-panel flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium text-base-200 transition-colors hover:text-base-100 disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>

        {error && (
          <div className="mb-6 rounded-xl border border-danger-500/30 bg-danger-500/10 px-4 py-3 text-sm text-danger-400">
            {error}
          </div>
        )}

        <div className="mb-8 grid grid-cols-1 gap-5 sm:grid-cols-3">
          <KpiCard
            label="Total Reports Generated"
            value={totalReports.toLocaleString()}
            icon={<FileText className="h-5 w-5" />}
            accent="brand"
          />
          <KpiCard
            label="Average Generation Time"
            value={avgGenerationMs !== null ? `${(avgGenerationMs / 1000).toFixed(2)}s` : "—"}
            icon={<Timer className="h-5 w-5" />}
            accent="accent"
          />
          <KpiCard
            label="Active Listings"
            value={activeListings.toLocaleString()}
            icon={<Building2 className="h-5 w-5" />}
            accent="success"
          />
        </div>

        <ReportsTable reports={reports} />
      </main>
    </div>
  );
}
