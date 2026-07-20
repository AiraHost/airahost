"""
Phase 6A: Observation-first reuse for interactive/custom reports.

For interactive reports (execution_policy=interactive_live_report) where:
  - a saved_listing_id is available (listing shorthand / rerun flows)
  - input_mode is criteria-based (not URL mode)

This module checks whether recent-enough target_price_observations cover the
requested date range.  If they do, the report is assembled from stored data
instead of scraping Airbnb — avoiding unnecessary Airbnb requests.

Freshness Policy
----------------
Stay dates are bucketed into three tiers, each with its own max observation age:

  Tier          Date offset       Max observation age
  ─────────────────────────────────────────────────────────────────────────
  near_term     D0 – D(T1-1)      OBS_FRESH_NEAR_TERM_HOURS  (default: 12h)
  medium        D(T1) – D(T2-1)   OBS_FRESH_MEDIUM_HOURS     (default: 36h)
  far           D(T2)+            OBS_FRESH_FAR_HOURS         (default: 72h)

  T1 = OBS_FRESH_TIER1_END  (default: 4,  sourced from env)
  T2 = OBS_FRESH_TIER2_END  (default: 11, sourced from env)

Eligibility Gate (Phase 6A: all-or-nothing)
--------------------------------------------
All requested stay_dates must have a fresh observation.  If any date is
missing or stale, assess_observation_coverage() returns eligible=False and
the worker falls back to a full live scrape.

Partial-date reuse (some dates from observations + some from live scrape)
requires hooking into the per-date scrape loop and is deferred to Phase 6B.

Scope Restrictions
------------------
Observation reuse is applied only when ALL of the following are true:
  1. The job is NOT nightly (is_nightly=False)
  2. The job has a saved listing_id (requires linked saved listing)
  3. input_mode is "criteria", "criteria-by-city", or "criteria-by-zip"
     — URL mode always scrapes at least once to extract real listing specs

Failure Semantics
-----------------
All functions are designed to be called inside try/except.  Any exception
is non-fatal: the caller falls back to the live scrape path unchanged.
"""

from __future__ import annotations

import datetime
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("worker.core.observation_reuse")

# ---------------------------------------------------------------------------
# Freshness thresholds (configurable via env vars)
# ---------------------------------------------------------------------------

# D0–D(TIER1_END-1): near-term dates change most with booking demand.
# Default 12h: nightly refreshes every ~24h; 12h gives ~50% recency margin.
OBS_FRESH_NEAR_TERM_HOURS: int = int(os.getenv("OBS_FRESH_NEAR_TERM_HOURS", "12"))

# D(TIER1_END)–D(TIER2_END-1): medium-term dates change more slowly.
# Default 36h: still acceptable after a missed nightly run.
OBS_FRESH_MEDIUM_HOURS: int = int(os.getenv("OBS_FRESH_MEDIUM_HOURS", "36"))

# D(TIER2_END)+: far-out dates are least volatile in short windows.
# Default 72h: 3 nightly cycles; reasonable for dates >10 days out.
OBS_FRESH_FAR_HOURS: int = int(os.getenv("OBS_FRESH_FAR_HOURS", "72"))

# ---------------------------------------------------------------------------
# Tier boundaries for the freshness policy above.
# ---------------------------------------------------------------------------

_TIER1_END: int = int(os.getenv("OBS_FRESH_TIER1_END", "4"))   # D0 – D(T1-1)
_TIER2_END: int = int(os.getenv("OBS_FRESH_TIER2_END", "11"))  # D(T1) – D(T2-1)

