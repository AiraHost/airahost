"use client";

import { useState } from "react";
import type { CalendarDay } from "@/lib/schemas";

interface Props {
  calendar: CalendarDay[];
  /** Current live listing nightly price captured by worker (single value for report start). */
  observedListingPrice?: number | null;
  /** Date (YYYY-MM-DD) that observedListingPrice applies to. */
  observedListingPriceDate?: string | null;
  /**
   * When true, the "Select nights" button appears in the header.
   * Entering that mode lets the user toggle selectedDates via tile clicks.
   * In the default (inspect) mode, tile clicks only affect focusedDate.
   */
  selectable?: boolean;
  onApplyDates?: (selectedDates: string[]) => void;
  /**
   * When provided, tile clicks in inspect mode (the default) fire this with the
   * date string, or null when the same date is clicked again (deselect).
   * Never fires in apply-selection mode.
   */
  onFocusDate?: (date: string | null) => void;
  /** The currently focused date — receives a sky-blue ring in inspect mode. */
  focusedDate?: string | null;
}

const TODAY = new Date().toISOString().split("T")[0];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function PricingHeatmap({
  calendar,
  observedListingPrice = null,
  observedListingPriceDate = null,
  selectable = false,
  onApplyDates,
  onFocusDate,
  focusedDate,
}: Props) {
  const days = calendar;

  // selectedDates: the committed apply selection (all dates by default).
  // Reset is handled externally via the `key` prop — when key changes React
  // remounts this component and re-runs useState initializers.
  const [selectedDates, setSelectedDates] = useState<Set<string>>(
    () => new Set(days.map((d) => d.date))
  );

  // Apply-selection mode. When false (default = inspect mode):
  //   • tile click → onFocusDate → comps panel
  // When true (apply-selection mode):
  //   • tile click → toggleDate → selectedDates
  //   • focusedDate ring is hidden so it doesn't clash with selection visuals
  const [applyMode, setApplyMode] = useState(false);

  // Snapshot of selectedDates taken when apply mode is entered, used by Cancel
  // to restore the previous selection without persisting in-mode changes.
  const [selectionSnapshot, setSelectionSnapshot] = useState<Set<string> | null>(null);

  const [view, setView] = useState<"7" | "30">("30");

  if (days.length === 0) return null;

  const displayPrices = days.map((d) => d.recommendedDailyPrice ?? d.basePrice);
  const visibleDays = view === "7" ? days.slice(0, 7) : days;
  const selectedCount = selectedDates.size;
  const totalCount = days.length;

  const startOffset =
    view === "30" ? new Date(visibleDays[0].date + "T00:00:00").getDay() : 0;

  // ── Apply-mode controls ──────────────────────────────────────────────────

  function enterApplyMode() {
    setSelectionSnapshot(new Set(selectedDates)); // snapshot for Cancel
    setApplyMode(true);
  }

  function doneApplyMode() {
    // Keep selectedDates as-is; just exit apply mode.
    setSelectionSnapshot(null);
    setApplyMode(false);
  }

  function cancelApplyMode() {
    // Restore the selection that existed before the user entered apply mode.
    if (selectionSnapshot !== null) setSelectedDates(selectionSnapshot);
    setSelectionSnapshot(null);
    setApplyMode(false);
  }

  function toggleDate(date: string) {
    setSelectedDates((prev) => {
      const next = new Set(prev);
      if (next.has(date)) next.delete(date);
      else next.add(date);
      return next;
    });
  }

  function selectAll() { setSelectedDates(new Set(days.map((d) => d.date))); }
  function selectNone() { setSelectedDates(new Set()); }

  return (
    <div className="rounded-2xl border border-border bg-white">

      {/* ── Header ── */}
      <div className="flex items-center justify-between gap-3 px-4 py-3 sm:px-6 sm:py-4">
        {applyMode ? (
          /* Apply-mode header */
          <>
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-foreground/80">Select nights</h3>
              <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-semibold text-foreground/45">
                {selectedCount} of {totalCount}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <div className="flex items-center gap-1.5 text-xs text-foreground/30">
                <button type="button" onClick={selectAll} className="transition-colors hover:text-foreground/55">All</button>
                <span>·</span>
                <button type="button" onClick={selectNone} className="transition-colors hover:text-foreground/55">None</button>
              </div>
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={cancelApplyMode}
                  className="rounded-lg border border-gray-200 px-2.5 py-1 text-xs font-medium text-foreground/50 transition-colors hover:border-gray-300 hover:text-foreground/70"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={doneApplyMode}
                  className="rounded-lg bg-gray-900 px-2.5 py-1 text-xs font-semibold text-white transition-colors hover:bg-gray-700"
                >
                  Done
                </button>
              </div>
            </div>
          </>
        ) : (
          /* Inspect-mode header (default) */
          <>
            <h3 className="text-sm font-semibold text-foreground/80">30-Day Pricing Plan</h3>
            <div className="flex items-center gap-3">
              <div className="flex items-center rounded-lg bg-gray-100/70 p-0.5">
                {(["7", "30"] as const).map((v) => (
                  <button
                    key={v}
                    type="button"
                    onClick={() => setView(v)}
                    className={`rounded-md px-2.5 py-1 text-xs font-medium transition-all ${
                      view === v
                        ? "bg-white text-foreground/75 shadow-sm"
                        : "text-foreground/35 hover:text-foreground/55"
                    }`}
                  >
                    {v === "7" ? "7 days" : "30 days"}
                  </button>
                ))}
              </div>
              {selectable && (
                <button
                  type="button"
                  onClick={enterApplyMode}
                  className="flex items-center gap-1.5 rounded-lg border border-gray-200 px-2.5 py-1 text-xs font-medium text-foreground/45 transition-colors hover:border-gray-300 hover:text-foreground/65"
                >
                  Select nights
                  {/* Show current selection count so users know their state before entering apply mode */}
                  <span className="rounded-full bg-gray-100 px-1.5 py-px text-[10px] font-semibold text-foreground/40">
                    {selectedCount}
                  </span>
                </button>
              )}
            </div>
          </>
        )}
      </div>

      {/* ── Apply-mode hint strip ── */}
      {applyMode && (
        <div className="border-t border-gray-100 bg-gray-50/60 px-4 py-2 sm:px-6">
          <p className="text-xs text-foreground/40">
            Tap a date to add or remove it from your apply selection. Press <strong className="font-semibold text-foreground/55">Done</strong> when finished.
          </p>
        </div>
      )}

      {/* ── Weekday headers ── */}
      <div className="grid grid-cols-7 border-t border-gray-100 px-2 sm:px-4">
        {WEEKDAYS.map((wd) => (
          <div key={wd} className="py-2 text-center text-xs font-normal text-foreground/40">
            {wd}
          </div>
        ))}
      </div>

      {/* ── Calendar grid ── */}
      <div className="grid grid-cols-7 gap-1 px-2 pb-2 sm:gap-1.5 sm:px-4 sm:pb-4">
        {Array.from({ length: startOffset }).map((_, i) => (
          <div key={`empty-${i}`} />
        ))}

        {visibleDays.map((day, idx) => {
          const globalIdx = days.indexOf(day);
          const price = displayPrices[globalIdx >= 0 ? globalIdx : idx];
          const d = new Date(day.date + "T00:00:00");
          const dateNum = d.getDate();
          const isToday = day.date === TODAY;
          const isPast = day.date < TODAY;
          const isSelected = selectedDates.has(day.date);
          // Only show focus ring in inspect mode — apply mode has its own selection ring.
          const isFocused = !applyMode && !isPast && focusedDate === day.date;

          // Tile style priority: apply-selected (black) > focused ring > default compare tint
          const suggested = day.recommendedDailyPrice ?? day.baseDailyPrice ?? day.basePrice;
          const hasLivePrice =
            typeof observedListingPrice === "number" && observedListingPrice > 0;
          const dayFlags = Array.isArray(day.flags)
            ? day.flags.map((f) => String(f).trim().toLowerCase())
            : [];
          const isTargetOnlyFallbackDay = dayFlags.includes("target_listing_only_fallback");
          const dayUserPrice =
            typeof day.userListingPrice === "number" && day.userListingPrice > 0
              ? day.userListingPrice
              : null;
          // User listing price for THIS day only.
          // Normal mode: only the captured live day is available.
          // Target-only fallback mode: baseDailyPrice is the user's day-level price.
          const originalUserPrice =
            dayUserPrice != null
              ? dayUserPrice
              : (isTargetOnlyFallbackDay &&
                  typeof day.baseDailyPrice === "number" &&
                  day.baseDailyPrice > 0
                  ? day.baseDailyPrice
              : (
                  hasLivePrice && observedListingPriceDate === day.date
                    ? observedListingPrice
                    : null
                ));
          const hasComparablePrices =
            typeof originalUserPrice === "number" &&
            originalUserPrice > 0 &&
            typeof suggested === "number" &&
            suggested > 0;
          // If this day is marked missing_data, we should not color it.
          // This covers both target-only fallback days and regular scrape days
          // where a reliable day-level price signal was not available.
          const isMissingPriceDay =
            dayFlags.includes("missing_data") ||
            !(typeof originalUserPrice === "number" && originalUserPrice > 0);

          let compareToneCls = "border-gray-200/80 bg-white";
          if (!isMissingPriceDay && hasComparablePrices) {
            const diffPct = ((originalUserPrice / suggested) - 1) * 100;
            const absDiffPct = Math.abs(diffPct);
            if (absDiffPct > 8) compareToneCls = "border-rose-200 bg-rose-50/60";
            else if (absDiffPct > 3) compareToneCls = "border-amber-200 bg-amber-50/60";
            else compareToneCls = "border-emerald-200 bg-emerald-50/60";
          }

          const tileCls = applyMode && isSelected && !isPast
            ? "border-gray-900 bg-gray-900"
            : `${compareToneCls} ${isFocused ? "ring-1 ring-sky-300/60 border-sky-400/60" : ""}`;

          const isApplySelected = applyMode && isSelected && !isPast;

          const dateNumEl = isToday ? (
            <span className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-semibold sm:h-6 sm:w-6 sm:text-xs ${isApplySelected ? "bg-white/20 text-white" : "bg-rose-500 text-white"}`}>
              {dateNum}
            </span>
          ) : (
            <span className={`text-[11px] font-medium sm:text-sm ${isPast ? "text-foreground/25" : isApplySelected ? "text-white" : "text-foreground/80"}`}>
              {dateNum}
            </span>
          );

          const priceEl = !isPast ? (
            <div className={`${isApplySelected ? "text-white/80" : "text-foreground/70"}`}>
              <p className="mt-1 text-[11px] font-medium sm:mt-3 sm:text-sm">
                ${price}
              </p>
              <p className={`text-[10px] sm:text-[11px] ${isApplySelected ? "text-white/70" : "text-foreground/50"}`}>
                Your {typeof originalUserPrice === "number" && originalUserPrice > 0 ? `$${originalUserPrice}` : "—"}
              </p>
            </div>
          ) : null;

          // Which action does a click perform?
          // apply mode + future → toggle selectedDates
          // inspect mode + future + onFocusDate → toggle focusedDate
          // past / no handler → no click
          const clickable = !isPast && (applyMode || !!onFocusDate);

          const tileBody = (
            <>
              {dateNumEl}
              {priceEl}
            </>
          );

          if (clickable) {
            // Hover style depends on current tile state to avoid visual jarring:
            // – black apply-selected tiles stay dark on hover (bg-gray-800)
            // – other tiles get the standard lighter border on hover
            const hoverCls = isApplySelected
              ? "hover:bg-gray-800 hover:border-gray-800"
              : "hover:border-gray-400/50";
            return (
              <button
                key={day.date}
                type="button"
                onClick={() => {
                  if (applyMode) {
                    toggleDate(day.date);
                  } else {
                    // Inspect mode: only focusedDate changes. selectedDates is never touched.
                    // The Comparable Listings panel will stay visible as long as focusedDate
                    // is non-null — entering apply mode afterwards does not clear it.
                    onFocusDate?.(day.date === focusedDate ? null : day.date);
                  }
                }}
                className={`w-full rounded-xl border p-1.5 text-left transition-colors sm:rounded-2xl sm:p-2.5 ${tileCls} cursor-pointer ${hoverCls}`}
              >
                {tileBody}
              </button>
            );
          }

          return (
            <div
              key={day.date}
              className={`rounded-xl border p-1.5 sm:rounded-2xl sm:p-2.5 ${tileCls}`}
            >
              {tileBody}
            </div>
          );
        })}
      </div>

      {/* ── Apply footer (always visible when selectable, both modes) ── */}
      {selectable && (
        <div className="border-t border-gray-100 px-3 py-3 sm:px-6 sm:py-4">
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs text-foreground/35">
              {selectedCount === totalCount
                ? `All ${totalCount} nights`
                : `${selectedCount} of ${totalCount} nights`}
              {" selected"}
            </p>
            <button
              type="button"
              disabled={selectedCount === 0}
              onClick={() => onApplyDates?.(Array.from(selectedDates))}
              className="rounded-xl bg-gray-900 px-4 py-2 text-xs font-semibold text-white transition-colors hover:bg-gray-700 disabled:opacity-40"
            >
              Apply {selectedCount} nights →
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
