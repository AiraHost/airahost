import { z } from "zod";

// ── Co-host verification status ─────────────────────────────────────────────

export type CohostVerificationStatus =
  | "not_started"
  | "invite_opened"
  | "user_confirmed"
  | "verification_pending"
  | "verified"
  | "verification_failed";

// ── Property Input ──────────────────────────────────────────────

export const propertyTypeEnum = z.enum([
  "entire_home",
  "private_room",
  "shared_room",
  "hotel_room",
]);

export const amenityEnum = z.enum([
  "wifi",
  "kitchen",
  "washer",
  "dryer",
  "ac",
  "heating",
  "pool",
  "hot_tub",
  "free_parking",
  "pets_allowed",
  "waterfront",
  "guest_favorite",
  "ev_charger",
  "gym",
  "bbq",
  "fire_pit",
  "piano",
  "lake_access",
  "ski_in_out",
  "beach_access",
]);

export const listingInputSchema = z.object({
  address: z.string().min(1, "Please enter a city or ZIP code"),
  propertyType: propertyTypeEnum,
  bedrooms: z.number().int().min(0).max(20),
  bathrooms: z.number().min(0.5).max(20).multipleOf(0.5),
  maxGuests: z.number().int().min(1).max(50),
  sizeSqFt: z.number().int().min(0).max(50000).optional(),
  amenities: z.array(amenityEnum).optional().default([]),
  city: z.string().min(1).max(120).optional(),
  state: z.string().min(1).max(120).optional(),
  postalCode: z.string().min(1).max(20).optional(),
  country: z.string().min(1).max(120).optional(),
  countryCode: z.string().min(2).max(2).optional(),
});

// ── Date Input ──────────────────────────────────────────────────

export const dateInputSchema = z
  .object({
    startDate: z.string().refine((v) => !isNaN(Date.parse(v)), "Invalid date"),
    endDate: z.string().refine((v) => !isNaN(Date.parse(v)), "Invalid date"),
  })
  .refine(
    (d) => {
      const start = new Date(d.startDate);
      const end = new Date(d.endDate);
      const diff = (end.getTime() - start.getTime()) / (1000 * 60 * 60 * 24);
      return diff >= 1 && diff <= 30;
    },
    { message: "Date range must be 1–30 days" }
  );

// ── Discount Policy ─────────────────────────────────────────────

export const discountStackingModeEnum = z.enum([
  "compound",
  "best_only",
  "additive",
]);

export const discountPolicySchema = z.object({
  weeklyDiscountPct: z.number().min(0).max(50).default(0),
  monthlyDiscountPct: z.number().min(0).max(70).default(0),
  refundable: z.boolean().default(true),
  nonRefundableDiscountPct: z.number().min(0).max(30).default(0),
  stackingMode: discountStackingModeEnum.default("compound"),
  maxTotalDiscountPct: z.number().min(0).max(80).default(40),
});

export const lastMinuteStrategyModeEnum = z.enum(["auto", "manual"]);
export const lastMinuteStrategyPreferenceSchema = z.object({
  mode: lastMinuteStrategyModeEnum.default("auto"),
  aggressiveness: z.number().int().min(0).max(100).default(50),
  floor: z.number().min(0.65).max(0.9).default(0.65),
  cap: z.number().min(1.0).max(1.1).default(1.05),
});

// ── Input Mode ──────────────────────────────────────────────────

export const inputModeEnum = z.enum(["url", "criteria", "criteria-by-city", "criteria-by-zip"]);

// ── Preferred Comparables ────────────────────────────────────────

export const preferredCompSchema = z.object({
  listingUrl: z.string().url("Please enter a valid Airbnb listing URL"),
  name: z.string().max(100).optional(),
  note: z.string().max(500).optional(),
  enabled: z.boolean().default(true),
});

/** A list of up to 10 preferred comparable listings. */
export const preferredCompsSchema = z.array(preferredCompSchema).max(10);

