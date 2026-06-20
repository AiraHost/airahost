"use client";

import { useEffect, useMemo, useState } from "react";
import type {
  BenchmarkInfo,
  ComparableListing,
  CompsSummary,
  ExcludedComp,
} from "@/lib/schemas";

// â”€â”€ Stable identifier helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// roomId is the canonical key for all comp-level decisions (pinned,
// excluded, conflict).  URLs are display-only / fallback.

function extractRoomId(url: string | null | undefined): string | null {
  if (!url) return null;
  const m = url.match(/\/rooms\/(\d+)/);
  return m ? m[1] : null;
}

function listingRoomId(listing: ComparableListing): string | null {
  // Worker fills `id` with build_comp_id() â€” usually room ID, falls back to URL.
  // Prefer `id` first; if it's not numeric, derive from `url`.
  if (listing.id && /^\d+$/.test(listing.id)) return listing.id;
  return extractRoomId(listing.url);
}

function isPinnedListing(
  listing: ComparableListing,
  pinnedRoomIds: string[]
): boolean {
  const flaggedPinned =
    (listing as ComparableListing & { isPinnedBenchmark?: boolean })
      .isPinnedBenchmark === true;
  if (flaggedPinned) return true;
  const rid = listingRoomId(listing);
  if (rid && pinnedRoomIds.includes(rid)) return true;
  return false;
}

// â”€â”€ Date helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function nextDay(dateStr: string, nights = 1): string {
  const d = new Date(dateStr + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + Math.max(1, nights));
  return d.toISOString().slice(0, 10);
}

function listingUrlForDate(url: string, date: string, nights = 1): string {
  const checkout = nextDay(date, nights);
  try {
    // Canonicalize: strip any existing date params (old or new key names),
    // then inject the correct Airbnb params check_in / check_out.
    const u = new URL(url, "https://www.airbnb.com");
    u.searchParams.delete("checkin");
    u.searchParams.delete("checkout");
    u.searchParams.delete("check_in");
    u.searchParams.delete("check_out");
    u.searchParams.set("check_in", date);
    u.searchParams.set("check_out", checkout);
    return u.toString();
  } catch {
    // Fallback for relative or malformed URLs â€” safe append.
    const sep = url.includes("?") ? "&" : "?";
    return `${url}${sep}check_in=${date}&check_out=${checkout}`;
  }
}

// â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function similarityBadgeClasses(similarity: number): string {
  if (similarity >= 0.8) return "bg-green-50 text-green-700";
  if (similarity >= 0.6) return "bg-yellow-50 text-yellow-700";
  return "bg-gray-100 text-gray-600";
}

function formatComparableNightlyPrice(price: number, queryNights: number): string {
  if (queryNights <= 1) {
    return String(Math.ceil(price));
  }
  return String(Number(price.toFixed(2)));
}

function matchQualityLabel(stage: string): {
  label: string;
  description: string;
  classes: string;
} {
  switch (stage) {
    case "strict":
      return {
        label: "Strong match set",
        description: "Most of these listings are very close matches to your home.",
        classes: "bg-green-50 text-green-700",
      };
    case "medium":
      return {
        label: "Balanced match set",
        description: "We found a good mix of close matches and broader nearby comps.",
        classes: "bg-yellow-50 text-yellow-700",
      };
    default:
      return {
        label: "Broader match set",
        description: "There were fewer near-identical listings, so we used a wider comparison set.",
        classes: "bg-gray-100 text-gray-700",
      };
  }
}

function formatPropertyType(type: string | null | undefined): string {
  if (!type) return "";
  return type
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatComparableSpecs(listing: ComparableListing): string {
  const parts: string[] = [];
  const propertyType = formatPropertyType(listing.propertyType);
  if (propertyType) parts.push(propertyType);

  if (typeof listing.accommodates === "number") {
    parts.push(`${listing.accommodates} guest${listing.accommodates !== 1 ? "s" : ""}`);
  }
  if (typeof listing.bedrooms === "number") {
    parts.push(`${listing.bedrooms} bd`);
  }
  if (typeof listing.baths === "number") {
    parts.push(`${listing.baths} ba`);
  }

  return parts.join(" | ");
}

// â”€â”€ Skeleton â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function SkeletonCard() {
  return (
    <div className="animate-pulse rounded-xl border border-gray-100 p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1 space-y-2.5">
          <div className="h-4 w-3/5 rounded bg-gray-100" />
          <div className="h-3.5 w-4/5 rounded bg-gray-50" />
        </div>
        <div className="shrink-0 space-y-2 text-right">
          <div className="ml-auto h-6 w-24 rounded bg-gray-100" />
          <div className="ml-auto h-5 w-16 rounded-full bg-gray-50" />
        </div>
      </div>
      <div className="mt-3 flex gap-4 border-t border-gray-50 pt-3">
        <div className="h-3.5 w-20 rounded bg-gray-50" />
        <div className="h-3.5 w-16 rounded bg-gray-50" />
      </div>
    </div>
  );
}

