"use client";

import { useCallback, useState } from "react";
import { Building2, FileText, RefreshCw, Timer } from "lucide-react";
import type { PricingReportRow } from "@/lib/adminReports";
import { Sidebar } from "./Sidebar";
import { KpiCard } from "./KpiCard";
import { ReportsTable } from "./ReportsTable";

export function AdminDashboard({
  initialReports,
  initialError,
}: {
  initialReports: PricingReportRow[];
  initialError: string | null;
}) {
  const [reports, setReports] = useState<PricingReportRow[]>(initialReports);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(initialError);

  const fetchReports = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/admin/reports", { cache: "no-store" });
      const body = (await response.json()) as {
        reports?: PricingReportRow[];
        error?: string;
      };
      if (!response.ok) {
        setError(body.error ?? `Request failed (${response.status})`);
      } else {
        setError(null);
        setReports(body.reports ?? []);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load reports.");
    } finally {
      setLoading(false);
    }
  }, []);

  const totalReports = reports.length;
  const readyReports = reports.filter((r) => r.status === "ready" && r.generation_time_ms !== null);
  const avgGenerationMs =
    readyReports.length > 0
      ? readyReports.reduce((sum, r) => sum + (r.generation_time_ms ?? 0), 0) / readyReports.length
      : null;
  const activeListings = new Set(reports.map((r) => r.input_address).filter(Boolean)).size;

  return (
    <div className="flex h-full bg-[radial-gradient(circle_at_top,_var(--color-base-900),_var(--color-base-950)_65%)]">
      <Sidebar />

      <main className="admin-scrollbar flex-1 overflow-y-auto p-8">
        <div className="mb-8 flex items-center justify-between">
          <div>
            <h1 className="font-display text-2xl font-semibold tracking-tight text-base-100">
              Overview
            </h1>
            <p className="mt-1 text-sm text-base-400">Pricing report activity across all users</p>
          </div>
          <button
            onClick={() => void fetchReports()}
            disabled={loading}
            className="admin-panel flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium text-base-200 transition-colors hover:text-base-100 disabled:opacity-50"
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