export type PreferredComp = z.infer<typeof preferredCompSchema>;
export type PreferredComps = z.infer<typeof preferredCompsSchema>;

// ── Excluded Comparables (per-listing permanent hide) ────────────
// Stored alongside preferredComps in saved_listings.input_attributes.
// roomId (Airbnb room ID) is the stable key — URL is for display + recovery.

export const excludedCompSchema = z.object({
  /** Stable Airbnb room ID extracted from listingUrl (numeric). Canonical key. */
  roomId: z.string().regex(/^\d+$/, "Airbnb room ID must be numeric"),
  /** Original listing URL at moment of exclusion (display + recovery fallback). */
  listingUrl: z.string().url().optional(),
  /** Title snapshot shown in the Excluded panel. */
  title: z.string().max(200).optional(),
  /** Optional user note (Phase 3 — UI not in MVP toast flow). */
  reason: z.string().max(300).optional(),
  /** ISO-8601 timestamp of the exclusion. */
  excludedAt: z.string().datetime(),
});

/** Per-listing list of excluded comparables (cap 200 — power users may accumulate). */
export const excludedCompsSchema = z.array(excludedCompSchema).max(200);

export type ExcludedComp = z.infer<typeof excludedCompSchema>;
export type ExcludedComps = z.infer<typeof excludedCompsSchema>;

// ── Full Report Request ─────────────────────────────────────────

export const createReportRequestSchema = z
  .object({
    inputMode: inputModeEnum.default("criteria"),
    listing: listingInputSchema,
    dates: dateInputSchema,
    discountPolicy: discountPolicySchema,
    lastMinuteStrategy: lastMinuteStrategyPreferenceSchema.optional(),
    listingUrl: z.string().url().optional(),
    preferredComps: preferredCompsSchema.optional(),
    saveToListings: z
      .object({
        enabled: z.boolean().default(false),
        name: z.string().min(1).max(100).optional(),
      })
      .optional(),
  })
  .superRefine((data, ctx) => {
    // For criteria-based input modes, city and state are required.
    // URL mode populates location from the scraped listing instead.
    const isCriteria =
      data.inputMode === "criteria" ||
      data.inputMode === "criteria-by-city" ||
      data.inputMode === "criteria-by-zip";
    if (isCriteria) {
      if (!data.listing.city?.trim()) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["listing", "city"],
          message: "City is required for criteria search",
        });
      }
      if (!data.listing.state?.trim()) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["listing", "state"],
          message: "State is required for criteria search",
        });
      }
    }
  });

export type CreateReportRequest = z.infer<typeof createReportRequestSchema>;
export type InputMode = z.infer<typeof inputModeEnum>;
export type ListingInput = z.infer<typeof listingInputSchema>;
export type DateInput = z.infer<typeof dateInputSchema>;
export type DiscountPolicy = z.infer<typeof discountPolicySchema>;
export type LastMinuteStrategyMode = z.infer<typeof lastMinuteStrategyModeEnum>;
export type LastMinuteStrategyPreference = z.infer<typeof lastMinuteStrategyPreferenceSchema>;
export type PropertyType = z.infer<typeof propertyTypeEnum>;
export type Amenity = z.infer<typeof amenityEnum>;
export type DiscountStackingMode = z.infer<typeof discountStackingModeEnum>;

// ── Report Output Types ─────────────────────────────────────────

export interface CalendarDay {
  date: string;
  dayOfWeek: string;
  isWeekend: boolean;

  // ── CANONICAL USER-FACING RECOMMENDATION ─────────────────────────────────
  /**
   * CANONICAL DAILY RECOMMENDATION FIELD. Use this for all primary UI.
   *
   * Computed as: perDayMarketMedian × demandAdjustment, where demandAdjustment
   * accounts for weekend premium, peak/event flags, and market tightness (range
   * ~0.90–1.05). Last-minute time discounts are intentionally NOT applied — this
   * is the host's recommended list price, not a dynamic revenue-strategy signal.
   *
   * Absent on reports predating the canonical contract. UI must fall back:
   *   const displayPrice = day.recommendedDailyPrice ?? day.basePrice;
   */
  recommendedDailyPrice?: number | null;

