"use client";

import { useEffect, useState, useCallback, useRef, useMemo, use } from "react";
import Link from "next/link";
import { Card } from "@/components/Card";
import { Button } from "@/components/Button";
import { HowWeEstimated } from "@/components/report/HowWeEstimated";
import { ComparableListingsSection } from "@/components/report/ComparableListingsSection";
import { PricingHeatmap } from "@/components/dashboard/PricingHeatmap";
import { getSupabaseBrowser } from "@/lib/supabase";
import { normalizeMlForecastRunRow } from "@/lib/mlSidecar";
import type { MlForecastRun } from "@/lib/mlSidecar";
import type {
  CalendarDay,
  PricingReport,
  TargetSpec,
  QueryCriteria,
  CompsSummary,
  ComparableListing,
  BenchmarkInfo,
} from "@/lib/schemas";
import { generatePricingReport } from "@/core/pricingCore";

// Demo report — realistic property, dynamic date range (next 30 days).
function getDemoReport(): PricingReport {
  const today = new Date();
  const startDate = today.toISOString().split("T")[0];
  const end = new Date(today);
  end.setDate(today.getDate() + 30);
  const endDate = end.toISOString().split("T")[0];

  // Four sampled scrape dates spread across the 30-day window.
  const sampleDates = [0, 7, 14, 21].map((offset) => {
    const d = new Date(today);
    d.setDate(today.getDate() + offset);
    return d.toISOString().split("T")[0];
  });
  const [d0, d7, d14, d21] = sampleDates;

  const demoAddress = "2847 Hillcrest Drive, Santa Barbara, CA";
  const demoPolicy = {
    weeklyDiscountPct: 10,
    monthlyDiscountPct: 20,
    refundable: true,
    nonRefundableDiscountPct: 10,
    stackingMode: "compound" as const,
    maxTotalDiscountPct: 40,
  };

  const result = generatePricingReport({
    listing: {
      address: demoAddress,
      propertyType: "entire_home",
      bedrooms: 2,
      bathrooms: 1,
      maxGuests: 4,
      amenities: ["wifi", "kitchen", "washer", "free_parking", "pool"],
    },
    startDate,
    endDate,
    discountPolicy: demoPolicy,
  });

  // ── Transparency data ──────────────────────────────────────────

  const targetSpec: TargetSpec = {
    title: "2847 Hillcrest Drive",
    location: "Santa Barbara, CA",
    propertyType: "Entire home",
    accommodates: 4,
    bedrooms: 2,
    beds: 2,
    baths: 1,
    amenities: ["Wifi", "Kitchen", "Washer", "Free parking on premises", "Pool"],
    rating: null,
    reviews: null,
  };

  const queryCriteria: QueryCriteria = {
    locationBasis: "Santa Barbara, CA",
    searchAdults: 2,
    checkin: startDate,
    checkout: endDate,
    propertyTypeFilter: "entire_home",
    tolerances: { accommodates: 2, bedrooms: 1, beds: 2, baths: 1 },
  };

  const benchmarkInfo: BenchmarkInfo = {
    benchmarkUsed: true,
    benchmarkUrl: "https://www.airbnb.com/rooms/45892310",
    benchmarkFetchStatus: "search_hit",
    benchmarkFetchMethod: "search_result_card",
    avgBenchmarkPrice: 188,
    avgMarketPrice: 196,
    marketAdjustmentPct: 4.3,
    appliedMarketWeight: 0.35,
    effectiveMarketWeight: 0.29,
    maxAdjCap: 0.35,
    benchmarkTargetSimilarity: 0.93,
    benchmarkMismatchLevel: "high_match",
    outlierDays: 1,
    conflictDetected: false,
    fallbackReason: null,
    fetchStats: {
      searchHits: 28,
      directFetches: 2,
      failed: 0,
      totalDays: 30,
      highConfidenceDays: 24,
      mediumConfidenceDays: 4,
      lowConfidenceDays: 2,
    },
    secondaryComps: null,
    consensusSignal: "strong",
  };

  const comparableListings: ComparableListing[] = [
    {
      id: "comp-sb-1",
      title: "Charming 2BR Cottage Near State Street",
      propertyType: "Entire home",
      accommodates: 4,
      bedrooms: 2,
      baths: 1,
      nightlyPrice: 185,
      currency: "USD",
      similarity: 0.94,
      rating: 4.87,
      reviews: 142,
      location: "Santa Barbara, CA",
      url: null,
      queryNights: 1,
      usedInPricingDays: 28,
      priceByDate: { [d0]: 178, [d7]: 192, [d14]: 201, [d21]: 183 },
    },
    {
      id: "comp-sb-2",
      title: "Sunny 2-Bedroom Home — Walk to Beach",
      propertyType: "Entire home",
      accommodates: 4,
      bedrooms: 2,
      baths: 1,
      nightlyPrice: 209,
      currency: "USD",
      similarity: 0.91,
      rating: 4.92,
      reviews: 88,
      location: "Santa Barbara, CA",
      url: null,
      queryNights: 1,
      usedInPricingDays: 26,
      priceByDate: { [d0]: 199, [d7]: 215, [d14]: 222, [d21]: 204 },
    },
    {
      id: "comp-sb-3",
      title: "Modern Bungalow with Patio — Eastside SB",
      propertyType: "Entire home",
      accommodates: 3,
      bedrooms: 2,
      baths: 1,
      nightlyPrice: 172,
      currency: "USD",
      similarity: 0.88,
      rating: 4.78,
      reviews: 215,
      location: "Santa Barbara, CA",
      url: null,
      queryNights: 1,
      usedInPricingDays: 30,
      priceByDate: { [d0]: 165, [d7]: 175, [d14]: 182, [d21]: 170 },
    },
    {
      id: "comp-sb-4",
      title: "Bright 2BD with Private Garden",
      propertyType: "Entire home",
      accommodates: 4,
      bedrooms: 2,
      baths: 1,
      nightlyPrice: 193,
      currency: "USD",
      similarity: 0.87,
      rating: 4.83,
      reviews: 61,
      location: "Santa Barbara, CA",
      url: null,
      queryNights: 1,
      usedInPricingDays: 24,
      priceByDate: { [d0]: 186, [d7]: 198, [d14]: 207, [d21]: 190 },
    },
    {
      id: "comp-sb-5",
      title: "Cozy Craftsman Near Downtown & Funk Zone",
      propertyType: "Entire home",
      accommodates: 4,
      bedrooms: 2,
      baths: 1,
      nightlyPrice: 168,
      currency: "USD",
      similarity: 0.85,
      rating: 4.71,
      reviews: 307,
      location: "Santa Barbara, CA",
      url: null,
      queryNights: 1,
      usedInPricingDays: 29,
      priceByDate: { [d0]: 162, [d7]: 172, [d14]: 179, [d21]: 165 },
    },
    {
      id: "comp-sb-6",
      title: "Santa Barbara Retreat with Pool Access",
      propertyType: "Entire home",
      accommodates: 5,
      bedrooms: 2,
      baths: 2,
      nightlyPrice: 228,
      currency: "USD",
      similarity: 0.82,
      rating: 4.95,
      reviews: 43,
      location: "Santa Barbara, CA",
      url: null,
      queryNights: 1,
      usedInPricingDays: 22,
      priceByDate: { [d0]: 219, [d7]: 235, [d14]: 248, [d21]: 222 },
    },
    {
      id: "comp-sb-7",
      title: "Updated 2BR Rancho — Quiet Street",
      propertyType: "Entire home",
      accommodates: 4,
      bedrooms: 2,
      baths: 1,
      nightlyPrice: 176,
      currency: "USD",
      similarity: 0.80,
      rating: 4.68,
      reviews: 129,
      location: "Santa Barbara, CA",
      url: null,
      queryNights: 1,
      usedInPricingDays: 27,
      priceByDate: { [d0]: 169, [d7]: 180, [d14]: 188, [d21]: 174 },
    },
    {
      id: "comp-sb-8",
      title: "Stylish 2BR with Rooftop Deck & Views",
      propertyType: "Entire home",
      accommodates: 4,
      bedrooms: 2,
      baths: 1,
      nightlyPrice: 214,
      currency: "USD",
      similarity: 0.77,
      rating: 4.88,
      reviews: 76,
      location: "Santa Barbara, CA",
      url: null,
      queryNights: 1,
      usedInPricingDays: 20,
      priceByDate: { [d0]: 205, [d7]: 220, [d14]: 229, [d21]: 210 },
    },
  ];

  const compsSummary: CompsSummary = {
    collected: 24,
    afterFiltering: 12,
    usedForPricing: 8,
    filterStage: "strict",
    topSimilarity: 0.94,
    avgSimilarity: 0.86,
    sampledDays: 4,
    interpolatedDays: 26,
    missingDays: 0,
    belowSimilarityFloor: 4,
    filterFloor: 0.65,
    lowCompConfidenceDays: 0,
  };

  return {
    id: "demo",
    shareId: "demo",
    createdAt: new Date().toISOString(),
    status: "ready",
    coreVersion: result.coreVersion,
    inputAddress: demoAddress,
    inputAttributes: {
      address: demoAddress,
      propertyType: "entire_home",
      bedrooms: 2,
      bathrooms: 1,
      maxGuests: 4,
      amenities: ["wifi", "kitchen", "washer", "free_parking", "pool"],
      lastMinuteStrategy: {
        mode: "auto",
        aggressiveness: 50,
        floor: 0.65,
        cap: 1.05,
      },
    },
    inputDateStart: startDate,
    inputDateEnd: endDate,
    discountPolicy: demoPolicy,
    resultSummary: {
      ...result.summary,
      targetSpec,
      queryCriteria,
      benchmarkInfo,
      comparableListings,
      compsSummary,
    },
    resultCalendar: result.calendar,
    errorMessage: null,
  };
}