// â”€â”€ Mobile overflow menu (â€¢â€¢â€¢ kebab) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function MobileCompActionMenu({
  listing,
  alreadyBenchmark,
  onExclude,
  onPromote,
}: {
  listing: ComparableListing;
  alreadyBenchmark?: boolean;
  onExclude?: (listing: ComparableListing) => void;
  onPromote?: (listing: ComparableListing) => void;
}) {
  const [open, setOpen] = useState(false);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const onDown = (ev: MouseEvent) => {
      const target = ev.target as HTMLElement | null;
      if (!target || !target.closest?.("[data-mobile-comp-menu]")) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  return (
    <div className="absolute right-2 top-2 md:hidden" data-mobile-comp-menu>
      <button
        type="button"
        data-testid="comp-action-overflow"
        aria-label="More actions"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        className="rounded-md bg-white/95 p-1.5 text-gray-500 ring-1 ring-gray-200 shadow-sm transition hover:bg-gray-50"
      >
        <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
          <circle cx="3" cy="8" r="1.5" />
          <circle cx="8" cy="8" r="1.5" />
          <circle cx="13" cy="8" r="1.5" />
        </svg>
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 top-9 z-10 w-48 overflow-hidden rounded-md bg-white text-sm shadow-lg ring-1 ring-gray-200"
        >
          <button
            type="button"
            data-testid="comp-action-promote"
            role="menuitem"
            disabled={alreadyBenchmark}
            onClick={(e) => {
              e.stopPropagation();
              setOpen(false);
              onPromote?.(listing);
            }}
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-amber-700 hover:bg-amber-50 disabled:cursor-not-allowed disabled:text-gray-400"
          >
            <span aria-hidden="true">â­</span>
            {alreadyBenchmark ? "Already a benchmark" : "Use as benchmark"}
          </button>
          <button
            type="button"
            data-testid="comp-action-exclude"
            role="menuitem"
            onClick={(e) => {
              e.stopPropagation();
              setOpen(false);
              onExclude?.(listing);
            }}
            className="flex w-full items-center gap-2 px-3 py-2 text-left text-rose-700 hover:bg-rose-50"
          >
            <span aria-hidden="true">âŠ˜</span>
            Hide from future reports
          </button>
        </div>
      )}
    </div>
  );
}

// â”€â”€ Listing Card â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

interface ComparableCardProps {
  listing: ComparableListing;
  isPinned?: boolean;
  selectedDate?: string | null;
  /** True when the user can act on this card (dashboard view, not share view). */
  canManage?: boolean;
  /** True if this card is in the process of being hidden (animation hint). */
  isExiting?: boolean;
  /** True if the comp is already a benchmark â€” disable the Promote button. */
  alreadyBenchmark?: boolean;
  /** True if pressing Promote should open a "Replace which?" picker (â‰¥10). */
  promoteAtCap?: boolean;
  onExclude?: (listing: ComparableListing) => void;
  onPromote?: (listing: ComparableListing) => void;
}