  // ── MARKET REFERENCE ─────────────────────────────────────────────────────
  /**
   * Raw per-day market median observed across comparable listings for this date.
   * This is the market REFERENCE signal — what comparable listings charge, unmodified.
   * It is NOT the canonical recommendation. Use this for the market line in charts
   * or market-reference transparency displays.
   *
   * Differs from recommendedDailyPrice: no demand adjustments applied.
   * Absent on interpolated/missing days (null = no comp data for that date).
   */
  baseDailyPrice?: number | null;

  /**
   * User listing nightly price for this exact stay date when available.
   * Null/undefined means no reliable day-level user listing price was captured.
   */
  userListingPrice?: number | null;

  /** Day-of-date flags. e.g. "peak", "low_demand", "missing_data", "interpolated" */
  flags?: string[];

  // ── INTERNAL / ADJUSTMENT PIPELINE ───────────────────────────────────────
  /**
   * Dynamic pricing adjustment metadata for this date (internal / transparency use).
   * Contains the raw demand score, time multiplier, and demand adjustment factor
   * used to compute priceAfterTimeAdjustment. NOT a user-facing recommendation signal.
   */
  dynamicAdjustment?: {
    demandScore: number;
    confidence: "low" | "medium" | "high";
    timeMultiplier: number;   // last-minute discount factor (0.75–1.00); excluded from recommendation
    demandAdjustment: number; // demand signal only (0.90–1.05); IS included in recommendedDailyPrice
    finalMultiplier: number;  // timeMultiplier × demandAdjustment (internal combined factor)
    reasons: string[];
  };
  /**
   * Internal pipeline stage: baseDailyPrice × finalMultiplier (time + demand combined).
   * Includes last-minute discounts for near-term dates. NOT the canonical recommendation.
   * Do not surface directly as "recommended price."
   */
  priceAfterTimeAdjustment?: number | null;
  /** Alias for dynamicAdjustment.timeMultiplier. Internal; not for primary UI. */
  lastMinuteMultiplier?: number | null;
  /**
   * Internal: priceAfterTimeAdjustment with the full discount stack (weekly + monthly +
   * non-refundable discounts) applied. Retained for compatibility; not primary UI.
   */
  effectiveDailyPriceRefundable?: number | null;
  /** Internal: same as effectiveDailyPriceRefundable plus non-refundable discount. */
  effectiveDailyPriceNonRefundable?: number | null;

  // ── LEGACY COMPATIBILITY ──────────────────────────────────────────────────
  // These fields predate the canonical contract. Retained so old-report readers
  // do not break. Do NOT use these as the primary price for new UI work.
  /**
   * @deprecated Use recommendedDailyPrice for new UI.
   * Legacy: equals priceAfterTimeAdjustment (or overallMedian for missing days).
   * Ambiguously named — present on all reports including very old ones.
   * Kept as the fallback in: `day.recommendedDailyPrice ?? day.basePrice`
   */
  basePrice: number;
  /**
   * @deprecated Use recommendedDailyPrice for new UI.
   * Legacy: priceAfterTimeAdjustment with weekly/monthly discount stack applied.
   * Discount concepts have been removed from the user-facing product.
   */
  refundablePrice: number;
  /**
   * @deprecated Use recommendedDailyPrice for new UI.
   * Legacy: refundablePrice with an additional non-refundable cancellation discount.
   * Discount concepts have been removed from the user-facing product.
   */
  nonRefundablePrice: number;
}

// ── Transparency Types ──────────────────────────────────────────

