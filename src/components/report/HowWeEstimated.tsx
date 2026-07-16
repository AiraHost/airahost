import type { BenchmarkInfo, PricingReport } from "@/lib/schemas";
import { TargetSpecCard } from "./TargetSpecCard";
import { QueryCriteriaCard } from "./QueryCriteriaCard";
import { CompsDistributionCard } from "./CompsDistributionCard";
import { ComparableListingsSection } from "./ComparableListingsSection";

// ── Signal quality row ────────────────────────────────────────────

type SignalLevel = "good" | "warn" | "alert" | "neutral";

function SignalRow({
  level,
  label,
  description,
}: {
  level: SignalLevel;
  label: string;
  description: string;
}) {
  const dotColor: Record<SignalLevel, string> = {
    good:    "bg-emerald-500",
    warn:    "bg-amber-400",
    alert:   "bg-red-400",
    neutral: "bg-gray-300",
  };
  return (
    <div className="flex items-start gap-2">
      <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${dotColor[level]}`} />
      <p className="text-xs text-gray-600">
        <span className="font-medium text-gray-800">{label}</span>
        {" — "}
        {description}
      </p>
    </div>
  );
}

// ── Benchmark listing row ─────────────────────────────────────────

function BenchmarkListingRow({
  url,
  avgPrice,
  daysFound,
  totalDays,
  label,
  isPrimary = false,
}: {
  url: string;
  avgPrice: number | null;
  daysFound: number;
  totalDays: number;
  label: string;
  isPrimary?: boolean;
}) {
  const coverage = totalDays > 0 ? Math.round((daysFound / totalDays) * 100) : null;
  return (
    <div className="flex items-start justify-between gap-3 rounded-lg border border-gray-100 bg-white px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${
            isPrimary ? "bg-gray-900 text-white" : "bg-gray-100 text-gray-600"
          }`}>
            {label}
          </span>
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className="truncate text-xs text-accent hover:underline"
          >
            {url.replace(/^https?:\/\//, "").replace(/\?.*$/, "")}
          </a>
        </div>
        {coverage != null && (
          <p className="mt-0.5 text-[10px] text-gray-400">
            {daysFound} of {totalDays} days sampled
          </p>
        )}
      </div>
      <div className="shrink-0 text-right">
        {avgPrice != null ? (
          <p className="text-sm font-semibold text-gray-900">
            ${avgPrice}
            <span className="text-[10px] font-normal text-gray-500"> /night avg</span>
          </p>
        ) : (
          <p className="text-xs text-gray-400">No price data</p>
        )}
      </div>
    </div>
  );
}

// ── Benchmark transparency block ─────────────────────────────────

function BenchmarkBlock({ info, name }: { info: BenchmarkInfo; name?: string | null }) {
  const statusUsed = info.benchmarkUsed && info.benchmarkFetchStatus !== "failed";
  const statusColor = statusUsed
    ? "border-emerald-200 bg-emerald-50"
    : "border-amber-200 bg-amber-50";
  const badgeColor = statusUsed
    ? "bg-emerald-100 text-emerald-800"
    : "bg-amber-100 text-amber-800";
  const dividerColor = statusUsed ? "border-emerald-200" : "border-amber-200";

  const fetchLabel =
    info.benchmarkFetchStatus === "search_hit"
      ? "Found in search results"
      : info.benchmarkFetchStatus === "direct_page"
        ? "Fetched from listing page"
        : "Fetch failed";

  const adjSign =
    info.marketAdjustmentPct != null && info.marketAdjustmentPct > 0 ? "+" : "";

  // ── Signal 1: fetch confidence ──────────────────────────────────
  const totalDays = info.fetchStats?.totalDays ?? 0;
  const failedDays = info.fetchStats?.failed ?? 0;
  const primaryDaysFound = Math.max(0, totalDays - failedDays);
  const highDays  = info.fetchStats?.highConfidenceDays ?? 0;
  const lowDays   = info.fetchStats?.lowConfidenceDays  ?? 0;
  const highPct = totalDays > 0 ? highDays / totalDays : 0;
  const lowPct  = totalDays > 0 ? lowDays  / totalDays : 0;
  const confidencePct = totalDays > 0 ? Math.round(highPct * 100) : null;

  let fetchLevel: SignalLevel;
  let fetchDesc: string;
  if (!statusUsed) {
    fetchLevel = "alert";
    fetchDesc  = "Benchmark price was unavailable for this report";
  } else if (highPct >= 0.8) {
    fetchLevel = "good";
    fetchDesc  = "High — benchmark price found live in search results";
  } else if (lowPct >= 0.5) {
    fetchLevel = "warn";
    fetchDesc  = "Lower — price was estimated from the listing page";
  } else {
    fetchLevel = "warn";
    fetchDesc  = "Moderate — some days required fetching directly from the listing page";
  }

  // ── Signal 2: structural match (benchmark vs target) ───────────
  let matchLevel: SignalLevel = "neutral";
  let matchDesc  = "Structural similarity was not assessed";
  const mml = info.benchmarkMismatchLevel;
  if (mml && mml !== "unknown") {
    const simPct =
      info.benchmarkTargetSimilarity != null
        ? ` (${Math.round(info.benchmarkTargetSimilarity * 100)}% structural match)`
        : "";
    if (mml === "high_match") {
      matchLevel = "good";
      matchDesc  = `Good match for your listing${simPct}`;
    } else if (mml === "moderate_mismatch") {
      matchLevel = "warn";
      matchDesc  = `Some structural differences from your listing${simPct} — weight reduced slightly`;
    } else {
      matchLevel = "alert";
      matchDesc  = `Significant differences from your listing${simPct} — benchmark had less influence on the result`;
    }
  }

  // ── Signal 3: market alignment ─────────────────────────────────
  let alignLevel: SignalLevel = "good";
  let alignDesc  = "Benchmark and market prices were generally aligned";
  if (info.conflictDetected) {
    const outlier = info.outlierDays ?? 0;
    alignLevel = "warn";
    alignDesc  = totalDays > 0
      ? `Prices diverged on ${outlier} of ${totalDays} sampled days — system applied a more conservative correction`
      : "Notable price conflict detected — system applied a more conservative correction";
  }

  // ── Signal 4: secondary comps consensus ────────────────────────
  const secComps   = info.secondaryComps ?? [];
  const hasSecondary = secComps.length > 0;
  const secFound   = secComps.filter((s) => s.avgPrice != null).length;
  let secLevel: SignalLevel = "neutral";
  let secDesc  = "No secondary benchmark comps provided";
  if (hasSecondary) {
    const n = `${secFound} secondary comp${secFound !== 1 ? "s" : ""}`;
    if (info.consensusSignal === "strong") {
      secLevel = "good";
      secDesc  = `${n} support the benchmark price`;
    } else if (info.consensusSignal === "divergent") {
      secLevel = "warn";
      secDesc  = `${n} lean toward market prices rather than the benchmark`;
    } else if (info.consensusSignal === "mixed") {
      secLevel = "neutral";
      secDesc  = `${n} showed mixed signals`;
    } else {
      secLevel = "neutral";
      secDesc  = `${n} collected (consensus not determined)`;
    }
  }

  // ── Footer: effective vs nominal market weight ──────────────────
  const nominalPct   = Math.round((info.appliedMarketWeight ?? 0.3) * 100);
  const effectivePct =
    info.effectiveMarketWeight != null
      ? Math.round(info.effectiveMarketWeight * 100)
      : null;
  const maxCapPct = Math.round((info.maxAdjCap ?? 0.25) * 100);

  let weightFooter: string;
  if (effectivePct != null && Math.abs(effectivePct - nominalPct) > 3) {
    if (effectivePct < nominalPct) {
      weightFooter = `Market had less influence than usual — ${effectivePct}% effective weight (standard ${nominalPct}%), cap ±${maxCapPct}%. Benchmark stayed dominant.`;
    } else {
      weightFooter = `Market had more influence than usual — ${effectivePct}% effective weight (standard ${nominalPct}%), cap ±${maxCapPct}%. Benchmark confidence was lower.`;
    }
  } else {
    weightFooter = `Market correction applied at ${effectivePct ?? nominalPct}% weight, capped at ±${maxCapPct}%. Benchmark price stays dominant.`;
  }

  return (
    <div className={`mb-4 rounded-xl border p-4 ${statusColor}`}>
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-sm font-semibold text-gray-900">
            {name || "Your benchmark listing"}
          </p>
          {info.benchmarkUrl && (
            <a
              href={info.benchmarkUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-0.5 block max-w-xs truncate text-xs text-accent hover:underline"
            >
              {info.benchmarkUrl}
            </a>
          )}
        </div>
        <div className="flex flex-wrap gap-1.5">
          <span className="rounded-full bg-gray-900 px-2.5 py-1 text-[10px] font-semibold text-white">
            Pinned by you
          </span>
          <span className={`rounded-full px-2.5 py-1 text-[10px] font-semibold ${badgeColor}`}>
            {statusUsed
              ? "Used as primary benchmark"
              : "Benchmark fetch failed — fallback to market comps"}
          </span>
        </div>
      </div>

      {statusUsed && (
        <p className="mt-2 text-xs font-medium text-emerald-700">
          Your benchmark anchored this estimate — this is your most accurate report.
        </p>
      )}

      {/* All benchmark listings */}
      <div className={`mt-3 space-y-1.5 border-t ${dividerColor} pt-3`}>
        <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
          Benchmark listings
        </p>
        <BenchmarkListingRow
          url={info.benchmarkUrl}
          avgPrice={info.avgBenchmarkPrice}
          daysFound={primaryDaysFound}
          totalDays={totalDays}
          label="Primary"
          isPrimary
        />
        {info.secondaryComps && info.secondaryComps.length > 0 && (
          info.secondaryComps.map((sc, i) => (
            <BenchmarkListingRow
              key={sc.url}
              url={sc.url}
              avgPrice={sc.avgPrice}
              daysFound={sc.daysFound}
              totalDays={sc.totalDays}
              label={`Secondary ${i + 1}`}
            />
          ))
        )}
      </div>

      {/* Stats grid */}
      {statusUsed && (
        <div className={`mt-3 grid grid-cols-2 gap-3 border-t ${dividerColor} pt-3 sm:grid-cols-4`}>
          {confidencePct != null && (
            <div>
              <p className="text-[10px] text-gray-500">Confidence rate</p>
              <p className="text-sm font-semibold">{confidencePct}% high-confidence days</p>
            </div>
          )}
          {info.avgBenchmarkPrice != null && (
            <div>
              <p className="text-[10px] text-gray-500">Benchmark avg</p>
              <p className="text-sm font-semibold">${info.avgBenchmarkPrice}/night</p>
            </div>
          )}
          {info.avgMarketPrice != null && (
            <div>
              <p className="text-[10px] text-gray-500">Market avg</p>
              <p className="text-sm font-semibold">${info.avgMarketPrice}/night</p>
            </div>
          )}
          {info.marketAdjustmentPct != null && (
            <div>
              <p className="text-[10px] text-gray-500">Market offset</p>
              <p className="text-sm font-semibold">
                {adjSign}{info.marketAdjustmentPct}%
              </p>
            </div>
          )}
          <div>
            <p className="text-[10px] text-gray-500">Fetch method</p>
            <p className="text-sm font-semibold">{fetchLabel}</p>
          </div>
        </div>
      )}

      {/* Signal quality section */}
      {statusUsed && (
        <div className={`mt-3 space-y-1.5 border-t ${dividerColor} pt-3`}>
          <p className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
            Signal quality
          </p>
          <SignalRow level={fetchLevel} label="Fetch confidence" description={fetchDesc} />
          {mml && mml !== "unknown" && (
            <SignalRow level={matchLevel} label="Structural match" description={matchDesc} />
          )}
          <SignalRow level={alignLevel} label="Market alignment" description={alignDesc} />
          {hasSecondary && (
            <SignalRow level={secLevel} label="Secondary comps" description={secDesc} />
          )}
        </div>
      )}

      {!statusUsed && info.fallbackReason && (
        <p className="mt-2 text-xs text-amber-700">
          {info.fallbackReason === "benchmark_unavailable_for_dates"
            ? "Your benchmark listing isn't bookable for these dates (its calendar appears blocked or closed), so no benchmark price exists. Market comps were used instead."
            : `Fallback reason: ${info.fallbackReason.replace(/_/g, " ")}. Market comps were used instead.`}
        </p>
      )}

      <p className="mt-3 text-[10px] text-gray-500">{weightFooter}</p>
    </div>
  );
}

// ── Main component ───────────────────────────────────────────────

export function HowWeEstimated({
  report,
  selectedDate,
  clickedDate,
  hideComparableListings = false,
}: {
  report: PricingReport;
  /** Effective price date used for exact priceByDate lookup. */
  selectedDate?: string | null;
  /** The date the user clicked on the heatmap. */
  clickedDate?: string | null;
  /**
   * When true, the comparable listings block is omitted from this section.
   * Use this when the caller renders a contextual comps panel closer to the
   * heatmap and wants to avoid showing a duplicate large comps section here.
   */
  hideComparableListings?: boolean;
}) {
  const target = report.targetSpec ?? report.resultSummary?.targetSpec;
  const criteria = report.queryCriteria ?? report.resultSummary?.queryCriteria;
  const comps = report.compsSummary ?? report.resultSummary?.compsSummary;
  const dist =
    report.priceDistribution ?? report.resultSummary?.priceDistribution;
  const comparableListings =
    report.comparableListings ?? report.resultSummary?.comparableListings;
  const benchmarkInfo =
    report.benchmarkInfo ?? report.resultSummary?.benchmarkInfo ?? null;

  // Real name of the pinned benchmark: name saved when the user added it
  // (matched to benchmarkUrl by room id), else the worker-resolved title.
  const benchmarkName: string | null = (() => {
    const compsArr = report.inputAttributes?.preferredComps;
    const bmUrl = benchmarkInfo?.benchmarkUrl;
    let savedName: string | null = null;
    if (Array.isArray(compsArr) && bmUrl) {
      const roomId = (u: string) => u.match(/\/rooms\/(\d+)/)?.[1] ?? null;
      const bmRoomId = roomId(bmUrl);
      const match = compsArr.find(
        (c) =>
          c.listingUrl &&
          ((bmRoomId != null && roomId(c.listingUrl) === bmRoomId) ||
            c.listingUrl.split("?")[0].toLowerCase() === bmUrl.split("?")[0].toLowerCase())
      );
      savedName = match?.name?.trim() || null;
    }
    return savedName || benchmarkInfo?.benchmarkTitle?.trim() || null;
  })();

  // Pinned comp URLs (from report input) to mark in comparable list
  const pinnedUrls: string[] = (() => {
    const compsArr = report.inputAttributes?.preferredComps;
    const base = Array.isArray(compsArr)
      ? compsArr
      .filter((c) => c.enabled !== false && c.listingUrl)
      .map((c) => c.listingUrl)
      : [];
    const bmUrl = benchmarkInfo?.benchmarkUrl;
    if (typeof bmUrl === "string" && bmUrl.trim()) {
      const exists = base.some((u) => u.split("?")[0].toLowerCase() === bmUrl.split("?")[0].toLowerCase());
      if (!exists) base.unshift(bmUrl);
    }
    return base;
  })();

  const usedForPricing = comps?.usedForPricing ?? 0;
  const availableComparableCount = comparableListings?.length ?? 0;
  const shouldShowComparableSection =
    comparableListings != null ||
    availableComparableCount > 0 ||
    usedForPricing > 0 ||
    benchmarkInfo != null ||
    pinnedUrls.length > 0;
  const comparableCountLabel = availableComparableCount > 0
    ? `${availableComparableCount} available`
    : usedForPricing > 0
      ? `${usedForPricing} used in pricing`
      : "No comparable details";

  // Nothing to show for old reports without transparency data
  if (
    !target &&
    !criteria &&
    !comps &&
    !comparableListings &&
    !benchmarkInfo &&
    pinnedUrls.length === 0
  ) {
    return null;
  }

  return (
    <section className="mb-8">
      <h2 className="mb-4 text-lg font-semibold">How we estimated your price</h2>

      {/* Benchmark block — shown first when present */}
      {benchmarkInfo && (
        <div className="mb-4 overflow-hidden rounded-xl border border-emerald-200 bg-emerald-50/40">
          <div className="border-b border-emerald-100 px-4 py-3">
            <p className="text-sm font-semibold text-gray-900">Benchmark report</p>
            <p className="text-xs text-gray-600">
              This section explains the listing you pinned, how reliably we priced it, and how much it influenced the final recommendation.
            </p>
          </div>
          <div className="px-4 py-4">
            <BenchmarkBlock info={benchmarkInfo} name={benchmarkName} />
          </div>
        </div>
      )}

      <div className="mb-4 grid gap-4 md:grid-cols-2">
        {target && <TargetSpecCard spec={target} />}
        {criteria && <QueryCriteriaCard criteria={criteria} />}
      </div>

      {comps && dist && (
        <CompsDistributionCard comps={comps} distribution={dist} />
      )}

      {shouldShowComparableSection && !hideComparableListings && (
        <div className="mt-4 overflow-hidden rounded-xl border border-amber-200 bg-amber-50/30">
          <div className="flex items-center justify-between gap-3 border-b border-amber-100 px-4 py-3">
            <div>
              <p className="text-sm font-semibold text-gray-900">
                {benchmarkInfo?.benchmarkUsed
                  ? "Comparable listings that shaped your price"
                  : "Comparable listings used in this estimate"}
              </p>
              <p className="text-xs text-gray-600">
                {benchmarkInfo?.benchmarkUsed
                  ? "Your benchmark stays primary. These listings are the market checks we used to confirm or gently adjust it."
                  : "These are the closest comparable listings behind your recommendation, shown up front instead of hidden."}
              </p>
            </div>
            <span className="rounded-full bg-white px-2.5 py-1 text-xs font-semibold text-amber-800 shadow-sm ring-1 ring-amber-200">
              {comparableCountLabel}
            </span>
          </div>
          <div className="px-4 py-4">
            {availableComparableCount > 0 || usedForPricing > 0 ? (
              <ComparableListingsSection
                listings={comparableListings ?? null}
                comps={comps ?? null}
                benchmarkInfo={benchmarkInfo}
                targetSpec={target ?? null}
                pinnedUrls={pinnedUrls}
                selectedDate={selectedDate ?? null}
                clickedDate={clickedDate ?? null}
                embedded
              />
            ) : (
              <p className="text-sm text-gray-600">
                Comparable listing details are not available for this report yet.
              </p>
            )}
          </div>
        </div>
      )}

      {!benchmarkInfo && (
        <p className="mt-4 rounded-xl border border-amber-100 bg-amber-50/50 px-4 py-3 text-xs text-amber-800">
          <span className="font-semibold">Tip:</span> Add a benchmark listing to your next report — paste the Airbnb URL you compete with most for a more accurate, market-anchored estimate.
        </p>
      )}

      {report.recommendedPrice?.notes &&
        report.recommendedPrice.notes !== "" && (
          <p className="mt-3 text-xs text-muted italic">
            {report.recommendedPrice.notes}
          </p>
        )}
    </section>
  );
}