function ComparableCard({
  listing,
  isPinned = false,
  selectedDate,
  canManage = false,
  isExiting = false,
  alreadyBenchmark = false,
  promoteAtCap = false,
  onExclude,
  onPromote,
}: ComparableCardProps) {
  const [isHovered, setIsHovered] = useState(false);
  // Action icons require a stable numeric roomId â€” without one we can't write
  // a guaranteed-stable excludedComps entry, so the action would 400 at the
  // schema level.  Hide rather than letting the user hit a wall.
  const hasStableRoomId = !!listingRoomId(listing);
  const showActions = canManage && hasStableRoomId && !isExiting;
  const matchPct = Math.round(listing.similarity * 100);
  const badgeClasses = similarityBadgeClasses(listing.similarity);

  // When a date is selected, use only the exact scraped price for that date.
  // Do NOT fall back to nightlyPrice or an average â€” that would mislead the user.
  const datePrice: number | undefined = selectedDate
    ? (listing.priceByDateDetails?.[selectedDate]?.price ?? listing.priceByDate?.[selectedDate])
    : undefined;
  // No date selected â†’ show the general comparable price.
  // Date selected + price found â†’ show sampled date price.
  // Date selected + no price â†’ show unavailable (not a fallback average).
  const hasSampledDatePrice = selectedDate != null && datePrice != null;
  const isPriceUnavailable = selectedDate != null && datePrice == null;
  const displayPrice = selectedDate ? datePrice : listing.nightlyPrice;
  const hasDisplayPrice =
    typeof displayPrice === "number" && Number.isFinite(displayPrice) && displayPrice > 0;
  const detailForDate = selectedDate ? listing.priceByDateDetails?.[selectedDate] : undefined;
  const queryNights =
    selectedDate && detailForDate?.queryNights != null
      ? detailForDate.queryNights
      : listing.queryNights != null
        ? listing.queryNights
        : 1;
  const queryTotalPrice =
    queryNights > 1
      ? typeof detailForDate?.queryTotalPrice === "number" && detailForDate.queryTotalPrice > 0
        ? detailForDate.queryTotalPrice
        : typeof listing.queryTotalPrice === "number" && listing.queryTotalPrice > 0
          ? listing.queryTotalPrice
        : typeof displayPrice === "number" && displayPrice > 0
          ? Number((displayPrice * queryNights).toFixed(2))
          : null
      : null;

  // When a specific date is selected, append checkin/checkout so the Airbnb
  // listing page opens in the same date context as our shown price.
  const viewUrl = listing.url
    ? selectedDate
      ? detailForDate?.url
        ? detailForDate.url
        : listingUrlForDate(listing.url, selectedDate, queryNights)
      : listing.url
    : null;

  return (
    <div
      data-testid="comparable-card"
      data-state={isExiting ? "exiting" : "idle"}
      data-room-id={listingRoomId(listing) ?? ""}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      style={
        isExiting
          ? {
              opacity: 0,
              transform: "scale(0.98)",
              transition: "opacity 200ms ease-out, transform 200ms ease-out",
            }
          : undefined
      }
      className={`group relative rounded-xl border p-4 transition hover:shadow-sm ${
        isPinned ? "border-amber-300 bg-amber-50/70 shadow-sm ring-1 ring-amber-100" : "border-gray-100 bg-white"
      }`}
    >
      {/* Hover-reveal action icons (desktop only â€” md+) */}
      {showActions && (
        <div
          className={`pointer-events-none absolute right-3 top-3 hidden gap-1 transition-opacity duration-150 ease-out md:flex ${
            isHovered ? "opacity-100" : "opacity-0"
          } group-focus-within:opacity-100`}
          aria-hidden="true"
        >
          <button
            type="button"
            data-testid="comp-action-promote"
            disabled={alreadyBenchmark}
            onClick={(e) => {
              e.stopPropagation();
              onPromote?.(listing);
            }}
            title={
              alreadyBenchmark
                ? "Already a benchmark"
                : promoteAtCap
                ? "Replace a benchmark"
                : "Use as benchmark"
            }
            aria-label="Use as benchmark"
            className="pointer-events-auto rounded-md bg-white/95 p-1.5 text-amber-600 ring-1 ring-amber-200 shadow-sm transition hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
              <path d="M8 1.5l1.95 3.95 4.36.63-3.16 3.08.74 4.34L8 11.46l-3.9 2.04.74-4.34L1.7 6.08l4.36-.63L8 1.5z" />
            </svg>
          </button>
          <button
            type="button"
            data-testid="comp-action-exclude"
            onClick={(e) => {
              e.stopPropagation();
              onExclude?.(listing);
            }}
            title="Hide from future reports"
            aria-label="Hide from future reports"
            className="pointer-events-auto rounded-md bg-white/95 p-1.5 text-rose-500 ring-1 ring-rose-200 shadow-sm transition hover:bg-rose-50"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
              <circle cx="8" cy="8" r="6" />
              <path d="M3.5 12.5 L12.5 3.5" />
            </svg>
          </button>
        </div>
      )}

      {/* Mobile overflow menu (â€¢â€¢â€¢ kebab â€” replaces hover on touch devices) */}
      {showActions && (
        <MobileCompActionMenu
          listing={listing}
          alreadyBenchmark={alreadyBenchmark}
          onExclude={onExclude}
          onPromote={onPromote}
        />
      )}

      {/* Top row: title+specs LEFT, price+badge RIGHT */}
      <div className="flex items-start justify-between gap-4">
        {/* Left zone */}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="truncate text-base font-medium text-gray-900">
              {listing.title}
            </p>
            {isPinned && (
              <span className="shrink-0 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800 ring-1 ring-amber-200">
                Pinned by you
              </span>
            )}
          </div>
          <p className="mt-0.5 text-sm text-gray-600">
            {formatComparableSpecs(listing)}
          </p>
        </div>

        {/* Right zone */}
        <div className="shrink-0 text-right">
          {isPriceUnavailable ? (
            <div className="text-right">
              <p className="text-sm font-medium text-gray-400">
                No scraped data for this date
              </p>
            </div>
          ) : hasDisplayPrice ? (
            <p className="text-2xl font-semibold text-gray-900">
              ${formatComparableNightlyPrice(displayPrice, queryNights)}
              <span className="text-sm font-normal text-gray-500"> / night</span>
            </p>
          ) : null}
          {hasSampledDatePrice && (
            <p className="text-[10px] text-emerald-600 font-medium">
              exact date price
            </p>
          )}
          <p className={`text-[10px] font-medium ${queryNights > 1 ? "text-amber-600" : "text-gray-400"}`}>
            {queryNights > 1 ? `${queryNights}-night-derived / night` : "1-night price"}
          </p>
          {queryNights > 1 && queryTotalPrice != null && !isPriceUnavailable && hasDisplayPrice && (
            <p className="text-[10px] text-amber-700">
              {`From $${queryTotalPrice} total for ${queryNights} nights`}
            </p>
          )}
          <span
            className={`mt-1 inline-block rounded-full px-2 py-1 text-xs font-medium ${badgeClasses}`}
          >
            {matchPct}% match
          </span>
        </div>
      </div>

      {/* Bottom meta row */}
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-gray-50 pt-3 text-sm text-gray-500">
        {listing.rating != null && (
          <span>
            <span className="text-amber-500">&#9733;</span>
            {listing.rating.toFixed(2)}
            {listing.reviews != null && (
              <span className="text-gray-400"> ({listing.reviews})</span>
            )}
          </span>
        )}
        {listing.location && <span>{listing.location}</span>}
        {viewUrl && (
          <a
            href={viewUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="ml-auto text-xs font-medium text-accent hover:underline"
          >
            View &rarr;
          </a>
        )}
      </div>
    </div>
  );
}

// â”€â”€ Section â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

type SortMode = "similarity" | "price";

interface ComparableListingsSectionProps {
  listings: ComparableListing[] | null | undefined;
  comps: CompsSummary | null | undefined;
  benchmarkInfo?: BenchmarkInfo | null;
  loading?: boolean;
  embedded?: boolean;
  /**
   * @deprecated use pinnedRoomIds (roomId-first) instead.
   * Kept for backward-compat â€” derived to roomIds internally.
   */
  pinnedUrls?: string[];
  /** Stable room IDs of comps that are user benchmarks. */
  pinnedRoomIds?: string[];
  /** Effective price date for exact day-level comp filtering. */
  selectedDate?: string | null;
  /** The date the user clicked. */
  clickedDate?: string | null;
  // â”€â”€ Per-listing manage controls (dashboard view only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  /** Room IDs the user has excluded â€” filtered out at render time. */
  excludedRoomIds?: string[];
  /** Full ExcludedComp objects for the Manage popover (title + restore). */
  excludedDetails?: ExcludedComp[];
  /** Snapshot from report.excludedRoomIdsAtRun â€” controls banner wording. */
  reportExcludedRoomIdsAtRun?: string[] | null;
  /**
   * True only when the viewer owns the listing.  Public share view passes
   * false â†’ action icons / banner / Manage all hidden.
   */
  canManageComps?: boolean;
  onExcludeComp?: (listing: ComparableListing) => void;
  onPromoteComp?: (
    listing: ComparableListing,
    opts?: { unexcludeRoomId?: string }
  ) => void;
  onRestoreExcluded?: (roomId: string) => void;
  onRerun?: () => Promise<void> | void;
  /**
   * Called when the user tries to Exclude a comp that is currently a benchmark.
   * Should open an inline confirm dialog and, on confirm, atomically remove
   * from preferredComps + add to excludedComps.
   */
  onExcludeBenchmarkConflict?: (
    listing: ComparableListing,
    benchmarkRoomId: string
  ) => void;
}

export function ComparableListingsSection({
  listings,
  comps,
  loading = false,
  embedded = false,
  pinnedUrls = [],
  pinnedRoomIds: pinnedRoomIdsProp,
  selectedDate = null,
  excludedRoomIds = [],
  excludedDetails = [],
  reportExcludedRoomIdsAtRun = null,
  canManageComps = false,
  onExcludeComp,
  onPromoteComp,
  onRestoreExcluded,
  onRerun,
  onExcludeBenchmarkConflict,
}: ComparableListingsSectionProps) {
  const [sortBy, setSortBy] = useState<SortMode>("similarity");
  const [expanded, setExpanded] = useState(false);
  const [manageOpen, setManageOpen] = useState(false);
  const [rerunStarting, setRerunStarting] = useState(false);
  const [conflictDialog, setConflictDialog] = useState<{
    type: "exclude-benchmark" | "promote-excluded";
    listing: ComparableListing;
    targetRoomId: string;
  } | null>(null);
  // Cards currently animating out.  Exit window: 200 ms.
  // While a card's roomId is in this set, it stays rendered with
  // data-state="exiting" even after the parent's optimistic excludedRoomIds
  // would otherwise filter it away.
  const [exitingRoomIds, setExitingRoomIds] = useState<Set<string>>(
    () => new Set()
  );

  // Derive canonical pinnedRoomIds from new prop or legacy pinnedUrls.
  const pinnedRoomIds = useMemo<string[]>(() => {
    if (pinnedRoomIdsProp && pinnedRoomIdsProp.length > 0) return pinnedRoomIdsProp;
    return pinnedUrls
      .map((u) => extractRoomId(u))
      .filter((rid): rid is string => Boolean(rid));
  }, [pinnedRoomIdsProp, pinnedUrls]);

  const excludedSet = useMemo(
    () => new Set(excludedRoomIds),
    [excludedRoomIds]
  );

  const reportSnapshotSet = useMemo(
    () => new Set(reportExcludedRoomIdsAtRun ?? []),
    [reportExcludedRoomIdsAtRun]
  );

  // Banner two-state logic:
  //   - hasPendingHide: there are excluded comps not yet reflected in this report
  //   - alreadyApplied: every current excluded id is already in the report snapshot
  //                     â†’ "Pricing excludes X hidden comparables" (no Re-run button)
  //   - mixed/pending â†’ "X comparables hidden locally. Re-run to update pricing."
  const allExcludedAlreadyAtRun = excludedRoomIds.every((rid) =>
    reportSnapshotSet.has(rid)
  );
  const hiddenCount = excludedRoomIds.length;

  const matchQuality = comps
    ? matchQualityLabel(comps.filterStage)
    : null;

  const sorted = useMemo(() => {
    if (!listings || listings.length === 0) return [];
    let filtered = selectedDate
      ? listings.filter((listing) => listing.priceByDate?.[selectedDate] != null)
      : listings;
    // Render-time filter: drop user-excluded comps so the card list reflects
    // the (eventually) post-rerun report.  canManageComps=false on share view
    // â†’ snapshot semantics, don't filter.
    //
    // Exception: cards in `exitingRoomIds` stay rendered (with isExiting=true)
    // even though they're already in the optimistic excludedSet â€” this lets
    // the 200 ms exit animation actually play out before the DOM removal.
    if (canManageComps && excludedSet.size > 0) {
      filtered = filtered.filter((listing) => {
        const rid = listingRoomId(listing);
        if (!rid) return true;
        if (!excludedSet.has(rid)) return true;
        return exitingRoomIds.has(rid);
      });
    }
    const copy = [...filtered];
    // When a date is selected, use only the exact date price (may be undefined).
    // When no date is selected, fall back to the general nightlyPrice.
    const getPrice = (listing: ComparableListing): number | undefined =>
      selectedDate ? listing.priceByDate?.[selectedDate] : listing.nightlyPrice;
    const comparePinned = (a: ComparableListing, b: ComparableListing) => {
      const aPinned = isPinnedListing(a, pinnedRoomIds);
      const bPinned = isPinnedListing(b, pinnedRoomIds);
      if (aPinned === bPinned) return 0;
      return aPinned ? -1 : 1;
    };
    if (sortBy === "similarity") {
      copy.sort((a, b) => comparePinned(a, b) || b.similarity - a.similarity);
    } else {
      // Listings with no price for the selected date sort to the end.
      copy.sort((a, b) => {
        const pinned = comparePinned(a, b);
        if (pinned !== 0) return pinned;
        const pa = getPrice(a);
        const pb = getPrice(b);
        if (pa == null && pb == null) return 0;
        if (pa == null) return 1;
        if (pb == null) return -1;
        return pa - pb;
      });
    }
    return copy;
  }, [
    listings,
    sortBy,
    pinnedRoomIds,
    selectedDate,
    canManageComps,
    excludedSet,
    exitingRoomIds,
  ]);

  const initialVisibleCount = embedded ? 5 : 10;
  const visible = expanded ? sorted : sorted.slice(0, initialVisibleCount);
  const hasMore = sorted.length > initialVisibleCount && !expanded;
  const canCollapse = expanded && sorted.length > initialVisibleCount;

  // Subtext
  const used = comps?.usedForPricing ?? sorted.length;
  const locationBasis = "your area"; // fallback; caller can pass queryCriteria
  const showingCount = visible.length;
  const nonPinnedVisibleForDate = selectedDate
    ? visible.filter((listing) => !isPinnedListing(listing, pinnedRoomIds))
    : [];
  const hasAnyComparableDataForSelectedDate = !!(
    selectedDate &&
    (nonPinnedVisibleForDate.length > 0
      ? nonPinnedVisibleForDate.some((listing) => listing.priceByDate?.[selectedDate] != null)
      : visible.some((listing) => listing.priceByDate?.[selectedDate] != null))
  );

  // â”€â”€ Loading state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  if (loading) {
    return (
      <section className={embedded ? "" : "mb-8"}>
        {!embedded && (
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-lg font-semibold">Comparable listings</h2>
          </div>
        )}
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <SkeletonCard key={i} />
          ))}
        </div>
      </section>
    );
  }

  // â”€â”€ Empty state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  if (!listings || listings.length === 0) {
    if (comps && comps.usedForPricing > 0) {
      return (
        <section className={embedded ? "" : "mb-8"}>
          {!embedded && <h2 className="mb-2 text-lg font-semibold">Comparable listings</h2>}
          <div className="rounded-xl border border-gray-100 p-6 text-center">
            <p className="text-sm text-gray-500">
              Comparable details unavailable, but{" "}
              <span className="font-medium text-gray-700">
                {comps.usedForPricing}
              </span>{" "}
              listings were still used in the analysis.
            </p>
          </div>
        </section>
      );
    }
    return null;
  }

  // â”€â”€ Populated state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  return (
    <section className={embedded ? "" : "mb-8"}>
      {/* Header */}
      <div className="mb-1 flex items-center justify-between">
        {!embedded && <h2 className="text-lg font-semibold">Comparable listings</h2>}
        {matchQuality && (
          <span
            className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${matchQuality.classes}`}
          >
            {matchQuality.label}
          </span>
        )}
      </div>
      <p className="mb-4 text-sm text-gray-600">
        Based on {used} similar listings in {locationBasis}. Showing the {showingCount} clearest matches first so the recommendation is easier to trust at a glance.
      </p>
      {matchQuality && (
        <p className="mb-3 text-xs text-gray-500">
          {matchQuality.description}
        </p>
      )}
      {pinnedRoomIds.length > 0 && (
        <p className="mb-3 text-xs font-medium text-amber-700">
          Your pinned benchmark listing appears first in this list whenever it is present in the collected comps.
        </p>
      )}

      {/* Hidden-comps banner â€” two states based on whether the report has been re-run */}
      {canManageComps && hiddenCount > 0 && (
        <div
          data-testid="hidden-banner"
          className={`mb-3 flex items-center gap-2 rounded-lg border px-3 py-2 text-xs ${
            allExcludedAlreadyAtRun
              ? "border-blue-100 bg-blue-50 text-blue-800"
              : "border-amber-100 bg-amber-50 text-amber-800"
          }`}
        >
          <span className="flex-1">
            {allExcludedAlreadyAtRun
              ? `Pricing excludes ${hiddenCount} hidden ${hiddenCount === 1 ? "comparable" : "comparables"}.`
              : `${hiddenCount} ${hiddenCount === 1 ? "comparable" : "comparables"} hidden locally. Re-run to update pricing.`}
          </span>
          {!allExcludedAlreadyAtRun && onRerun && (
            <button
              type="button"
              data-testid="rerun-report-button"
              disabled={rerunStarting}
              onClick={async () => {
                if (rerunStarting) return;
                setRerunStarting(true);
                try {
                  await onRerun();
                } finally {
                  setRerunStarting(false);
                }
              }}
              className="rounded-md bg-amber-100 px-2 py-1 text-xs font-medium text-amber-900 ring-1 ring-amber-200 transition hover:bg-amber-200 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {rerunStarting ? "Startingâ€¦" : "Re-run"}
            </button>
          )}
          <button
            type="button"
            data-testid="banner-manage"
            onClick={() => setManageOpen((v) => !v)}
            className="rounded-md px-2 py-1 text-xs font-medium underline-offset-2 hover:underline"
          >
            Manage
          </button>
        </div>
      )}

      {/* Manage popover (lives directly under the banner) */}
      {canManageComps && manageOpen && (
        <div
          data-testid="manage-panel"
          className="mb-3 rounded-lg border border-gray-200 bg-white p-3 text-xs shadow-sm"
        >
          {hiddenCount === 0 ? (
            <p className="text-gray-500">No comps are currently hidden.</p>
          ) : (
            <ul className="divide-y divide-gray-100">
              {excludedDetails.map((ec) => (
                <li
                  key={ec.roomId}
                  data-testid={`manage-row-${ec.roomId}`}
                  className="flex items-center gap-2 py-1.5"
                >
                  <span className="flex-1 truncate text-gray-700">
                    {ec.title || `Room ${ec.roomId}`}
                  </span>
                  {onRestoreExcluded && (
                    <button
                      type="button"
                      data-testid={`manage-restore-${ec.roomId}`}
                      onClick={() => onRestoreExcluded(ec.roomId)}
                      className="rounded bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700 ring-1 ring-emerald-200 transition hover:bg-emerald-100"
                    >
                      Restore
                    </button>
                  )}
                </li>
              ))}
              {/* When excludedDetails is empty but excludedRoomIds has values
                  (caller didn't pass full details), surface IDs alone. */}
              {excludedDetails.length === 0 &&
                excludedRoomIds.map((rid) => (
                  <li key={rid} className="flex items-center gap-2 py-1.5">
                    <span className="flex-1 truncate text-gray-700">Room {rid}</span>
                    {onRestoreExcluded && (
                      <button
                        type="button"
                        data-testid={`manage-restore-${rid}`}
                        onClick={() => onRestoreExcluded(rid)}
                        className="rounded bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700 ring-1 ring-emerald-200 transition hover:bg-emerald-100"
                      >
                        Restore
                      </button>
                    )}
                  </li>
                ))}
            </ul>
          )}
        </div>
      )}

      {/* Inline confirm dialog (conflict cases â€” single feedback channel, no toast error) */}
      {conflictDialog && (
        <div
          role="alertdialog"
          aria-modal="true"
          data-testid="conflict-dialog"
          className="mb-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
        >
          <p className="mb-2 font-medium">
            {conflictDialog.type === "exclude-benchmark"
              ? "This is a benchmark. Remove it and exclude?"
              : "This comp is currently excluded. Promote and restore it?"}
          </p>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              data-testid="conflict-dialog-cancel"
              onClick={() => setConflictDialog(null)}
              className="rounded-md px-3 py-1 text-xs font-medium text-amber-900 hover:bg-amber-100"
            >
              Cancel
            </button>
            <button
              type="button"
              data-testid="conflict-dialog-confirm"
              onClick={() => {
                if (conflictDialog.type === "exclude-benchmark") {
                  onExcludeBenchmarkConflict?.(
                    conflictDialog.listing,
                    conflictDialog.targetRoomId
                  );
                } else {
                  onPromoteComp?.(conflictDialog.listing, {
                    unexcludeRoomId: conflictDialog.targetRoomId,
                  });
                }
                setConflictDialog(null);
              }}
              className="rounded-md bg-amber-600 px-3 py-1 text-xs font-medium text-white hover:bg-amber-700"
            >
              Confirm
            </button>
          </div>
        </div>
      )}
      {/* Sort toggle */}
      <div className="mb-3 flex gap-1 self-start rounded-lg border border-gray-200 p-0.5 w-fit">
        <button
          onClick={() => setSortBy("similarity")}
          className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
            sortBy === "similarity"
              ? "bg-gray-900 text-white"
              : "text-gray-500 hover:text-gray-700"
          }`}
        >
          Similarity
        </button>
        <button
          onClick={() => setSortBy("price")}
          className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
            sortBy === "price"
              ? "bg-gray-900 text-white"
              : "text-gray-500 hover:text-gray-700"
          }`}
        >
          Price
        </button>
      </div>

      {/* Date context */}
      {selectedDate ? (
        !hasAnyComparableDataForSelectedDate ? (
          <p className="mb-2 text-xs font-medium text-amber-700">
            No comparable listing prices were queried exactly on {selectedDate}.
            {" "}Click a different calendar day, or click the selected day to deselect.
          </p>
        ) : (
          <p className="mb-2 text-xs font-medium text-accent">
            Showing scraped nightly prices for {selectedDate}.
            {" "}Click a calendar day to change, or click the selected day to deselect.
          </p>
        )
      ) : null}

      {/* Cards */}
      <div className="space-y-3">
        {visible.map((listing) => {
          const rid = listingRoomId(listing);
          const alreadyBenchmark = !!(rid && pinnedRoomIds.includes(rid));
          const promoteAtCap = pinnedRoomIds.length >= 10;
          const isExcluded = !!(rid && excludedSet.has(rid));
          const isExiting = !!(rid && exitingRoomIds.has(rid));
          return (
            <ComparableCard
              key={listing.id}
              listing={listing}
              isPinned={isPinnedListing(listing, pinnedRoomIds)}
              selectedDate={selectedDate}
              canManage={canManageComps}
              isExiting={isExiting}
              alreadyBenchmark={alreadyBenchmark}
              promoteAtCap={promoteAtCap}
              onExclude={(l) => {
                const r = listingRoomId(l);
                // Conflict: comp is currently a benchmark â†’ inline confirm.
                if (r && pinnedRoomIds.includes(r)) {
                  setConflictDialog({
                    type: "exclude-benchmark",
                    listing: l,
                    targetRoomId: r,
                  });
                  return;
                }
                if (!r) return;
                // Queue *immediately* so the manager owns the pending op.
                // If the user navigates within 200 ms, pagehide / route /
                // listing-switch flush paths now know about this click â€”
                // delaying the queue would silently lose the operation.
                onExcludeComp?.(l);
                // Animation runs in parallel: keep the card mounted with
                // data-state="exiting" for 200 ms.  The render filter
                // respects exitingRoomIds so the optimistic excludedSet
                // (from the parent) doesn't yank the card before the
                // opacity/scale transition has time to play.
                setExitingRoomIds((prev) => {
                  const next = new Set(prev);
                  next.add(r);
                  return next;
                });
                setTimeout(() => {
                  setExitingRoomIds((prev) => {
                    if (!prev.has(r)) return prev;
                    const next = new Set(prev);
                    next.delete(r);
                    return next;
                  });
                }, 200);
              }}
              onPromote={(l) => {
                const r = listingRoomId(l);
                // If comp is currently in excluded â†’ inline confirm before atomic
                // promote-and-unexclude.
                if (r && isExcluded) {
                  setConflictDialog({
                    type: "promote-excluded",
                    listing: l,
                    targetRoomId: r,
                  });
                  return;
                }
                onPromoteComp?.(l);
              }}
            />
          );
        })}
      </div>

      {/* Show more / Show less */}
      {hasMore && (
        <button
          onClick={() => setExpanded(true)}
          className="mt-4 w-full rounded-xl border border-gray-200 py-2.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-50"
        >
          Show {sorted.length - initialVisibleCount} more listings
        </button>
      )}
      {canCollapse && (
        <button
          onClick={() => setExpanded(false)}
          className="mt-4 w-full rounded-xl border border-gray-200 py-2.5 text-sm font-medium text-gray-600 transition-colors hover:bg-gray-50"
        >
          Show less
        </button>
      )}
    </section>
  );
}

