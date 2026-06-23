"""
Observation write helper — Phase 5A.

Writes normalized per-date pricing observations from a completed nightly
report into the three observation tables:

  target_price_observations    — market median / effective prices per stay_date
  benchmark_price_observations — pinned benchmark price vs market per stay_date
  market_comp_observations     — per-comp nightly price per stay_date

Calling convention
------------------
This module is imported lazily inside the nightly hook in worker/main.py so
that import failures (e.g. if this file is missing) are caught and logged
rather than crashing the worker at startup.

All public functions are designed to be called inside a try/except block.
If a write fails, the caller logs the exception and continues — observation
failures must NEVER affect the main nightly report or its pricing_reports row.

Data sources (from a completed nightly report)
-----------------------------------------------
  result_calendar              → target_price_observations
  comparableListings (pinned)  → benchmark_price_observations
  comparableListings (all)     → market_comp_observations

Idempotency
-----------
target_price_observations has a UNIQUE constraint on
(saved_listing_id, pricing_report_id, stay_date); upsert with on_conflict
makes re-runs safe.  The other two tables use plain INSERT; re-running the
same report ID produces duplicate rows in those tables, which is acceptable
for a historical audit trail.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("worker.core.observations")

_ROOM_ID_RE = re.compile(r"/rooms/(\d+)")

# Maximum rows per Supabase insert call.  Large comp × date products can easily
# exceed a few thousand rows; chunking keeps individual request payloads small.
_INSERT_CHUNK_SIZE = 500


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def write_nightly_observations(
    client,
    *,
    saved_listing_id: str,
    pricing_report_id: str,
    captured_at: datetime.datetime,
    summary: Dict[str, Any],
    calendar: List[Dict[str, Any]],
) -> None:
    """
    Normalize and write all three observation tables from a completed nightly report.

    Args:
        client:             Supabase client (service role — bypasses RLS).
        saved_listing_id:   saved_listings.id for this report's target listing.
        pricing_report_id:  pricing_reports.id that was just marked ready.
        captured_at:        When the market data was scraped.  Use
                            datetime.utcnow() at the call site when
                            market_captured_at is not separately available.
        summary:            result_summary dict from the completed report.
        calendar:           result_calendar list from the completed report.

    Raises:
        Any exception from Supabase client calls.  Callers must wrap this
        function in try/except so failures remain non-fatal.
    """
    captured_at_iso = (
        captured_at.isoformat()
        if isinstance(captured_at, datetime.datetime)
        else str(captured_at)
    )

    n_target    = _write_target_observations(
        client, saved_listing_id, pricing_report_id, captured_at_iso, calendar,
    )
    n_benchmark = _write_benchmark_observations(
        client, saved_listing_id, pricing_report_id, captured_at_iso, summary, calendar,
    )
    n_comp      = _write_comp_observations(
        client, saved_listing_id, pricing_report_id, captured_at_iso, summary,
    )

    logger.info(
        "[observations] report=%s listing=%s  target=%d  benchmark=%d  comp=%d",
        pricing_report_id, saved_listing_id, n_target, n_benchmark, n_comp,
    )


# ---------------------------------------------------------------------------
# Per-table writers
# ---------------------------------------------------------------------------

def _write_target_observations(
    client,
    saved_listing_id: str,
    pricing_report_id: str,
    captured_at_iso: str,
    calendar: List[Dict[str, Any]],
) -> int:
    """
    One row per stay_date from result_calendar.

    Uses upsert so that retrying the same report ID is safe — the UNIQUE
    constraint on (saved_listing_id, pricing_report_id, stay_date) ensures
    duplicate rows are not created.
    """
    rows = []
    for day in calendar:
        date_str = day.get("date")
        if not date_str:
            continue
        rows.append({
            "saved_listing_id":             saved_listing_id,
            "pricing_report_id":            pricing_report_id,
            "captured_at":                  captured_at_iso,
            "stay_date":                    date_str,
            # baseDailyPrice is the newer field; basePrice is kept for compat.
            "market_median_price":          _safe_num(
                day.get("baseDailyPrice") or day.get("basePrice")
            ),
            "market_price_adjusted":        _safe_num(day.get("priceAfterTimeAdjustment")),
            "effective_price_refundable":   _safe_num(day.get("effectiveDailyPriceRefundable")),
            "effective_price_nonrefundable":_safe_num(day.get("effectiveDailyPriceNonRefundable")),
            "is_weekend":                   bool(day.get("isWeekend")),
            "day_flags":                    day.get("flags") or [],
            "source_type":                  "nightly_board_refresh",
        })

    if not rows:
        return 0

    client.table("target_price_observations").upsert(
        rows,
        on_conflict="saved_listing_id,pricing_report_id,stay_date",
    ).execute()
    return len(rows)


def _write_benchmark_observations(
    client,
    saved_listing_id: str,
    pricing_report_id: str,
    captured_at_iso: str,
    summary: Dict[str, Any],
    calendar: List[Dict[str, Any]],
) -> int:
    """
    One row per stay_date from the pinned benchmark comp's priceByDate.

    The pinned benchmark comp is identified by isPinnedBenchmark=True in
    comparableListings.  Rows are only written when such a comp is present
    in the report; this write path does not check Mode C explicitly.

    market_median_price is correlated from result_calendar so each
    benchmark observation can be compared to the market on the same day.
    """
    comparables = summary.get("comparableListings") or []
    pinned = next(
        (c for c in comparables if c.get("isPinnedBenchmark")),
        None,
    )
    if not pinned:
        return 0

    price_by_date: Dict[str, Any] = pinned.get("priceByDate") or {}
    if not price_by_date:
        return 0

    # Build a date → market_median lookup from result_calendar for correlation
    market_by_date: Dict[str, Optional[float]] = {
        day["date"]: _safe_num(day.get("baseDailyPrice") or day.get("basePrice"))
        for day in calendar
        if day.get("date")
    }

    benchmark_url = pinned.get("url")
    rows = []
    for date_str, price in price_by_date.items():
        if not date_str or price is None:
            continue
        rows.append({
            "saved_listing_id":       saved_listing_id,
            "pricing_report_id":      pricing_report_id,
            "captured_at":            captured_at_iso,
            "stay_date":              date_str,
            "benchmark_nightly_price":_safe_num(price),
            "market_median_price":    market_by_date.get(date_str),
            "benchmark_listing_url":  benchmark_url,
            # Per-date confidence is not yet available from the benchmark
            # pipeline; the field is reserved for a future phase.
            "confidence":             None,
            "source_type":            "nightly_board_refresh",
        })

    if not rows:
        return 0

    client.table("benchmark_price_observations").insert(rows).execute()
    return len(rows)


def _write_comp_observations(
    client,
    saved_listing_id: str,
    pricing_report_id: str,
    captured_at_iso: str,
    summary: Dict[str, Any],
) -> int:
    """
    One row per (comp_listing × stay_date) from comparableListings[].priceByDate.

    All comps are written, including the pinned benchmark comp (which will also
    appear in benchmark_price_observations with market correlation data).
    is_pinned_benchmark=True on such rows makes them easy to filter later.

    Large comp × date products are chunked to avoid Supabase payload limits.
    """
    comparables = summary.get("comparableListings") or []
    rows = []

    for comp in comparables:
        comp_url     = comp.get("url")
        comp_id      = _extract_room_id(comp_url)
        similarity   = _safe_num(comp.get("similarity"))
        is_pinned    = bool(comp.get("isPinnedBenchmark", False))
        price_by_date: Dict[str, Any] = comp.get("priceByDate") or {}
        price_details: Dict[str, Any] = comp.get("priceByDateDetails") or {}

        for date_str, price in price_by_date.items():
            if not date_str or price is None:
                continue
            detail = price_details.get(date_str) if isinstance(price_details, dict) else None
            if isinstance(detail, dict) and detail.get("source") == "fixed_pool_reuse":
                continue
            rows.append({
                "saved_listing_id": saved_listing_id,
                "pricing_report_id":pricing_report_id,
                "captured_at":      captured_at_iso,
                "stay_date":        date_str,
                "comp_airbnb_id":   comp_id,
                "comp_listing_url": comp_url,
                "nightly_price":    _safe_num(price),
                "similarity_score": similarity,
                "is_pinned_benchmark": is_pinned,
                "source_type":      "nightly_board_refresh",
            })

    if not rows:
        return 0

    _batch_insert(client, "market_comp_observations", rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_room_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = _ROOM_ID_RE.search(url)
    return m.group(1) if m else None


def _safe_num(v: Any) -> Optional[float]:
    """Return a rounded float or None for missing / non-numeric values."""
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _batch_insert(
    client,
    table: str,
    rows: List[Dict[str, Any]],
    chunk_size: int = _INSERT_CHUNK_SIZE,
) -> None:
    for i in range(0, len(rows), chunk_size):
        client.table(table).insert(rows[i : i + chunk_size]).execute()