// Polling config
const POLL_INTERVAL_MS = 2_000;

// Staleness tiers based on worker_heartbeat_at age
const STALE_FRESH_MS = 45_000;    // < 45s  → fresh (normal spinner)
const STALE_SLOW_MS = 90_000;     // 45–90s → slow (mild warning)
const STALE_DELAYED_MS = 300_000; // 90–300s → delayed (strong warning)
// > 300s → unavailable

type StalenessTier = "fresh" | "slow" | "delayed" | "unavailable";

function getStaleness(workerHeartbeatAt: string | null | undefined): StalenessTier {
  if (!workerHeartbeatAt) return "fresh"; // no heartbeat yet = just started
  const ageMs = Date.now() - new Date(workerHeartbeatAt).getTime();
  if (ageMs < STALE_FRESH_MS) return "fresh";
  if (ageMs < STALE_SLOW_MS) return "slow";
  if (ageMs < STALE_DELAYED_MS) return "delayed";
  return "unavailable";
}

const STAGE_LABELS: Record<string, string> = {
  connecting: "Connecting to browser",
  extracting_target: "Extracting listing details",
  fetching_benchmark: "Fetching benchmark listing",
  searching_comps: "Searching comparable listings",
  pricing: "Computing pricing estimates",
  saving_results: "Saving results",
  completed: "Complete",
};

interface ReportApplyResponse {
  executionMode?: "stub" | "live";
  executionModeNote?: string;
  nightsQueued?: number;
  nightsSimulatedSuccess?: number;
}

function formatPrice(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `$${Math.round(value).toLocaleString()}`
    : "-";
}