export interface TargetSpec {
  title: string;
  location: string;
  city?: string | null;
  state?: string | null;
  postalCode?: string | null;
  country?: string | null;
  countryCode?: string | null;
  lat?: number | null;
  lng?: number | null;
  propertyType: string;
  accommodates: number | null;
  bedrooms: number | null;
  beds: number | null;
  baths: number | null;
  amenities: string[];
  rating: number | null;
  reviews: number | null;
}

export interface QueryCriteria {
  locationBasis: string;
  searchAdults: number;
  checkin: string;
  checkout: string;
  propertyTypeFilter: string | null;
  tolerances: {
    accommodates: number;
    bedrooms: number;
    beds: number;
    baths: number;
  };
}

export interface CompsSummary {
  collected: number;
  afterFiltering: number;
  usedForPricing: number;
  filterStage: string;
  topSimilarity: number | null;
  avgSimilarity: number | null;
  sampledDays?: number;
  interpolatedDays?: number;
  missingDays?: number;
  /** Comps excluded by the similarity floor (score < filterFloor). Present on new reports only. */
  belowSimilarityFloor?: number;
  /** The minimum similarity score required for a comp to enter pricing. */
  filterFloor?: number;
  /** Days where only 1–2 comps survived the similarity floor (low confidence). */
  lowCompConfidenceDays?: number;
}

export interface PriceDistribution {
  min: number | null;
  p25: number | null;
  median: number | null;
  p75: number | null;
  max: number | null;
  currency: string;
}

export interface ComparableListing {
  id: string;
  title: string;
  propertyType: string;
  accommodates: number;
  bedrooms: number;
  baths: number;
  nightlyPrice: number;
  /** Nightly price keyed by date ("YYYY-MM-DD"). Present on new reports only. */
  priceByDate?: Record<string, number>;
  /**
   * Day-level scraped detail keyed by date ("YYYY-MM-DD").
   * Used to keep date-price alignment with the exact listing URL/query window.
   */
  priceByDateDetails?: Record<
    string,
    {
      price: number;
      url?: string;
      queryNights?: number;
      queryTotalPrice?: number;
    }
  >;
  currency: string;
  similarity: number; // 0–1
  rating: number | null;
  reviews: number | null;
  location: string | null;
  url: string | null;
  /** True when this row is the user's pinned benchmark (primary or secondary). */
  isPinnedBenchmark?: boolean;
  /**
   * Number of nights that the scraped Airbnb card price covered.
   * 1 = "for 1 night" or "/night" — price is already per-night, no change.
   * 2 = "for 2 nights" — original price was a 2-night total; divided by 2.
   * N = "for N nights" — original price was an N-night total; divided by N.
   * Absent on old reports (treat as 1).
   */
  queryNights?: number;
  /**
   * Original trip-total amount when queryNights > 1 and nightlyPrice was
   * normalized by division (e.g. $500 total for 2 nights -> $250 nightlyPrice).
   */
  queryTotalPrice?: number;
  /**
   * Number of sampled days on which this comp appeared in the pricing pool.
   * Higher = more consistently similar across dates. Present on new reports only.
   */
  usedInPricingDays?: number;
}

export interface RecommendedPrice {
  /**
   * CANONICAL TOP-LEVEL RECOMMENDED PRICE.
   * Always equals calendar[0].recommendedDailyPrice (day-0 demand-adjusted recommendation).
   * Pinned in _execute_analysis() after the calendar is built.
   * This is what the dashboard banner, report hero, and alert emails display.
   * Do NOT use weekdayEstimate/weekendEstimate as the primary UI value.
   */
  nightly: number | null;
  /**
   * Pre-canonical weekday estimate from the pricing engine (similarity-weighted).
   * Not aligned to the canonical daily series. Use only for supplementary display.
   * Null when the canonical pin block ran (most new reports).
   */
  weekdayEstimate: number | null;
  /**
   * Pre-canonical weekend estimate from the pricing engine. Same caveats as weekdayEstimate.
   */
  weekendEstimate: number | null;
  /** @deprecated Always 0 in current product. Retained for wire-format compatibility. */
  discountApplied: number;
  /** Diagnostic notes from the pricing engine or canonical pin block. Not primary UI. */
  notes: string;
  /**
   * Secondary context only: the pricing engine's 30-day similarity-weighted recommendation
   * before the canonical pin. Preserved when transparent_result is available.
   * Not shown in primary UI. Absent on older reports and obs-reuse reports.
   */
  windowMedian?: number | null;
}

