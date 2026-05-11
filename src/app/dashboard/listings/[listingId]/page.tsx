"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { Card } from "@/components/Card";
import { Button } from "@/components/Button";
import { MlForecastCard } from "@/components/dashboard/MlForecastCard";
import { PricingHeatmap } from "@/components/dashboard/PricingHeatmap";
import { PriceLineChart } from "@/components/dashboard/PriceLineChart";
import type { MlForecastRun } from "@/lib/mlSidecar";
import { getSupabaseBrowser } from "@/lib/supabase";
import type { RecommendedPrice, CalendarDay } from "@/lib/schemas";


type ReportSnapshot = {
  id: string;
  share_id: string;
  status: "queued" | "running" | "ready" | "error";
  // Only live_analysis is selected as source-of-truth; forecast_snapshot rows are ignored.
  report_type?: "live_analysis";
  created_at: string;
  completed_at?: string | null;
  market_captured_at?: string | null;
  input_date_start: string;
  input_date_end: string;
  result_summary: {
    nightlyMedian?: number;
    recommendedPrice?: RecommendedPrice;
    comparableListings?: Array<unknown> | null;
    compsSummary?: { usedForPricing?: number } | null;
    benchmarkInfo?: {
      benchmarkUsed?: boolean | null;
      benchmarkFetchStatus?: string | null;
      benchmarkMismatchLevel?: string | null;
      conflictDetected?: boolean | null;
      fetchStats?: {
        totalDays?: number;
        highConfidenceDays?: number;
        mediumConfidenceDays?: number;
        lowConfidenceDays?: number;
      } | null;
    } | null;
  } | null;
  result_calendar?: CalendarDay[];
  error_message?: string | null;
};

type HistoryRow = {
  id: string;
  trigger: "manual" | "rerun" | "scheduled" | string;
  created_at: string;
  pricing_reports: ReportSnapshot | ReportSnapshot[] | null;
};

type PreferredComp = {
  listingUrl: string;
  name?: string;
  note?: string;
  enabled: boolean;
};

type ListingDetail = {
  id: string;
  name: string;
  input_address: string;
  input_attributes?: { preferredComps?: PreferredComp[] | null; listingUrl?: string | null };
};

type StatusFilter = "all" | "ready" | "error";