function getCalendarSuggestedPrice(day: CalendarDay | null | undefined): number | null {
  if (!day) return null;
  const price = day.recommendedDailyPrice ?? day.basePrice;
  return typeof price === "number" && Number.isFinite(price) ? price : null;
}

function getMlForecastFromSummary(summary: PricingReport["resultSummary"] | null | undefined): MlForecastRun | null {
  const raw = (summary as (PricingReport["resultSummary"] & {
    mlForecast?: Record<string, unknown> | null;
  }) | null | undefined)?.mlForecast;
  if (!raw) return null;
  return normalizeMlForecastRunRow(raw);
}

function getFriendlyReportError(message: string | null | undefined) {
  if (!message) {
    return "We couldn't generate your pricing report. Please try again.";
  }

  if (message.includes("Could not reach Airbnb data")) {
    return "We found Airbnb listings, but couldn't verify enough nightly prices for this report. Please try again in a moment.";
  }

  if (message.includes("Could not collect enough pricing data")) {
    return "We couldn't collect enough trustworthy nightly prices to build this report. Please try again in a moment.";
  }

  return message;
}

export default function ResultsPage({
  params,
}: {
  params: Promise<{ shareId: string }>;
}) {
  const { shareId } = use(params);
  const [report, setReport] = useState<PricingReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Polling state
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Auth state
  const [isSignedIn, setIsSignedIn] = useState<boolean | null>(null);

  // Save-to-dashboard state
  const [isReportSaved, setIsReportSaved] = useState<boolean | null>(null);
  const [saveLoading, setSaveLoading] = useState(false);
  const [saveError, setSaveError] = useState("");

  // Auto-Apply status for this listing (null = loading, false = not configured)
  const [autoApplyConfigured, setAutoApplyConfigured] = useState<boolean | null>(null);
  const [cohostVerified, setCohostVerified] = useState<boolean>(false);
  const [autoApplySubmitting, setAutoApplySubmitting] = useState(false);
  const [autoApplyError, setAutoApplyError] = useState("");
  const [autoApplySuccess, setAutoApplySuccess] = useState("");
  const [applyModalOpen, setApplyModalOpen] = useState(false);
  const [applyModalPhase, setApplyModalPhase] = useState<"applying" | "result">("applying");
  const [applyModalResult, setApplyModalResult] = useState<ReportApplyResponse | null>(null);
  const [applyModalError, setApplyModalError] = useState<string | null>(null);
  const reportListingId = (report as (typeof report & { listingId?: string | null }))?.listingId ?? null;

  // Date the user clicked on the heatmap (null = no selection).
  const [clickedDate, setClickedDate] = useState<string | null>(null);
  const [mlWeightPct, setMlWeightPct] = useState(0);

  function updateMlWeightPct(value: number) {
    if (!Number.isFinite(value)) return;
    setMlWeightPct(Math.min(100, Math.max(0, Math.round(value))));
  }

  // Reset the clicked date whenever the report changes.
  const reportId = report?.id ?? null;
  useEffect(() => { setClickedDate(null); }, [reportId]);

  const snappedDate = clickedDate;

  const mlForecastRun = useMemo(
    () => getMlForecastFromSummary(report?.resultSummary),
    [report?.resultSummary]
  );

  const mlPredictionsByDate = useMemo(() => {
    const byDate = new Map<string, number>();
    for (const prediction of mlForecastRun?.predictions ?? []) {
      if (
        typeof prediction.predictedPrice === "number" &&
        Number.isFinite(prediction.predictedPrice)
      ) {
        byDate.set(prediction.date, prediction.predictedPrice);
      }
    }
    return byDate;
  }, [mlForecastRun]);

  const mlWeight = mlWeightPct / 100;
  const adjustedCalendar = useMemo(() => {
    const calendar = report?.resultCalendar ?? [];
    if (calendar.length === 0 || mlWeightPct === 0 || mlPredictionsByDate.size === 0) {
      return calendar;
    }

    return calendar.map((day) => {
      const suggestedPrice = getCalendarSuggestedPrice(day);
      const mlPrice = mlPredictionsByDate.get(day.date);
      if (
        typeof suggestedPrice !== "number" ||
        typeof mlPrice !== "number"
      ) {
        return day;
      }
      const adjustedPrice = Math.round(
        suggestedPrice * (1 - mlWeight) + mlPrice * mlWeight
      );
      return {
        ...day,
        recommendedDailyPrice: adjustedPrice,
      };
    });
  }, [report?.resultCalendar, mlPredictionsByDate, mlWeight, mlWeightPct]);

  const priceBlendPreview = useMemo(() => {
    const calendar = report?.resultCalendar ?? [];
    const previewDay =
      (snappedDate ? calendar.find((day) => day.date === snappedDate) : null) ??
      calendar[0] ??
      null;
    const suggestedPrice = getCalendarSuggestedPrice(previewDay);
    const mlPrice = previewDay ? mlPredictionsByDate.get(previewDay.date) ?? null : null;
    const adjustedPrice =
      typeof suggestedPrice === "number" && typeof mlPrice === "number"
        ? Math.round(suggestedPrice * (1 - mlWeight) + mlPrice * mlWeight)
        : suggestedPrice;
    return {
      date: previewDay?.date ?? null,
      suggestedPrice,
      mlPrice,
      adjustedPrice,
      hasMlPrediction: typeof mlPrice === "number" && Number.isFinite(mlPrice),
      isSelectedDate: !!snappedDate && previewDay?.date === snappedDate,
    };
  }, [report?.resultCalendar, snappedDate, mlPredictionsByDate, mlWeight]);

  const contextualBenchmarkInfo = useMemo((): BenchmarkInfo | null => {
    return (report?.benchmarkInfo ?? report?.resultSummary?.benchmarkInfo ?? null) as BenchmarkInfo | null;
  }, [report]);

  const contextualPinnedUrls = useMemo((): string[] => {
    if (!report) return [];
    const comps = report.inputAttributes?.preferredComps;
    const base = Array.isArray(comps)
      ? comps
        .filter((c) => c.enabled !== false && c.listingUrl)
        .map((c) => c.listingUrl!)
      : [];
    const bmUrl = contextualBenchmarkInfo?.benchmarkUrl;
    if (typeof bmUrl === "string" && bmUrl.trim()) {
      const exists = base.some(
        (u) => u.split("?")[0].toLowerCase() === bmUrl.split("?")[0].toLowerCase()
      );
      if (!exists) base.unshift(bmUrl);
    }
    return base;
  }, [report, contextualBenchmarkInfo]);

  const contextualComparableListings = useMemo((): ComparableListing[] | null => {
    if (!report) return null;
    const rawListings = (
      report.comparableListings ?? report.resultSummary?.comparableListings ?? []
    ) as ComparableListing[];
    const bmUrl = contextualBenchmarkInfo?.benchmarkUrl;
    if (!bmUrl) return rawListings;

    const normalize = (url: string) => url.split("?")[0].toLowerCase();
    const roomIdFromUrl = (url: string | null | undefined): string | null => {
      if (!url) return null;
      const m = url.match(/\/rooms\/(\d+)/);
      return m ? m[1] : null;
    };
    const bmNorm = normalize(bmUrl);
    const bmRoomId = roomIdFromUrl(bmUrl);

    const flagged = rawListings.map((listing) => {
      const sameUrl = !!(listing.url && normalize(listing.url) === bmNorm);
      const sameRoomId = !!(
        bmRoomId &&
        listing.url &&
        roomIdFromUrl(listing.url) === bmRoomId
      );
      const alreadyPinned =
        (listing as ComparableListing & { isPinnedBenchmark?: boolean }).isPinnedBenchmark === true;
      if (sameUrl || sameRoomId || alreadyPinned) {
        return { ...listing, isPinnedBenchmark: true };
      }
      return listing;
    });

    const existingIdx = flagged.findIndex(
      (listing) =>
        (bmRoomId != null &&
          roomIdFromUrl(listing.url ?? null) === bmRoomId) ||
        (listing.url && normalize(listing.url) === bmNorm) ||
        (listing as ComparableListing & { isPinnedBenchmark?: boolean }).isPinnedBenchmark === true
    );
    if (existingIdx >= 0) {
      const existing = flagged[existingIdx];
      const rest = flagged.filter((_, idx) => idx !== existingIdx);
      const hydrated: ComparableListing = {
        ...existing,
        isPinnedBenchmark: true,
        url: existing.url ?? bmUrl,
        title: existing.title || "",
        nightlyPrice:
          typeof existing.nightlyPrice === "number" && existing.nightlyPrice > 0
            ? existing.nightlyPrice
            : (contextualBenchmarkInfo?.avgBenchmarkPrice ?? 0),
      };
      return [hydrated, ...rest];
    }

    return flagged;
  }, [report, contextualBenchmarkInfo]);

  const fetchReport = useCallback(async () => {
    try {
      const res = await fetch(`/api/r/${shareId}`);
      if (!res.ok) throw new Error("Report not found");
      const data = await res.json();
      setReport(data);

      // Stop polling once in a terminal state
      if (data.status === "ready" || data.status === "error") {
        if (pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
        setLoading(false);
      }
    } catch (e) {
      setError((e as Error).message);
      setLoading(false);
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    }
  }, [shareId]);

  useEffect(() => {
    if (shareId === "demo") {
      setReport(getDemoReport());
      setLoading(false);
      return;
    }

    // Initial fetch
    fetchReport();

    // Start polling
    pollRef.current = setInterval(() => {
      fetchReport();
    }, POLL_INTERVAL_MS);

    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [shareId, fetchReport]);

  useEffect(() => {
    const supabase = getSupabaseBrowser();
    supabase.auth.getUser().then(({ data: { user } }) => {
      setIsSignedIn(!!user);
    });
  }, []);

  async function handleSaveToDashboard() {
    if (!report) return;
    setSaveLoading(true);
    setSaveError("");
    try {
      const res = await fetch(`/api/reports/${report.id}/save`, {
        method: "POST",
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.error || "Failed to save report");
      }
      setIsReportSaved(true);
    } catch (err) {
      setSaveError((err as Error).message);
    } finally {
      setSaveLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;

    async function checkSavedStatus() {
      if (!report?.id || isSignedIn !== true) {
        setIsReportSaved(null);
        return;
      }

      try {
        setSaveError("");
        const res = await fetch(`/api/reports/${report.id}/save`, {
          method: "GET",
          cache: "no-store",
        });
        if (res.status === 401) {
          if (!cancelled) setIsReportSaved(false);
          return;
        }
        if (!res.ok) {
          throw new Error("Failed to check saved status");
        }
        const data = await res.json();
        if (!cancelled) setIsReportSaved(Boolean(data?.saved));
      } catch {
        if (!cancelled) {
          setIsReportSaved(false);
        }
      }
    }

    checkSavedStatus();
    return () => {
      cancelled = true;
    };
  }, [report?.id, isSignedIn]);

  // Fetch Auto-Apply settings for this listing when signed in
  useEffect(() => {
    let cancelled = false;
    async function checkAutoApply() {
      if (!reportListingId || isSignedIn !== true) {
        setAutoApplyConfigured(false);
        setCohostVerified(false);
        return;
      }
      try {
        const res = await fetch(`/api/listings/${reportListingId}`, { cache: "no-store" });
        if (!res.ok) {
          if (!cancelled) {
            setAutoApplyConfigured(false);
            setCohostVerified(false);
          }
          return;
        }
        const data = await res.json();
        const listing = (data?.listing ?? data) as
          | {
              auto_apply_last_updated_at?: string | null;
              auto_apply_cohost_status?: string | null;
            }
          | null;
        if (!cancelled) {
          setAutoApplyConfigured(!!listing?.auto_apply_last_updated_at);
          setCohostVerified((listing?.auto_apply_cohost_status ?? null) === "verified");
        }
      } catch {
        if (!cancelled) {
          setAutoApplyConfigured(false);
          setCohostVerified(false);
        }
      }
    }
    checkAutoApply();
    return () => { cancelled = true; };
  }, [reportListingId, isSignedIn]);

  async function queueManualApply(selectedDates: string[]) {
    if (!reportListingId) {
      window.location.href = "/dashboard";
      return;
    }
    setAutoApplySubmitting(true);
    setAutoApplyError("");
    setAutoApplySuccess("");
    setApplyModalOpen(true);
    setApplyModalPhase("applying");
    setApplyModalResult(null);
    setApplyModalError(null);
    try {
      const res = await fetch(`/api/listings/${reportListingId}/manual-apply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          selectedDates,
          sourceReportId: report?.id ?? null,
        }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        throw new Error(data?.error || "Failed to queue Auto-Apply.");
      }
      const nightsQueued = Number(data?.nightsQueued ?? 0);
      setApplyModalResult(data as ReportApplyResponse);
      setApplyModalPhase("result");
      setAutoApplySuccess(
        nightsQueued > 0
          ? `Auto-Apply queued for ${nightsQueued} night${nightsQueued === 1 ? "" : "s"}.`
          : "Auto-Apply queued successfully."
      );
    } catch (err) {
      const message = (err as Error).message;
      setAutoApplyError(message);
      setApplyModalError(message);
      setApplyModalPhase("result");
    } finally {
      setAutoApplySubmitting(false);
    }
  }

  async function handleAutoApplyNow() {
    const selectedDates = (report?.resultCalendar ?? []).map((d) => d.date);
    await queueManualApply(selectedDates);
  }

  async function handleHeatmapApply(selectedDates: string[]) {
    // If user reaches this path from report heatmap, attempt to queue directly.
    // Fallback to dashboard when context is incomplete.
    if (isSignedIn !== true || !reportListingId) {
      window.location.href = "/dashboard";
      return;
    }
    await queueManualApply(selectedDates);
  }


  // Queued / Running state
  if (
    report &&
    (report.status === "queued" || report.status === "running") &&
    !error
  ) {
    const staleness = getStaleness(report.workerHeartbeatAt);
    const progress = report.progressMeta;
    const pct = progress?.pct ?? 0;
    const stageLabel = progress?.stage ? (STAGE_LABELS[progress.stage] ?? progress.stage) : null;
    const estSec = progress?.est_seconds_remaining;

    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <div className="mx-auto w-full max-w-md text-center">
          {staleness !== "unavailable" && (
            <div className="mx-auto mb-6 h-10 w-10 animate-spin rounded-full border-3 border-accent border-t-transparent" />
          )}
          {staleness === "unavailable" && (
            <div className="mx-auto mb-6 flex h-10 w-10 items-center justify-center rounded-full bg-rose-100 text-rose-500">
              <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M12 3a9 9 0 100 18A9 9 0 0012 3z" />
              </svg>
            </div>
          )}

          <h2 className="mb-2 text-xl font-semibold">
            {staleness === "unavailable" ? "Worker unreachable" : "Analyzing your market"}
          </h2>

          {/* Progress bar — shown once worker has sent first progress update */}
          {progress && staleness !== "unavailable" && (
            <div className="mb-4 px-2">
              <div className="mb-1 flex items-center justify-between text-xs text-muted">
                <span>{stageLabel ?? "Working..."}</span>
                <span>{pct}%</span>
              </div>
              <div className="h-2 w-full overflow-hidden rounded-full bg-surface-alt">
                <div
                  className="h-full rounded-full bg-accent transition-all duration-500"
                  style={{ width: `${pct}%` }}
                />
              </div>
              {estSec != null && estSec > 0 && (
                <p className="mt-1 text-xs text-muted">
                  ~{estSec < 60 ? `${estSec}s` : `${Math.round(estSec / 60)}m`} remaining
                </p>
              )}
            </div>
          )}

          {!progress && (
            <p className="mb-1 text-sm text-muted">
              {report.status === "queued"
                ? "Your report is in the queue..."
                : "Crunching the numbers..."}
            </p>
          )}

          {progress?.message && staleness !== "unavailable" && (
            <p className="mb-1 text-sm text-muted">{progress.message}</p>
          )}

          {staleness === "fresh" && !progress && (
            <p className="text-sm text-muted">This typically takes 30 to 90 seconds.</p>
          )}

          {staleness === "slow" && (
            <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4">
              <p className="text-sm font-medium text-amber-800">Taking a bit longer than usual</p>
              <p className="mt-1 text-xs text-amber-700">
                The worker is still running — hold tight.
              </p>
            </div>
          )}

          {staleness === "delayed" && (
            <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4">
              <p className="text-sm font-medium text-amber-800">This is taking longer than expected</p>
              <p className="mt-1 text-xs text-amber-700">
                The worker may be overloaded. Your report will be processed as soon as possible.
              </p>
            </div>
          )}

          {staleness === "unavailable" && (
            <div className="mt-4 rounded-xl border border-rose-200 bg-rose-50 p-4">
              <p className="text-sm font-medium text-rose-800">Worker appears to be offline</p>
              <p className="mt-1 text-xs text-rose-700">
                No heartbeat received in over 5 minutes. Your report will resume automatically when the worker comes back online.
              </p>
            </div>
          )}

          {report.inputAddress && (
            <p className="mt-4 text-xs text-muted">
              Report for: {report.inputAddress}
            </p>
          )}

          <p className="mt-6 text-xs text-muted">
            You can navigate away — this report will be ready when you return.
          </p>
          <Link
            href="/dashboard"
            className="mt-2 inline-block text-sm font-medium text-accent hover:underline"
          >
            ← Back to dashboard
          </Link>
        </div>
      </div>
    );
  }

  // Error state
  if (report && report.status === "error") {
    return (
      <div className="mx-auto max-w-5xl px-4 py-12 text-center sm:px-6 sm:py-20">
        <div className="mx-auto mb-6 flex h-16 w-16 items-center justify-center rounded-full bg-rose-50">
          <span className="text-3xl">!</span>
        </div>
        <h1 className="mb-3 text-2xl font-bold">Something went wrong</h1>
        <p className="mb-6 text-muted">
          {getFriendlyReportError(report.errorMessage)}
        </p>
        <Button
          onClick={() => {
            window.location.href = "/tool";
          }}
        >
          Try again
        </Button>
      </div>
    );
  }

  // Initial loading
  if (loading) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <div className="text-center">
          <div className="mx-auto mb-4 h-8 w-8 animate-spin rounded-full border-2 border-accent border-t-transparent" />
          <p className="text-muted">Loading report...</p>
          <Link
            href="/dashboard"
            className="mt-4 inline-block text-sm font-medium text-accent hover:underline"
          >
            ← Back to dashboard
          </Link>
        </div>
      </div>
    );
  }

  // Not found
  if (error || !report || !report.resultSummary) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-12 text-center sm:px-6 sm:py-20">
        <h1 className="mb-4 text-2xl font-bold">Report not found</h1>
        <p className="text-muted">{error || "This report doesn't exist."}</p>
      </div>
    );
  }

  // Ready: show results
  const s = report.resultSummary;
  const suggestedNightly = s.recommendedPrice?.nightly ?? s.nightlyMedian;

  return (
    <div className="mx-auto max-w-5xl px-4 py-8 sm:px-6 sm:py-10">
      {/* Sample report banner */}
      {shareId === "demo" && (
        <div className="mb-6 flex items-center gap-3 rounded-xl border border-blue-200 bg-blue-50 px-4 py-3">
          <span className="shrink-0 rounded-full bg-blue-600 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-white">
            Sample
          </span>
          <p className="text-sm text-blue-800">
            This is a generated example report using illustrative data.{" "}
            <Link
              href="/tool"
              className="font-medium underline underline-offset-2 hover:text-blue-900"
            >
              Analyze your own listing →
            </Link>
          </p>
        </div>
      )}

      {/* Header */}
      <p className="mb-1 text-xs text-muted">{report.inputAddress}</p>
      <h1 className="mb-6 text-2xl font-bold tracking-tight">Pricing Report</h1>

      {/* Hero: suggested rate + KPI strip */}
      <div className="mb-6 overflow-hidden rounded-2xl border border-border bg-white">
        <div className="px-4 py-4 sm:px-6 sm:py-5">
          <p className="mb-1.5 text-xs text-foreground/40">Suggested rate</p>
          <div className="flex items-baseline gap-3">
            <span className="text-4xl font-bold tracking-tight">${suggestedNightly}</span>
            <span className="text-sm text-foreground/40">/night</span>
          </div>
          {s.insightHeadline && (
            <p className="mt-2 text-sm text-foreground/50">{s.insightHeadline}</p>
          )}
        </div>
        <div className="grid grid-cols-2 divide-x divide-border/50 border-t border-border/50 sm:grid-cols-3 md:grid-cols-5">
          {[
            { label: "Market median", value: s.nightlyMedian ? `$${s.nightlyMedian}` : "—" },
            { label: "Occupancy est.", value: s.occupancyPct ? `${s.occupancyPct}%` : "—" },
            { label: "Weekday avg", value: s.weekdayAvg ? `$${s.weekdayAvg}` : "—" },
            { label: "Weekend avg", value: s.weekendAvg ? `$${s.weekendAvg}` : "—" },
            { label: "Monthly est.", value: s.estimatedMonthlyRevenue ? `$${s.estimatedMonthlyRevenue.toLocaleString()}` : "—" },
          ].map((stat) => (
            <div key={stat.label} className="px-3 py-2.5 sm:px-4 sm:py-3">
              <p className="text-[11px] text-foreground/35">{stat.label}</p>
              <p className="mt-0.5 text-sm font-semibold text-foreground/70">{stat.value}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Save CTA (signed-out) */}
      {isSignedIn === false && shareId !== "demo" && (
        <Card className="mb-6 border-accent/20 bg-accent/3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <p className="text-sm font-medium">Save to your dashboard</p>
              <p className="mt-0.5 text-xs text-muted">
                Track pricing and rerun analyses over time.
              </p>
            </div>
            <Link href="/login?next=/dashboard">
              <Button size="sm">Sign up free</Button>
            </Link>
          </div>
        </Card>
      )}

      {/* 30-Day Pricing Plan */}
      {(report.resultCalendar ?? []).length > 0 && (
        <div className="mb-6 space-y-3">
          <PricingHeatmap
            calendar={adjustedCalendar}
            selectable={
              isSignedIn === true &&
              autoApplyConfigured === true &&
              cohostVerified === true
            }
            onApplyDates={(selectedDates) => {
              void handleHeatmapApply(selectedDates);
            }}
            onFocusDate={(date) => setClickedDate(date)}
            focusedDate={clickedDate}
          />
          {mlForecastRun && (
            <div className="rounded-2xl border border-sky-100 bg-white px-4 py-4 sm:px-6">
              <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <p className="text-sm font-semibold text-foreground/80">
                    Calendar price blend
                  </p>
                  <p className="mt-1 text-xs leading-5 text-foreground/45">
                    Adjusted price = suggested price x (1 - ML weight) + ML price x ML weight.
                  </p>
                </div>
                <div className="flex flex-wrap gap-2 text-xs">
                  <span className="rounded-full border border-gray-200 bg-gray-50 px-2.5 py-1 font-medium text-foreground/55">
                    Suggested {formatPrice(priceBlendPreview.suggestedPrice)}
                  </span>
                  <span className="rounded-full border border-sky-200 bg-sky-50 px-2.5 py-1 font-medium text-sky-700">
                    ML {formatPrice(priceBlendPreview.mlPrice)}
                  </span>
                  <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 font-semibold text-emerald-700">
                    Calendar {formatPrice(priceBlendPreview.adjustedPrice)}
                  </span>
                </div>
              </div>

              {mlPredictionsByDate.size > 0 ? (
                <div className="mt-4">
                  <div className="mb-2 flex items-center justify-between gap-3">
                    <label
                      htmlFor="ml-price-weight"
                      className="text-xs font-semibold uppercase tracking-[0.14em] text-foreground/35"
                    >
                      ML weight
                    </label>
                    <label className="flex items-center gap-1.5 text-sm font-semibold text-foreground/70">
                      <input
                        type="number"
                        min="0"
                        max="100"
                        step="1"
                        value={mlWeightPct}
                        onChange={(event) => updateMlWeightPct(Number(event.target.value))}
                        className="h-8 w-16 rounded-lg border border-gray-200 bg-gray-50 px-2 text-right text-sm font-semibold text-foreground/70 outline-none transition-colors focus:border-sky-300 focus:bg-white"
                        aria-label="ML weight percentage"
                      />
                      <span>%</span>
                    </label>
                  </div>
                  <input
                    id="ml-price-weight"
                    type="range"
                    min="0"
                    max="100"
                    step="5"
                    value={mlWeightPct}
                    onChange={(event) => updateMlWeightPct(Number(event.target.value))}
                    className="h-2 w-full accent-sky-700"
                  />
                  <div className="mt-2 flex justify-between text-[11px] text-foreground/35">
                    <span>Use suggested price</span>
                    <span>Use ML price</span>
                  </div>
                  {priceBlendPreview.date && (
                    <p className="mt-3 text-xs text-foreground/45">
                      Previewing {priceBlendPreview.date}
                      {priceBlendPreview.isSelectedDate ? " (selected date)" : ""}.
                      {" "}
                      {priceBlendPreview.hasMlPrediction
                        ? "Dates without an ML prediction keep the suggested price."
                        : "No ML prediction for this date, so the calendar keeps the suggested price."}
                    </p>
                  )}
                </div>
              ) : (
                <div className="mt-4 rounded-xl border border-sky-100 bg-sky-50/60 px-4 py-3">
                  <p className="text-sm font-medium text-sky-800">
                    ML forecast is {mlForecastRun.status}.
                  </p>
                  <p className="mt-1 text-xs text-sky-700/75">
                    The slider will become available after the forecast writes daily predicted prices.
                  </p>
                </div>
              )}
            </div>
          )}
          {(autoApplyError || autoApplySuccess) && (
            <div className="rounded-xl border border-gray-200 bg-white px-4 py-3">
              {autoApplyError && (
                <p className="text-xs text-rose-600">{autoApplyError}</p>
              )}
              {autoApplySuccess && (
                <p className="text-xs text-emerald-700">{autoApplySuccess}</p>
              )}
            </div>
          )}

          {/* Contextual Comparable Listings panel — appears immediately below the
              heatmap when a date is focused. This is the primary comps experience
              on the report page; HowWeEstimated will hide its duplicate comps block. */}
          {(() => {
            const compsListings = contextualComparableListings;
            const comps =
              report.compsSummary ?? report.resultSummary?.compsSummary ?? null;
            if (!clickedDate || !compsListings || compsListings.length === 0) return null;
            return (
              <div className="overflow-hidden rounded-2xl border border-sky-200/70 bg-white">
                <div className="flex items-center justify-between gap-3 border-b border-gray-100 px-5 py-3">
                  <div>
                    <p className="text-sm font-semibold text-foreground/80">
                      Comparable listings
                      <span className="ml-1.5 font-normal text-foreground/50">
                        for {clickedDate}
                      </span>
                    </p>
                    <p className="mt-0.5 text-xs text-foreground/45">
                      Showing listings scraped for this exact day.
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => setClickedDate(null)}
                    aria-label="Close comparable listings panel"
                    className="shrink-0 rounded-lg px-2 py-1 text-xs font-medium text-foreground/35 transition-colors hover:bg-gray-100 hover:text-foreground/65"
                  >
                    ✕
                  </button>
                </div>
                <div className="px-5 py-4">
                  <ComparableListingsSection
                    listings={compsListings as ComparableListing[]}
                    comps={comps}
                    benchmarkInfo={contextualBenchmarkInfo}
                    targetSpec={report.targetSpec ?? null}
                    embedded={true}
                    pinnedUrls={contextualPinnedUrls}
                    selectedDate={snappedDate}
                    clickedDate={clickedDate}
                  />
                </div>
              </div>
            );
          })()}

          {/* Auto-Apply CTA — show when setup is missing or co-host verification is incomplete */}
          {isSignedIn === true &&
            (autoApplyConfigured !== true || cohostVerified !== true) && (
            <div className="overflow-hidden rounded-2xl border border-gray-200 bg-gray-50">
              <div className="flex items-start gap-4 px-5 py-4">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-white shadow-sm ring-1 ring-gray-200">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" className="text-foreground/60" aria-hidden="true">
                    <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
                    <polyline points="13 2 13 9 20 9" />
                  </svg>
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-foreground/80">
                    Set up Auto-Apply to use this calendar
                  </p>
                  <p className="mt-0.5 text-xs leading-snug text-foreground/45">
                    Configure Auto-Apply in your dashboard to select nights and apply pricing recommendations directly to Airbnb.
                  </p>
                  {cohostVerified ? (
                    <button
                      type="button"
                      onClick={handleAutoApplyNow}
                      disabled={autoApplySubmitting}
                      className="mt-3 inline-flex items-center gap-1.5 rounded-xl bg-foreground px-4 py-2 text-xs font-semibold text-white transition-colors hover:bg-foreground/80 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {autoApplySubmitting ? "Queueing Auto-Apply..." : "Auto-Apply now"}
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                        <path d="M5 12h14M12 5l7 7-7 7" />
                      </svg>
                    </button>
                  ) : (
                    <a
                      href="/dashboard"
                      className="mt-3 inline-flex items-center gap-1.5 rounded-xl bg-foreground px-4 py-2 text-xs font-semibold text-white transition-colors hover:bg-foreground/80"
                    >
                      Go to dashboard
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                        <path d="M5 12h14M12 5l7 7-7 7" />
                      </svg>
                    </a>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* How We Estimated */}
      {(
        report.targetSpec ||
        report.resultSummary?.targetSpec ||
        report.compsSummary ||
        report.resultSummary?.compsSummary ||
        report.comparableListings ||
        report.resultSummary?.comparableListings ||
        report.benchmarkInfo ||
        report.resultSummary?.benchmarkInfo ||
        (Array.isArray(report.inputAttributes?.preferredComps) &&
          report.inputAttributes.preferredComps.some(
            (comp) => comp.enabled !== false && comp.listingUrl
          ))
      ) && (
        <HowWeEstimated
          report={report}
          selectedDate={snappedDate}
          clickedDate={clickedDate}
          hideComparableListings={true}
        />
      )}

      {/* Section 4 - Track your market */}
      {isSignedIn === false && (
        <div className="mb-8">
          <h2 className="mb-4 text-lg font-semibold">Track your market</h2>
          <Card className="border-accent/20 bg-accent/[0.02]">
            <p className="text-sm text-muted">
              Save this report to your dashboard and track pricing over time.
            </p>
            <div className="mt-4 flex flex-col gap-2 sm:flex-row">
              <Link href="/login?next=/dashboard">
                <Button size="sm">Sign up free</Button>
              </Link>
              <Link href="/login?next=/dashboard" className="self-start">
                <Button size="sm" variant="secondary">
                  Sign in
                </Button>
              </Link>
            </div>
          </Card>
        </div>
      )}

      {isSignedIn === true && isReportSaved === false && (
        <div className="mb-8">
          <h2 className="mb-4 text-lg font-semibold">Track your market</h2>
          <Card>
            <p className="text-base font-semibold text-foreground">
              Save to your dashboard
            </p>
            <p className="mt-1 text-sm text-muted">
              Keep this report and rerun analysis anytime.
            </p>
            {saveError && <p className="mt-3 text-sm text-rose-600">{saveError}</p>}
            <div className="mt-4">
              <Button onClick={handleSaveToDashboard} disabled={saveLoading}>
                {saveLoading ? "Saving..." : "Save to dashboard"}
              </Button>
            </div>
          </Card>
        </div>
      )}

      {/* Meta */}
      <p className="mt-8 text-center text-xs text-muted">
        {shareId === "demo"
          ? "Sample report — values are illustrative, not from live market data"
          : `Report generated by ${report.coreVersion} on ${new Date(report.createdAt).toLocaleDateString()}`}
      </p>

      {applyModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div
            className="absolute inset-0 bg-black/40"
            onClick={applyModalPhase !== "applying" ? () => setApplyModalOpen(false) : undefined}
            aria-hidden="true"
          />
          <div
            className="relative z-10 flex w-full max-w-md flex-col rounded-2xl bg-white p-6 shadow-2xl"
            role="dialog"
            aria-modal="true"
            aria-labelledby="report-apply-title"
          >
            <h3 id="report-apply-title" className="text-base font-bold tracking-tight text-foreground">
              {applyModalPhase === "applying" ? "Applying..." : applyModalError ? "Apply failed" : "Run complete"}
            </h3>
            <p className="mt-1 text-sm text-foreground/50">
              {applyModalPhase === "applying"
                ? "Preparing execution plan..."
                : applyModalError
                ? "We couldn't queue this apply run."
                : "Airbnb sync not enabled — no prices changed"}
            </p>

            {applyModalPhase === "applying" ? (
              <div className="mt-5 flex flex-col items-center justify-center rounded-xl border border-gray-100 bg-gray-50/70 py-14">
                <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-200 border-t-foreground/60" />
                <p className="mt-4 text-sm font-medium text-foreground/70">Assigning prices...</p>
                <p className="mt-1 text-xs text-foreground/45">
                  Please keep this window open until the run completes.
                </p>
              </div>
            ) : (
              <div className="mt-5 rounded-xl border border-gray-100 bg-gray-50/60 px-4 py-3">
                {applyModalError ? (
                  <p className="text-sm text-rose-700">{applyModalError}</p>
                ) : (
                  <>
                    <p className="text-sm font-semibold text-foreground/70">
                      Nights queued: {Number(applyModalResult?.nightsQueued ?? applyModalResult?.nightsSimulatedSuccess ?? 0)}
                    </p>
                    {applyModalResult?.executionModeNote && (
                      <p className="mt-1 text-xs text-foreground/45">{applyModalResult.executionModeNote}</p>
                    )}
                  </>
                )}
              </div>
            )}

            {applyModalPhase !== "applying" && (
              <div className="mt-5 flex justify-end">
                <button
                  type="button"
                  onClick={() => setApplyModalOpen(false)}
                  className="rounded-xl bg-foreground px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-foreground/85"
                >
                  Close
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}