# Input modes eligible for observation reuse.
# URL mode is excluded: it must scrape at least once to extract real listing
# specs (bedrooms, baths, guests) from the Airbnb page.
REUSE_ELIGIBLE_MODES = frozenset({"criteria", "criteria-by-city", "criteria-by-zip"})


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ObservationAssessment:
    """
    Result of assess_observation_coverage().

    eligible:           True when all requested stay_dates have fresh observations.
    reason:             Human-readable explanation of the eligibility decision.

    dates_requested:    All stay_dates in the window (YYYY-MM-DD), in order.
    dates_reusable:     Dates with observations fresh enough to serve.
    dates_missing:      Dates with no observation in the DB at all.
    dates_stale:        Dates where the most recent observation is too old.

    assembled_rows:     Pre-built daily_results dicts, ready for
                        _build_scrape_calendar() (populated when eligible).
                        Each entry: {date, median_price, is_weekend, flags,
                        _obs_captured_at, _obs_tier, _obs_age_hours}.

    freshness_hours:    Per-tier threshold used: {tier_name -> max_hours}.
    obs_age_hours:      {date_str -> age_hours} for all found observations.
    """
    eligible: bool
    reason: str

    dates_requested: List[str] = field(default_factory=list)
    dates_reusable: List[str] = field(default_factory=list)
    dates_missing: List[str] = field(default_factory=list)
    dates_stale: List[str] = field(default_factory=list)

    assembled_rows: List[Dict[str, Any]] = field(default_factory=list)

    freshness_hours: Dict[str, int] = field(default_factory=dict)
    obs_age_hours: Dict[str, float] = field(default_factory=dict)

    def to_debug_dict(self) -> Dict[str, Any]:
        """Serialisable summary for embedding in result_core_debug."""
        return {
            "obs_reuse_eligible":        self.eligible,
            "obs_reuse_reason":          self.reason,
            "obs_reuse_dates_total":     len(self.dates_requested),
            "obs_reuse_dates_reusable":  len(self.dates_reusable),
            "obs_reuse_dates_missing":   len(self.dates_missing),
            "obs_reuse_dates_stale":     len(self.dates_stale),
            "obs_reuse_freshness_hours": self.freshness_hours,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assess_observation_coverage(
    client,
    *,
    saved_listing_id: str,
    start_date: str,
    end_date: str,
) -> ObservationAssessment:
    """
    Check whether target_price_observations can serve an interactive report.

    Queries the most recent target_price_observation for each stay_date in
    [start_date, end_date) and applies per-tier freshness thresholds.

    Phase 6A gate: all dates must have a fresh observation for the assessment
    to return eligible=True.  Any missing or stale date makes the whole window
    ineligible — falling back to a full live scrape.

    Args:
        client:           Supabase client (service role — bypasses RLS).
        saved_listing_id: UUID of the saved listing.
        start_date:       Report window start date (inclusive, YYYY-MM-DD).
        end_date:         Report window end date (exclusive, YYYY-MM-DD).
                          Matches worker convention: range(total_days) from start.

    Returns:
        ObservationAssessment describing eligibility and, when eligible, the
        assembled daily_results rows ready for _build_scrape_calendar().
    """
    now = datetime.datetime.utcnow()
    freshness_hours: Dict[str, int] = {
        "near_term": OBS_FRESH_NEAR_TERM_HOURS,
        "medium":    OBS_FRESH_MEDIUM_HOURS,
        "far":       OBS_FRESH_FAR_HOURS,
    }

    # ── Build the ordered list of stay_dates in the window ────────────────
    try:
        d_start = datetime.date.fromisoformat(start_date)
        d_end   = datetime.date.fromisoformat(end_date)
    except (ValueError, AttributeError) as exc:
        return ObservationAssessment(
            eligible=False,
            reason=f"invalid date range ({start_date!r} → {end_date!r}): {exc}",
            freshness_hours=freshness_hours,
        )

    n_days = (d_end - d_start).days
    if n_days <= 0:
        return ObservationAssessment(
            eligible=False,
            reason="empty date range",
            freshness_hours=freshness_hours,
        )

    dates_requested = [
        (d_start + datetime.timedelta(days=i)).isoformat()
        for i in range(n_days)
    ]

    # ── Query target_price_observations for the full window ───────────────
    try:
        res = (
            client.table("target_price_observations")
            .select("stay_date,market_median_price,is_weekend,day_flags,captured_at")
            .eq("saved_listing_id", saved_listing_id)
            .gte("stay_date", dates_requested[0])
            .lte("stay_date", dates_requested[-1])
            .order("captured_at", desc=True)
            .limit(1000)
            .execute()
        )
        obs_rows = res.data or []
    except Exception as exc:
        logger.warning("[obs_reuse] DB query failed for listing=%s: %s", saved_listing_id, exc)
        return ObservationAssessment(
            eligible=False,
            reason=f"observation query failed: {exc}",
            freshness_hours=freshness_hours,
            dates_requested=dates_requested,
        )

    # Keep only the most recent observation per stay_date.
    # Rows are already ordered desc by captured_at, so the first occurrence wins.
    best_obs: Dict[str, Dict[str, Any]] = {}
    for row in obs_rows:
        date_str = row.get("stay_date")
        if date_str and date_str not in best_obs:
            best_obs[date_str] = row

    # ── Assess freshness for each requested date ──────────────────────────
    dates_reusable: List[str] = []
    dates_missing:  List[str] = []
    dates_stale:    List[str] = []
    assembled_rows: List[Dict[str, Any]] = []
    obs_age_hours:  Dict[str, float] = {}

    for i, date_str in enumerate(dates_requested):
        # Tier determines the freshness threshold for this date.
        if i < _TIER1_END:
            max_age_h = OBS_FRESH_NEAR_TERM_HOURS
            tier = "near_term"
        elif i < _TIER2_END:
            max_age_h = OBS_FRESH_MEDIUM_HOURS
            tier = "medium"
        else:
            max_age_h = OBS_FRESH_FAR_HOURS
            tier = "far"

        obs = best_obs.get(date_str)
        if obs is None:
            dates_missing.append(date_str)
            continue

        # Parse captured_at (ISO 8601, may include timezone offset).
        try:
            raw_ts = (obs.get("captured_at") or "").replace("Z", "+00:00")
            captured = datetime.datetime.fromisoformat(raw_ts)
            # Convert to naive UTC for comparison with utcnow().
            if captured.tzinfo is not None:
                captured = captured.utctimetuple()
                captured = datetime.datetime(*captured[:6])
            age_hours = (now - captured).total_seconds() / 3600.0
        except (ValueError, AttributeError, TypeError):
            # Unparseable timestamp — treat as stale to be safe.
            dates_stale.append(date_str)
            continue

        obs_age_hours[date_str] = round(age_hours, 2)

        if age_hours > max_age_h:
            dates_stale.append(date_str)
            continue

        # Fresh enough: record as reusable.
        dates_reusable.append(date_str)
        price = obs.get("market_median_price")
        assembled_rows.append({
            # Required by _build_scrape_calendar
            "date":         date_str,
            "median_price": float(price) if price is not None else None,
            "is_weekend":   bool(obs.get("is_weekend")),
            "flags":        list(obs.get("day_flags") or []),
            # Provenance fields — passed through the calendar builder but not
            # used for pricing; visible in raw debug output.
            "_obs_captured_at": obs.get("captured_at"),
            "_obs_tier":        tier,
            "_obs_age_hours":   round(age_hours, 2),
        })

    # ── Phase 6A eligibility gate: all dates must be fresh ────────────────
    n_total = len(dates_requested)

    if dates_missing:
        reason = (
            f"{len(dates_missing)}/{n_total} date(s) have no observation "
            f"(first missing: {dates_missing[0]})"
        )
        return ObservationAssessment(
            eligible=False,
            reason=reason,
            dates_requested=dates_requested,
            dates_reusable=dates_reusable,
            dates_missing=dates_missing,
            dates_stale=dates_stale,
            freshness_hours=freshness_hours,
            obs_age_hours=obs_age_hours,
        )

    if dates_stale:
        reason = (
            f"{len(dates_stale)}/{n_total} date(s) have stale observations "
            f"(first stale: {dates_stale[0]})"
        )
        return ObservationAssessment(
            eligible=False,
            reason=reason,
            dates_requested=dates_requested,
            dates_reusable=dates_reusable,
            dates_missing=dates_missing,
            dates_stale=dates_stale,
            freshness_hours=freshness_hours,
            obs_age_hours=obs_age_hours,
        )

    # Guard against null prices on otherwise-fresh dates.
    null_price_dates = [r["date"] for r in assembled_rows if r["median_price"] is None]
    if null_price_dates:
        reason = (
            f"{len(null_price_dates)}/{n_total} observation(s) have null market_median_price "
            f"(first: {null_price_dates[0]})"
        )
        return ObservationAssessment(
            eligible=False,
            reason=reason,
            dates_requested=dates_requested,
            dates_reusable=dates_reusable,
            dates_missing=dates_missing,
            dates_stale=dates_stale,
            freshness_hours=freshness_hours,
            obs_age_hours=obs_age_hours,
        )

    logger.info(
        "[obs_reuse] listing=%s: all %d date(s) fresh "
        "(near_term≤%dh  medium≤%dh  far≤%dh)",
        saved_listing_id, n_total,
        OBS_FRESH_NEAR_TERM_HOURS, OBS_FRESH_MEDIUM_HOURS, OBS_FRESH_FAR_HOURS,
    )
    return ObservationAssessment(
        eligible=True,
        reason=f"all {n_total} date(s) have fresh observations",
        dates_requested=dates_requested,
        dates_reusable=dates_reusable,
        dates_missing=[],
        dates_stale=[],
        assembled_rows=assembled_rows,
        freshness_hours=freshness_hours,
        obs_age_hours=obs_age_hours,
    )