// ── Benchmark Transparency ───────────────────────────────────────

export interface BenchmarkInfo {
  benchmarkUsed: boolean;
  benchmarkUrl: string;
  /** Real listing name resolved by the worker (user-saved name, PDP title, or search-card title). */
  benchmarkTitle?: string | null;
  /** "search_hit" | "direct_page" | "failed" */
  benchmarkFetchStatus: string;
  benchmarkFetchMethod: string;
  avgBenchmarkPrice: number | null;
  avgMarketPrice: number | null;
  /** Raw market offset vs benchmark, in percent (e.g. +10.5 or -6.2) */
  marketAdjustmentPct: number | null;
  /** Nominal market weight constant (baseline, before confidence/guardrail adjustments) */
  appliedMarketWeight: number;
  /**
   * Average effective market weight actually applied across sampled days.
   * Lower than appliedMarketWeight when benchmark confidence is high and
   * comps are plentiful; higher when confidence is low (market corrects more).
   */
  effectiveMarketWeight?: number | null;
  maxAdjCap: number;
  /**
   * Structural similarity score (0–1) between the benchmark listing and the
   * user's target property attributes (bedrooms, baths, accommodates, type).
   * 1.0 = perfect match; lower = more different.
   * null when user attributes were not available for comparison.
   */
  benchmarkTargetSimilarity?: number | null;
  /**
   * Human-readable classification of the benchmark-to-target similarity.
   * "high_match"          ≥ 0.70 — benchmark is structurally suitable
   * "moderate_mismatch"   0.45–0.70 — some structural differences
   * "strong_mismatch"     < 0.45 — benchmark may be a poor anchor
   * "unknown"             user attributes not provided
   */
  benchmarkMismatchLevel?: "high_match" | "moderate_mismatch" | "strong_mismatch" | "unknown" | null;
  /** Count of sampled days where benchmark vs market gap exceeded 40% */
  outlierDays?: number | null;
  /**
   * True when benchmark and market are in significant conflict:
   * outlier days > 30% of sampled days, or secondary comps signal "divergent".
   */
  conflictDetected?: boolean | null;
  fallbackReason: string | null;
  fetchStats: {
    searchHits: number;
    directFetches: number;
    failed: number;
    totalDays: number;
    /** Phase 2 — confidence breakdown from extract_nightly_price_from_listing_page() */
    highConfidenceDays?: number;
    mediumConfidenceDays?: number;
    lowConfidenceDays?: number;
  };
  /**
   * Phase 2 stub — preferredComps[1:] prices collected from search results.
   * Observational only; does NOT affect pricing formula.
   */
  secondaryComps?: Array<{
    url: string;
    avgPrice: number | null;
    daysFound: number;
    totalDays: number;
  }> | null;
  /**
   * Phase 2 stub — whether secondary comps agree with benchmark or market.
   * "strong"    secondary comps cluster near benchmark price (±20%)
   * "divergent" secondary comps cluster near market price instead
   * "mixed"     no clear consensus
   */
  consensusSignal?: "strong" | "mixed" | "divergent" | null;
}

// ── Worker Progress ──────────────────────────────────────────────