export default function ListingHistoryPage() {
  const { listingId } = useParams<{ listingId: string }>();
  const router = useRouter();

  const [listing, setListing] = useState<ListingDetail | null>(null);
  const [rows, setRows] = useState<HistoryRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [mlRun, setMlRun] = useState<MlForecastRun | null>(null);
  const [mlError, setMlError] = useState<string | null>(null);
  const [mlLoading, setMlLoading] = useState(false);
  const [isRunningMl, setIsRunningMl] = useState(false);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  // Today's date â€” used as the minimum selectable date for custom analysis (today-or-future rule).
  const todayStr = new Date().toISOString().split("T")[0];
  const [customStart, setCustomStart] = useState(() => new Date().toISOString().split("T")[0]);
  const [customEnd, setCustomEnd] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() + 29); // today start + 29 days end = 30 inclusive days
    return d.toISOString().split("T")[0];
  });
  const [isRunningCustom, setIsRunningCustom] = useState(false);
  // Max end date = start + 29 days (30 inclusive days total).
  const maxCustomEnd = customStart
    ? (() => {
        const d = new Date(customStart + "T12:00:00Z");
        d.setUTCDate(d.getUTCDate() + 29);
        return d.toISOString().split("T")[0];
      })()
    : "";
  // True when the selected range exceeds 30 inclusive days.
  const customRangeInvalid =
    !!(customStart && customEnd) &&
    Math.round(
      (new Date(customEnd + "T12:00:00Z").getTime() -
        new Date(customStart + "T12:00:00Z").getTime()) /
        86400000
    ) +
      1 >
      30;

  // Preferred comps state (list)
  const [showPinnedComps, setShowPinnedComps] = useState(false);
  const [pinnedCompsList, setPinnedCompsList] = useState<{ listingUrl: string; note: string }[]>([]);
  const [pinnedCompSaving, setPinnedCompSaving] = useState(false);
  const [pinnedCompMsg, setPinnedCompMsg] = useState("");

  const loadMlRun = useCallback(async () => {
    const mlRes = await fetch(`/api/listings/${listingId}/ml`, {
      cache: "no-store",
    });
    const mlData = await mlRes.json().catch(() => ({}));
    if (!mlRes.ok) {
      setMlError(
        (mlData as { details?: string; error?: string }).details ??
          (mlData as { error?: string }).error ??
          "ML forecast is unavailable."
      );
      return null;
    }

    const run = (mlData as { run?: MlForecastRun | null }).run ?? null;
    setMlRun(run);
    setMlError(null);
    return run;
  }, [listingId]);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      // Verify auth
      const supabase = getSupabaseBrowser();
      const {
        data: { user },
      } = await supabase.auth.getUser();
      if (!user) {
        router.push("/login");
        return;
      }

      // Fetch listing details, reports, and latest ML run in parallel.
      const [listingRes, reportsRes, mlRes] = await Promise.all([
        fetch(`/api/listings/${listingId}`),
        fetch(`/api/listings/${listingId}/reports`),
        fetch(`/api/listings/${listingId}/ml`),
      ]);

      if (listingRes.status === 401 || reportsRes.status === 401 || mlRes.status === 401) {
        router.push("/login");
        return;
      }
      if (listingRes.status === 404) {
        setError("Listing not found.");
        setLoading(false);
        return;
      }

      const listingData = await listingRes.json();
      const reportsData = await reportsRes.json();
      const mlData = await mlRes.json().catch(() => ({}));

      const loadedListing: ListingDetail | null = listingData.listing ?? null;
      setListing(loadedListing);
      setRows(reportsData.reports ?? []);
      setMlRun((mlData as { run?: MlForecastRun | null }).run ?? null);
      setMlError(
        !mlRes.ok
          ? ((mlData as { details?: string; error?: string }).details ??
              (mlData as { error?: string }).error ??
              "ML forecast is unavailable.")
          : null
      );
      // Pre-fill preferred comps from saved listing
      const savedComps = loadedListing?.input_attributes?.preferredComps;
      if (savedComps?.length) {
        setPinnedCompsList(savedComps.map((c) => ({ listingUrl: c.listingUrl, note: c.note ?? "" })));
      } else {
        setPinnedCompsList([]);
      }
    } catch {
      setError("Failed to load listing history.");
    } finally {
      setLoading(false);
    }
  }, [listingId, router]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    if (mlRun?.status !== "queued" && mlRun?.status !== "running") {
      return;
    }

    const interval = window.setInterval(() => {
      void loadMlRun();
    }, 5000);

    return () => window.clearInterval(interval);
  }, [loadMlRun, mlRun?.status]);

  // Normalize report relation (can be array or object from Supabase join)
  function getReport(row: HistoryRow): ReportSnapshot | null {
    const r = row.pricing_reports;
    if (!r) return null;
    return Array.isArray(r) ? r[0] ?? null : r;
  }

  const filteredRows = useMemo(() => {
    if (statusFilter === "all") return rows;
    return rows.filter((row) => {
      const report = getReport(row);
      return report?.status === statusFilter;
    });
  }, [rows, statusFilter]);

  async function handleRunMlForecast() {
    setIsRunningMl(true);
    setMlLoading(true);
    setMlError(null);

    try {
      const res = await fetch(`/api/listings/${listingId}/ml`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trainingScope: "global" }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(
          (data as { details?: string; error?: string }).details ??
            (data as { error?: string }).error ??
            "Failed to run ML forecast."
        );
      }

      setMlRun((data as { run?: MlForecastRun | null }).run ?? null);
      void loadMlRun();
    } catch (runError) {
      setMlError(
        runError instanceof Error
          ? runError.message
          : "Failed to run ML forecast."
      );
    } finally {
      setIsRunningMl(false);
      setMlLoading(false);
    }
  }

  async function handleRunCustomAnalysis() {
    // Today-or-future guard: reject past start dates.
    if (!customStart || !customEnd) return;
    if (customStart < todayStr) return;
    if (customEnd < customStart) return;
    // 30-day limit
    const days =
      Math.round(
        (new Date(customEnd + "T12:00:00Z").getTime() -
          new Date(customStart + "T12:00:00Z").getTime()) /
          86400000
      ) + 1;
    if (days > 30) return;
    setIsRunningCustom(true);
    try {
      const res = await fetch(`/api/listings/${listingId}/rerun`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dates: { startDate: customStart, endDate: customEnd } }),
      });
      if (!res.ok) throw new Error("Failed");
      const data = await res.json();
      if (data.shareId) {
        router.push(`/r/${data.shareId}`);
      } else {
        await loadData();
      }
    } catch {
      // silently fail; user can retry
    } finally {
      setIsRunningCustom(false);
    }
  }

  // Board source-of-truth: latest scheduled nightly ready report ONLY.
  // manual / rerun / custom reports are history only â€” never shown on the board.
  const latestReadyRow = useMemo(() => {
    for (const row of rows) {
      const report = getReport(row);
      if (report?.status === "ready" && row.trigger === "scheduled") {
        return { row, report };
      }
    }
    return null;
  }, [rows]);

  async function handleSavePinnedComps() {
    const valid = pinnedCompsList.filter((c) => c.listingUrl.includes("airbnb.com/rooms/"));
    setPinnedCompSaving(true);
    setPinnedCompMsg("");
    try {
      const res = await fetch(`/api/listings/${listingId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          preferredComps: valid.map((c) => ({
            listingUrl: c.listingUrl.trim(),
            note: c.note.trim() || undefined,
            enabled: true,
          })),
        }),
      });
      if (!res.ok) throw new Error("Failed to save");
      setPinnedCompMsg(`Saved ${valid.length} comparable${valid.length !== 1 ? "s" : ""}. Future nightly updates and custom analyses will use these.`);
      setShowPinnedComps(false);
      await loadData();
    } catch {
      setPinnedCompMsg("Failed to save. Please try again.");
    } finally {
      setPinnedCompSaving(false);
    }
  }

  async function handleRemovePinnedComps() {
    setPinnedCompSaving(true);
    setPinnedCompMsg("");
    try {
      const res = await fetch(`/api/listings/${listingId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preferredComps: null }),
      });
      if (!res.ok) throw new Error("Failed");
      setPinnedCompsList([]);
      setPinnedCompMsg("");
      setShowPinnedComps(false);
      await loadData();
    } catch {
      setPinnedCompMsg("Failed to remove. Please try again.");
    } finally {
      setPinnedCompSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-10">
        <p className="text-sm text-muted">Loading...</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6 px-6 py-10">

      {/* â”€â”€ Header â”€â”€ */}
      <div>
        <Link href="/dashboard" className="text-sm text-muted hover:text-foreground">
          Back to Dashboard
        </Link>
        <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="text-2xl font-bold tracking-tight">
            {listing?.name ?? "Listing"}
          </h1>
          {(() => {
            const url = listing?.input_attributes?.listingUrl;
            const isValid = !!(url?.includes("airbnb.com/rooms/"));
            if (isValid) {
              return (
                <a
                  href={url!}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-sm font-medium text-foreground/35 hover:text-foreground/65 transition-colors"
                >
                  Open on Airbnb
                </a>
              );
            }
            return (
              <span className="text-sm text-foreground/25">
                No Airbnb URL set
              </span>
            );
          })()}
        </div>
        {listing?.input_address && (
          <p className="mt-0.5 text-sm text-muted">{listing.input_address}</p>
        )}
      </div>

      {error && (
        <Card>
          <p className="text-sm text-warning">{error}</p>
        </Card>
      )}

      {/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
          Section 1: Current Market Board
      â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */}
      <div className="space-y-4">
        <div className="flex items-center gap-2.5">
          <p className="text-[11px] font-semibold uppercase tracking-widest text-foreground/35">
            Nightly Market Report
          </p>
          {latestReadyRow && (
            <span className="rounded-full bg-teal-50 px-2 py-0.5 text-[9px] font-semibold text-teal-700">
              Nightly
            </span>
          )}
        </div>

        {latestReadyRow ? (
          <>
            {/* Pricing intelligence strip */}
            {(() => {
              const r = latestReadyRow.report;
              const s = r.result_summary;
              const observed = (s as { observedListingPrice?: number | null } | null)?.observedListingPrice ?? null;
              const suggested = s?.recommendedPrice?.nightly ?? null;
              const median = s?.nightlyMedian ?? null;
              const livePriceStatus = (s as { livePriceStatus?: string | null } | null)?.livePriceStatus ?? null;
              const obsVsMktDiffPct = (s as { observedVsMarketDiffPct?: number | null } | null)?.observedVsMarketDiffPct ?? null;
              const pricingAction = (s as { pricingAction?: string | null } | null)?.pricingAction ?? null;
              const pricingActionTarget = (s as { pricingActionTarget?: number | null } | null)?.pricingActionTarget ?? null;

              return (
                <div className="rounded-2xl border border-border bg-white px-6 py-5 space-y-3">
                  <div className="flex flex-wrap items-start gap-6">
                    {observed != null ? (
                      <div>
                        <p className="text-[10px] font-semibold uppercase tracking-widest text-foreground/30">Your live price</p>
                        <p className="mt-0.5 text-3xl font-bold tracking-tight text-foreground">${observed}</p>
                        {obsVsMktDiffPct != null && (
                          <p className={`mt-0.5 text-xs font-semibold ${Math.abs(obsVsMktDiffPct) <= 3 ? "text-gray-500" : obsVsMktDiffPct > 0 ? "text-amber-600" : "text-emerald-600"}`}>
                            {Math.abs(obsVsMktDiffPct) <= 3 ? "At market" : `${Math.abs(obsVsMktDiffPct)}% ${obsVsMktDiffPct > 0 ? "above" : "below"} market`}
                          </p>
                        )}
                      </div>
                    ) : (
                      <div>
                        <p className="text-[10px] font-semibold uppercase tracking-widest text-foreground/30">Suggested nightly</p>
                        <p className="mt-0.5 text-3xl font-bold tracking-tight text-foreground">{suggested != null ? `$${suggested}` : "-"}</p>
                      </div>
                    )}
                    {median != null && (
                      <div className="border-l border-gray-100 pl-5">
                        <p className="text-[10px] font-semibold uppercase tracking-widest text-foreground/30">Market median</p>
                        <p className="mt-0.5 text-lg font-semibold text-foreground/60">${median}</p>
                      </div>
                    )}
                    {observed != null && suggested != null && observed !== suggested && (
                      <div className="border-l border-gray-100 pl-5">
                        <p className="text-[10px] font-semibold uppercase tracking-widest text-foreground/30">Recommended</p>
                        <p className="mt-0.5 text-lg font-semibold text-foreground/60">${suggested}</p>
                      </div>
                    )}
                  </div>

                  {/* Pricing action */}
                  {pricingAction && pricingAction !== "keep" && pricingActionTarget && (
                    <div className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-semibold ${pricingAction === "raise" ? "border-blue-200 bg-blue-50 text-blue-700" : "border-amber-200 bg-amber-50 text-amber-800"}`}>
                      {pricingAction === "raise" ? `Raise to $${pricingActionTarget}` : `Lower to $${pricingActionTarget}`}
                    </div>
                  )}

                  {/* Live price unavailable note */}
                  {observed == null && livePriceStatus === "no_listing_url" && (
                    <p className="text-xs text-foreground/40">
                      Add your Airbnb listing URL in settings to compare your live price to the market.
                    </p>
                  )}
                  {observed == null && livePriceStatus && livePriceStatus !== "no_listing_url" && livePriceStatus !== "captured" && (
                    <p className="text-xs text-foreground/40">
                      Live price not available for this report ({livePriceStatus}).
                    </p>
                  )}

                  <Link
                    href={`/r/${latestReadyRow.report.share_id}`}
                    className="inline-block text-xs font-semibold text-accent hover:underline"
                  >
                    View full report
                  </Link>
                </div>
              );
            })()}

            {/* Price line chart */}
            {latestReadyRow.report.result_calendar &&
              latestReadyRow.report.result_calendar.length > 1 && (
                <PriceLineChart
                  calendar={latestReadyRow.report.result_calendar}
                  observedListingPrice={
                    (latestReadyRow.report.result_summary as { observedListingPrice?: number | null } | null)
                      ?.observedListingPrice ?? null
                  }
                />
              )}

            {/* 30-day pricing calendar */}
            {latestReadyRow.report.result_calendar &&
              latestReadyRow.report.result_calendar.length > 0 && (
                <PricingHeatmap
                  calendar={latestReadyRow.report.result_calendar}
                  observedListingPrice={
                    (latestReadyRow.report.result_summary as { observedListingPrice?: number | null } | null)
                      ?.observedListingPrice ?? null
                  }
                  observedListingPriceDate={
                    (latestReadyRow.report.result_summary as { observedListingPriceDate?: string | null } | null)
                      ?.observedListingPriceDate ?? null
                  }
                />
              )}

          </>
        ) : (() => {
          const hasListingUrl = !!(listing?.input_attributes?.listingUrl?.includes("airbnb.com/rooms/"));
          return hasListingUrl ? (
            <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-border bg-white px-8 py-12 text-center">
              <p className="text-sm font-medium text-foreground/50">No nightly report yet</p>
              <p className="mt-1 text-xs text-foreground/35">
                This board updates nightly. Custom analyses are saved to history below and don&apos;t replace this board.
              </p>
            </div>
          ) : (
            <div className="rounded-2xl border border-amber-100 bg-amber-50/50 px-6 py-5">
              <p className="text-sm font-semibold text-amber-800">Nightly tracking not active</p>
              <p className="mt-1 text-xs text-amber-700/80">
                This listing doesn&apos;t have an Airbnb listing URL, so daily market updates are not scheduled.
                You can still run a <strong>Custom Analysis</strong> below at any time.
              </p>
              <p className="mt-2 text-xs text-amber-600/70">
                To enable nightly tracking, add your Airbnb listing URL in the listing settings on the dashboard.
              </p>
            </div>
          );
        })()}
      </div>

      {/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
          Section 2: Run Custom Analysis
      â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */}
      <div className="space-y-3">
        <p className="text-[11px] font-semibold uppercase tracking-widest text-foreground/35">
          Run Custom Analysis
        </p>
        <div className="rounded-2xl border border-border bg-white p-5 sm:p-6">
          <p className="mb-0.5 text-sm font-semibold text-foreground/80">
            Custom live analysis
          </p>
          <p className="mb-4 text-xs text-foreground/50">
            Scrapes fresh Airbnb market data for a future date range you choose. Results open as a full report.
          </p>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
            <label className="flex-1 space-y-1.5">
              <span className="block text-xs font-medium text-foreground/50">Start date</span>
              <input
                type="date"
                value={customStart}
                min={todayStr}
                onChange={(e) => setCustomStart(e.target.value)}
                className="w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2 text-sm outline-none focus:border-gray-300 focus:bg-white"
              />
            </label>
            <label className="flex-1 space-y-1.5">
              <span className="block text-xs font-medium text-foreground/50">End date</span>
              <input
                type="date"
                value={customEnd}
                min={customStart || todayStr}
                max={maxCustomEnd}
                onChange={(e) => setCustomEnd(e.target.value)}
                className="w-full rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2 text-sm outline-none focus:border-gray-300 focus:bg-white"
              />
            </label>
            <button
              type="button"
              disabled={isRunningCustom || !customStart || !customEnd || customStart < todayStr || customRangeInvalid}
              onClick={handleRunCustomAnalysis}
              className="shrink-0 rounded-xl bg-gray-900 px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-gray-800 disabled:opacity-40"
            >
              {isRunningCustom ? "Starting..." : "Run analysis"}
            </button>
          </div>
          {customRangeInvalid && (
            <p className="mt-2 text-xs text-amber-700">
              Select a date range of 30 days or less.
            </p>
          )}
        </div>
      </div>

      <div className="space-y-3">
        <p className="text-[11px] font-semibold uppercase tracking-widest text-foreground/35">
          Experimental ML Forecast
        </p>
        <MlForecastCard
          run={mlRun}
          loading={mlLoading}
          running={isRunningMl}
          error={mlError}
          onRun={handleRunMlForecast}
        />
      </div>

      {/* Section 3: Benchmark / Pinned comps */}
      <div className="space-y-3">
        <p className="text-[11px] font-semibold uppercase tracking-widest text-foreground/35">
          Benchmark Settings
        </p>
      {/* Benchmark / Pinned comps card */}
      {(() => {
        const currentComps = listing?.input_attributes?.preferredComps ?? [];
        const hasBenchmark = currentComps.length > 0;
        // Show edit form immediately when no benchmark is set; otherwise toggle
        const editOpen = !hasBenchmark || showPinnedComps;
        return (
          <Card
            className={hasBenchmark ? "border-amber-200 bg-amber-50/30" : "border-amber-100 bg-amber-50/20"}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-semibold">
                    {hasBenchmark ? "Pricing anchored to your benchmark" : "Benchmark listing"}
                  </p>
                  {hasBenchmark && (
                    <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
                      Active
                    </span>
                  )}
                  {!hasBenchmark && (
                    <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
                      Recommended
                    </span>
                  )}
                </div>
                {hasBenchmark && !showPinnedComps && (
                  <div className="mt-1 space-y-1">
                    {currentComps.map((comp, idx) => (
                      <p key={idx} className="truncate text-xs text-accent">
                        <span className="mr-1 font-medium text-muted">{idx + 1}.</span>
                        <a href={comp.listingUrl} target="_blank" rel="noopener noreferrer" className="hover:underline">
                          {comp.listingUrl}
                        </a>
                        {idx === 0 && (
                          <span className="ml-1.5 rounded-full bg-amber-100 px-1.5 py-0.5 text-[9px] font-semibold text-amber-800">primary</span>
                        )}
                        {comp.note && <span className="ml-1 italic text-muted"> - {comp.note}</span>}
                      </p>
                    ))}
                  </div>
                )}
                {pinnedCompMsg && (
                  <p className="mt-1 text-xs text-accent">{pinnedCompMsg}</p>
                )}
              </div>
              {hasBenchmark && !showPinnedComps && (
                <div className="flex shrink-0 gap-1.5">
                  <Button size="sm" variant="ghost" onClick={handleRemovePinnedComps} disabled={pinnedCompSaving}>
                    Clear all
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setShowPinnedComps(true)}>
                    Edit
                  </Button>
                </div>
              )}
            </div>

            {editOpen && (
              <div className={hasBenchmark ? "mt-4 space-y-3 border-t border-border pt-4" : "mt-3 space-y-3"}>
                {!hasBenchmark && (
                  <p className="text-xs text-muted">
                    The single best way to improve accuracy. Paste the Airbnb listing you compete with most - we&apos;ll anchor your estimate to its real nightly rate.
                  </p>
                )}
                {pinnedCompsList.map((comp, idx) => (
                  <div key={idx} className={`rounded-lg border p-3 ${idx === 0 ? "border-amber-300 bg-amber-50/60" : "border-gray-200 bg-white"}`}>
                    {pinnedCompsList.length > 1 && (
                      <div className="mb-2 flex items-center justify-between">
                        {idx === 0 ? (
                          <span className="rounded-full bg-amber-200 px-2 py-0.5 text-[10px] font-semibold text-amber-900">
                            Primary benchmark
                          </span>
                        ) : (
                          <button
                            type="button"
                            onClick={() => {
                              const next = [...pinnedCompsList];
                              const [picked] = next.splice(idx, 1);
                              next.unshift(picked);
                              setPinnedCompsList(next);
                            }}
                            className="rounded-full border border-gray-300 px-2 py-0.5 text-[10px] font-medium text-gray-500 hover:border-amber-400 hover:text-amber-700 transition-colors"
                          >
                            Set as primary
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => setPinnedCompsList(pinnedCompsList.filter((_, i) => i !== idx))}
                          className="text-xs text-muted hover:text-warning"
                        >
                          Remove
                        </button>
                      </div>
                    )}
                    <div className="space-y-1.5">
                      <input
                        type="url"
                        placeholder="https://airbnb.com/rooms/123..."
                        value={comp.listingUrl}
                        onChange={(e) => {
                          const next = [...pinnedCompsList];
                          next[idx] = { ...next[idx], listingUrl: e.target.value };
                          setPinnedCompsList(next);
                        }}
                        className="input w-full text-sm"
                      />
                      {comp.listingUrl && !comp.listingUrl.includes("airbnb.com/rooms/") && (
                        <p className="text-xs text-warning">Must be a valid Airbnb listing URL.</p>
                      )}
                      <input
                        type="text"
                        placeholder="Optional note (e.g. same building)"
                        value={comp.note}
                        onChange={(e) => {
                          const next = [...pinnedCompsList];
                          next[idx] = { ...next[idx], note: e.target.value };
                          setPinnedCompsList(next);
                        }}
                        className="input w-full text-sm"
                        maxLength={500}
                      />
                    </div>
                  </div>
                ))}
                {pinnedCompsList.length < 10 && (
                  <button
                    type="button"
                    onClick={() => setPinnedCompsList([...pinnedCompsList, { listingUrl: "", note: "" }])}
                    className="text-xs text-accent hover:underline"
                  >
                    + Add another listing
                  </button>
                )}
                <div className="flex gap-2 pt-1">
                  <Button
                    size="sm"
                    onClick={handleSavePinnedComps}
                    disabled={
                      pinnedCompSaving ||
                      pinnedCompsList.every((c) => !c.listingUrl.includes("airbnb.com/rooms/"))
                    }
                  >
                    {pinnedCompSaving ? "Saving..." : "Save"}
                  </Button>
                  {hasBenchmark && (
                    <Button size="sm" variant="ghost" onClick={() => setShowPinnedComps(false)}>
                      Cancel
                    </Button>
                  )}
                </div>
              </div>
            )}
          </Card>
        );
      })()}
      </div>{/* end Section 3 */}

      {/* â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
          Section 4: Report History
      â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <p className="text-[11px] font-semibold uppercase tracking-widest text-foreground/35">
            Market Report History
          </p>
          <span className="text-xs text-muted">
            {filteredRows.length} report{filteredRows.length !== 1 ? "s" : ""}
          </span>
        </div>

        {/* Filters */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted">Filter:</span>
          {(["all", "ready", "error"] as StatusFilter[]).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setStatusFilter(f)}
              className={`rounded-lg px-3 py-1.5 text-xs transition-colors ${
                statusFilter === f
                  ? "bg-foreground text-white"
                  : "bg-white text-muted border border-border hover:text-foreground"
              }`}
            >
              {f === "all" ? "All" : f === "ready" ? "Ready" : "Error"}
            </button>
          ))}
        </div>

      {/* Report list */}
      {filteredRows.length === 0 ? (
        <Card>
          <p className="text-sm text-muted">No reports found.</p>
        </Card>
      ) : (
        <Card>
          <div className="divide-y divide-border">
            {filteredRows.map((row) => {
              const report = getReport(row);
              if (!report) return null;

              const recommended =
                report.result_summary?.recommendedPrice?.nightly;
              const median = report.result_summary?.nightlyMedian;
              const benchmarkInfo = report.result_summary?.benchmarkInfo;
              const comparableCount =
                report.result_summary?.comparableListings?.length ?? 0;
              const benchmarkUsed = benchmarkInfo?.benchmarkUsed === true;
              const totalDays = benchmarkInfo?.fetchStats?.totalDays ?? 0;
              const highDays = benchmarkInfo?.fetchStats?.highConfidenceDays ?? 0;
              const lowDays = benchmarkInfo?.fetchStats?.lowConfidenceDays ?? 0;
              const confidenceRate =
                totalDays > 0 ? Math.round((highDays / totalDays) * 100) : null;
              const lowConfidenceRate =
                totalDays > 0 ? Math.round((lowDays / totalDays) * 100) : null;

              const statusColor =
                report.status === "ready"
                  ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                  : report.status === "error"
                    ? "bg-rose-50 text-rose-700 border-rose-200"
                    : "bg-gray-50 text-gray-600 border-gray-200";

              return (
                <div key={row.id} className="px-3 py-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    {/* Left: info */}
                    <div className="space-y-1">
                      <div className="flex items-center gap-2">
                        <span
                          className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${statusColor}`}
                        >
                          {report.status}
                        </span>
                        {row.trigger === "scheduled" ? (
                          <span className="rounded-full bg-teal-50 px-2 py-0.5 text-[10px] font-semibold text-teal-700">
                            Nightly
                          </span>
                        ) : (
                          <span className="text-xs text-muted">{row.trigger}</span>
                        )}
                      </div>
                      <p className="text-sm">
                        <span className="font-medium">
                          {report.input_date_start} &rarr;{" "}
                          {report.input_date_end}
                        </span>
                      </p>
                      <p className="text-xs text-muted">
                        Created{" "}
                        {new Date(report.created_at).toLocaleDateString(
                          undefined,
                          {
                            year: "numeric",
                            month: "short",
                            day: "numeric",
                            hour: "2-digit",
                            minute: "2-digit",
                          }
                        )}
                      </p>
                      {report.status === "ready" && (
                        <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                          {benchmarkUsed && (
                            <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
                              Benchmark used
                            </span>
                          )}
                          {benchmarkInfo?.benchmarkFetchStatus === "direct_page" && (
                            <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-medium text-blue-700">
                              Anchored via listing page
                            </span>
                          )}
                          {benchmarkInfo?.benchmarkMismatchLevel === "strong_mismatch" && (
                            <span className="rounded-full bg-rose-50 px-2 py-0.5 text-[10px] font-medium text-rose-700">
                              Benchmark mismatch
                            </span>
                          )}
                          {benchmarkInfo?.conflictDetected && (
                            <span className="rounded-full bg-yellow-50 px-2 py-0.5 text-[10px] font-medium text-yellow-700">
                              Market conflict
                            </span>
                          )}
                          {comparableCount > 0 && (
                            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-700">
                              {comparableCount} comps shown
                            </span>
                          )}
                          {benchmarkUsed && confidenceRate != null && (
                            <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
                              Confidence {confidenceRate}%
                            </span>
                          )}
                        </div>
                      )}
                      {report.status === "ready" && benchmarkUsed && confidenceRate != null && (
                        <p className="text-xs text-muted">
                          Benchmark confidence rate:{" "}
                          <span className="font-medium text-foreground">{confidenceRate}%</span>
                          {totalDays > 0 && (
                            <>
                              {" "}high-confidence days
                              {lowConfidenceRate != null && lowConfidenceRate > 0 && (
                                <span className="text-muted"> | {lowConfidenceRate}% low-confidence direct-page days</span>
                              )}
                            </>
                          )}
                        </p>
                      )}
                      {report.status === "ready" && (() => {
                        const s = report.result_summary as Record<string, unknown> | null;
                        const observed = s?.observedListingPrice as number | null | undefined;
                        return (
                          <p className="text-sm">
                            {observed != null ? (
                              <>
                                <span className="font-semibold">${observed}</span>
                                <span className="ml-1 text-xs text-muted">live price</span>
                              </>
                            ) : recommended != null ? (
                              <>
                                <span className="font-semibold">${recommended}</span>
                                <span className="ml-1 text-xs text-muted">suggested</span>
                              </>
                            ) : null}
                            {median != null && (
                              <span className="ml-2 text-xs text-muted">
                                mkt ${median}
                              </span>
                            )}
                          </p>
                        );
                      })()}
                      {report.status === "error" && report.error_message && (
                        <p className="text-xs text-rose-600">
                          {report.error_message}
                        </p>
                      )}
                    </div>

                    {/* Right: actions */}
                    <div className="flex shrink-0 items-center gap-1.5">
                      {report.status === "ready" && (
                        <Link href={`/r/${report.share_id}`}>
                          <Button size="sm" variant="ghost">
                            View report
                          </Button>
                        </Link>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      )}
      </div>{/* end Section 4 */}

    </div>
  );
}