export interface ProgressMeta {
  /** 0–100 percentage complete */
  pct: number;
  /** Stage identifier: "connecting" | "extracting_target" | "fetching_benchmark" | "searching_comps" | "pricing" | "saving_results" | "completed" */
  stage: string;
  /** Human-readable status message for the frontend */
  message: string;
  /** ISO-8601 UTC timestamp when this progress was last written */
  updated_at: string;
  /** Optional estimated seconds remaining */
  est_seconds_remaining?: number | null;
}

// ── Live Price Intelligence ──────────────────────────────────────

export interface LivePriceIntelligence {
  /** Host's actual listed nightly price observed on Airbnb. Null if unavailable. */
  observedListingPrice?: number | null;
  /** ISO date (YYYY-MM-DD) the price was observed for (the report's start_date). */
  observedListingPriceDate?: string | null;
  /** ISO-8601 UTC timestamp when the price was captured. */
  observedListingPriceCapturedAt?: string | null;
  /** How the price was extracted: "ld_json" | "booking_widget" | "body_text" */
  observedListingPriceSource?: string | null;
  /** Extraction confidence: "high" | "medium" | "low" | "failed" */
  observedListingPriceConfidence?: string | null;

  // ── Comparison intelligence (set when observedListingPrice is available) ──

  /** observed - market_median (positive = above market) */
  observedVsMarketDiff?: number | null;
  /** Percentage above/below market: round((obs/median - 1) * 100) */
  observedVsMarketDiffPct?: number | null;
  /** observed - recommended_price (positive = above recommendation) */
  observedVsRecommendedDiff?: number | null;
  /** Percentage above/below recommendation */
  observedVsRecommendedDiffPct?: number | null;
  /** "above_market" | "at_market" | "below_market" */
  pricingPosition?: "above_market" | "at_market" | "below_market" | null;
  /** Suggested action based on comparison to recommendation */
  pricingAction?: "raise" | "lower" | "keep" | null;
  /** Target price for the suggested action */
  pricingActionTarget?: number | null;

  // ── Status ──────────────────────────────────────────────────────────────
  /** "captured" | "no_listing_url" | "scrape_failed" | "no_price_found" */
  livePriceStatus?: string | null;
  livePriceStatusReason?: string | null;
}

// ── Summary & Report ────────────────────────────────────────────

export interface ReportSummary extends LivePriceIntelligence {
  insightHeadline: string;

  // ── Market proxy stats ───────────────────────────────────────
  // Backward-compatible market reference metrics derived from the legacy
  // basePrice field (= priceAfterTimeAdjustment or overallMedian, depending
  // on whether the day had valid comp data). These approximate the market
  // but are NOT guaranteed to be raw unadjusted market medians — they may
  // reflect time/demand multipliers on near-term dates in the scrape window.
  // Use for "Market median" KPI, alert comparison baselines, and revenue
  // estimates. NOT the canonical recommendation (use recommendedPrice.nightly).
  /** Window low end — market proxy. Derived from legacy basePrice. */
  nightlyMin: number;
  /**
   * Window midpoint — market proxy / backward-compatible market reference.
   * Derived from legacy basePrice (not guaranteed to be the raw per-day
   * comp median; may include time/demand adjustments on near-term dates).
   * Use for "Market median" KPI displays and alert comparison baselines.
   * The canonical recommendation is recommendedPrice.nightly (= day-0 recommendedDailyPrice).
   */
  nightlyMedian: number;
  /** Window high end — market proxy. Derived from legacy basePrice. */
  nightlyMax: number;
  occupancyPct: number;
  /** Market-proxy weekday average. Derived from legacy basePrice. Reference only. */
  weekdayAvg: number;
  /** Market-proxy weekend average. Derived from legacy basePrice. Reference only. */
  weekendAvg: number;
  estimatedMonthlyRevenue: number;
  weeklyStayAvgNightly: number;
  monthlyStayAvgNightly: number;
  selectedRangeNights?: number;
  selectedRangeAvgNightly?: number;
  stayLengthAverages?: Array<{
    stayLength: number;
    avgNightly: number;
    lengthDiscountPct: number;
  }>;

  // ── Canonical recommendation ──────────────────────────────────
  // recommendedPrice.nightly is the primary "Recommended Price" for all UI surfaces.
  // It equals calendar[0].recommendedDailyPrice (pinned in worker after calendar build).
  recommendedPrice?: RecommendedPrice;

  // ── Embedded transparency fields (present in new reports) ─────
  targetSpec?: TargetSpec;
  queryCriteria?: QueryCriteria;
  compsSummary?: CompsSummary;
  priceDistribution?: PriceDistribution;
  comparableListings?: ComparableListing[];
  benchmarkInfo?: BenchmarkInfo;

  /**
   * Snapshot of excluded comp roomIds at the moment this report was generated.
   * Frontend uses superset-check vs current `input_attributes.excludedComps`
   * to decide banner wording: "X hidden locally" vs "Pricing excludes X hidden".
   * Empty array when no exclusions were active during the run.
   */
  excludedRoomIdsAtRun?: string[];
}

export interface PricingReport {
  id: string;
  shareId: string;
  createdAt: string;
  status: "queued" | "running" | "ready" | "error";
  coreVersion: string;
  inputAddress: string;
  inputAttributes: ListingInput & {
    inputMode?: InputMode;
    listingUrl?: string | null;
    lastMinuteStrategy?: LastMinuteStrategyPreference;
    preferredComps?: PreferredComps | null;
  };
  inputDateStart: string;
  inputDateEnd: string;
  discountPolicy: DiscountPolicy;
  resultSummary: ReportSummary | null;
  resultCalendar: CalendarDay[] | null;
  errorMessage: string | null;
  workerAttempts?: number;
  // Top-level transparency fields (extracted from resultSummary by API)
  targetSpec?: TargetSpec | null;
  queryCriteria?: QueryCriteria | null;
  compsSummary?: CompsSummary | null;
  priceDistribution?: PriceDistribution | null;
  recommendedPrice?: RecommendedPrice | null;
  comparableListings?: ComparableListing[] | null;
  benchmarkInfo?: BenchmarkInfo | null;
  /** Worker progress snapshot. Null until the worker first calls update_progress(). */
  progressMeta?: ProgressMeta | null;
  /** ISO-8601 UTC timestamp of last worker heartbeat. Null for cached/old reports. */
  workerHeartbeatAt?: string | null;
}

// ── Saved Listings ─────────────────────────────────────────────

export const dateModeEnum = z.enum(["next_30", "custom"]);
export type DateMode = z.infer<typeof dateModeEnum>;

export const createListingSchema = z.object({
  name: z.string().min(1, "Name is required").max(100),
  inputAddress: z.string().min(5, "Please enter a valid address"),
  inputAttributes: listingInputSchema,
  defaultDiscountPolicy: discountPolicySchema.optional(),
});

export const updateListingSchema = z.object({
  name: z.string().min(1).max(100).optional(),
  inputAddress: z.string().min(5).optional(),
  inputAttributes: listingInputSchema.optional(),
  defaultDiscountPolicy: discountPolicySchema.optional(),
  defaultDateMode: dateModeEnum.optional(),
  defaultStartDate: z.string().nullable().optional(),
  defaultEndDate: z.string().nullable().optional(),
  preferredComps: preferredCompsSchema.nullable().optional(),
  excludedComps: excludedCompsSchema.nullable().optional(),
  pricingAlertsEnabled: z.boolean().optional(),
  minimumBookingNights: z.number().int().min(1).max(30).optional(),
  /** Update the Airbnb listing URL stored in input_attributes.listingUrl */
  listingUrl: z.string().url().nullable().optional(),
  // Auto-Apply settings (migration 017 + 019)
  autoApplyEnabled: z.boolean().optional(),
  // Max 30: recommendation system only covers the next 30 days.
  autoApplyWindowEndDays: z.number().int().min(1).max(30).optional(),
  autoApplyScope: z.enum(["actionable", "all_sellable"]).optional(),
  autoApplyMinPriceFloor: z.number().positive().nullable().optional(),
  autoApplyMinNoticeDays: z.number().int().min(0).max(30).optional(),
  autoApplyMaxIncreasePct: z.number().positive().max(200).nullable().optional(),
  autoApplyMaxDecreasePct: z.number().positive().max(100).nullable().optional(),
  autoApplySkipUnavailable: z.boolean().optional(),
  // Co-host invite-opened transition (migration 020).
  // Setting true transitions status from not_started → invite_opened.
  // All other co-host status transitions go through /cohost-verify.
  autoApplyCohostInviteOpened: z.boolean().optional(),
});

export interface SavedListing {
  id: string;
  userId: string;
  name: string;
  inputAddress: string;
  inputAttributes: ListingInput & {
    preferredComps?: PreferredComps | null;
    excludedComps?: ExcludedComps | null;
    postalCodePrefix?: string;
    locationSource?: string;
  };
  defaultDiscountPolicy: DiscountPolicy | null;
  defaultDateMode: DateMode;
  defaultStartDate: string | null;
  defaultEndDate: string | null;
  lastUsedAt: string | null;
  createdAt: string;
  updatedAt: string;
  // Pricing alert fields (migration 014)
  pricingAlertsEnabled: boolean;
  lastAlertSentAt: string | null;
  lastAlertDirection: string | null;
  lastLivePriceStatus: string | null;
  // Alert v2 fields (migration 015)
  minimumBookingNights: number;
  listingUrlValidationStatus: string | null;
  listingUrlValidatedAt: string | null;
  // Auto-Apply settings (migration 017 + 019)
  autoApplyEnabled: boolean;
  autoApplyWindowEndDays: number;
  autoApplyScope: "actionable" | "all_sellable";
  autoApplyMinPriceFloor: number | null;
  autoApplyMinNoticeDays: number;
  autoApplyMaxIncreasePct: number | null;
  autoApplyMaxDecreasePct: number | null;
  autoApplySkipUnavailable: boolean;
  autoApplyLastUpdatedAt: string | null;
  // Co-host verification model (migration 020)
  autoApplyCohostStatus: CohostVerificationStatus;
  autoApplyCohostConfirmedAt: string | null;
  autoApplyCohostVerifiedAt: string | null;
  autoApplyCohostVerificationError: string | null;
  autoApplyCohostVerificationMethod: string | null;
  /** @deprecated Use autoApplyCohostStatus instead. */
  autoApplyCohostReady: boolean;
}

export interface ListingReport {
  id: string;
  savedListingId: string;
  pricingReportId: string;
  trigger: "manual" | "rerun" | "scheduled";
  createdAt: string;
}

export const rerunListingSchema = z.object({
  dates: dateInputSchema,
  discountPolicy: discountPolicySchema.optional(),
  inputMode: inputModeEnum.optional(),
  listingUrl: z.string().url().optional(),
  preferredComps: preferredCompsSchema.optional(),
});

export const listingAnalysisSchema = z.object({
  listingId: z.string().uuid(),
  listingUrl: z.string().url().optional(),
  dateRange: dateInputSchema,
});

export type CreateListingRequest = z.infer<typeof createListingSchema>;
export type UpdateListingRequest = z.infer<typeof updateListingSchema>;
export type RerunListingRequest = z.infer<typeof rerunListingSchema>;
export type ListingAnalysisRequest = z.infer<typeof listingAnalysisSchema>;

// ── Market Tracking ─────────────────────────────────────────────

export const trackMarketRequestSchema = z.object({
  email: z.string().email("Please enter a valid email"),
  address: z.string().min(5),
  notifyWeekly: z.boolean().default(false),
  notifyUnderMarket: z.boolean().default(false),
});

export type TrackMarketRequest = z.infer<typeof trackMarketRequestSchema>;
