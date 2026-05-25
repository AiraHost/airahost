"""
AriaHost Worker — Long-running process that polls Supabase for queued
pricing reports and processes them.

Usage:
    python -m worker.main

Environment variables: see .env.example
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import signal
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv

# Load .env from worker directory or repo root.
# override=False so shell-set env vars (e.g. WORKER_LANE, WORKER_ENV) always win.
# This lets multiple local workers run from separate terminals with different config.
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)
load_dotenv(override=False)

from worker.core import db as db_helpers
from worker.core.cache import compute_cache_key, get_cached, set_cached
from worker.core.concurrent_runner import MAX_SCRAPER_WORKERS
from worker.core.discounts import (
    apply_discount,
    average_refundable_price_for_stay,
    build_stay_length_averages,
)
from worker.core.dynamic_pricing import compute_dynamic_pricing_adjustment
from worker.core.report_policy import (
    resolve_execution_policy,
    NIGHTLY_POLICIES,
)
from worker.core.auto_price_assignment import assign_prices_calendar
# mock_core removed — scrape failures now mark jobs as error

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "5"))
STALE_MINUTES = int(os.getenv("WORKER_STALE_MINUTES", "15"))
MAX_ATTEMPTS = int(os.getenv("WORKER_MAX_ATTEMPTS", "3"))
HEARTBEAT_SECONDS = int(os.getenv("WORKER_HEARTBEAT_SECONDS", "10"))
MAX_RUNTIME_SECONDS = int(os.getenv("WORKER_MAX_RUNTIME_SECONDS", "180"))
CDP_CONNECT_TIMEOUT_MS = int(os.getenv("CDP_CONNECT_TIMEOUT_MS", "15000"))
WORKER_VERSION = os.getenv("WORKER_VERSION", "worker-0.1.0")
CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222")
WORKER_ENV = os.getenv("WORKER_ENV", "production")
WORKER_LANE = os.getenv("WORKER_LANE", "interactive")

MAX_SCROLL_ROUNDS = int(os.getenv("MAX_SCROLL_ROUNDS", "12"))
MAX_CARDS = int(os.getenv("MAX_CARDS", "80"))
RATE_LIMIT_SECONDS = float(os.getenv("SCRAPE_RATE_LIMIT_SECONDS", "1.0"))
# Memory-safe defaults: keep fan-out conservative unless explicitly overridden.
DAY_QUERY_MAX_WORKERS = max(1, min(int(os.getenv("DAY_QUERY_MAX_WORKERS", "1")), MAX_SCRAPER_WORKERS))
BENCHMARK_DAY_QUERY_MAX_WORKERS = max(
    1, min(int(os.getenv("BENCHMARK_DAY_QUERY_MAX_WORKERS", "1")), MAX_SCRAPER_WORKERS)
)
FIXED_POOL_MAX_WORKERS = max(1, min(int(os.getenv("FIXED_POOL_MAX_WORKERS", "1")), MAX_SCRAPER_WORKERS))
AIRBNB_DISABLE_MAP_SEARCH = bool(
    str(os.getenv("AIRBNB_DISABLE_MAP_SEARCH", "0")).strip().lower() in ("1", "true", "yes", "on")
)
AIRBNB_ENABLE_AI_SEARCH = bool(
    str(os.getenv("AIRBNB_ENABLE_AI_SEARCH", "0")).strip().lower() in ("1", "true", "yes", "on")
)

# Auto-apply queue settings (single-process mode via worker.main)
AUTO_APPLY_STALE_MINUTES = int(os.getenv("AUTO_APPLY_STALE_MINUTES", "15"))
AUTO_APPLY_MAX_ATTEMPTS = int(os.getenv("AUTO_APPLY_MAX_ATTEMPTS", "3"))
AUTO_APPLY_CDP_URL = os.getenv("AUTO_APPLY_CDP_URL", CDP_URL)

NIGHTLY_QUEUE_ML_FORECAST = str(
    os.getenv("NIGHTLY_QUEUE_ML_FORECAST", "1")
).strip().lower() in ("1", "true", "yes", "on")
NIGHTLY_ML_TRAINING_SCOPE = os.getenv("NIGHTLY_ML_TRAINING_SCOPE", "global")
NIGHTLY_ML_FORCE_RETRAIN = str(
    os.getenv("NIGHTLY_ML_FORCE_RETRAIN", "1")
).strip().lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Logging — console + rotating file (logs/worker.log)
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Ensure logs/ directory exists next to this file (i.e. worker/logs/)
_log_dir = Path(__file__).resolve().parent / "logs"
_log_dir.mkdir(exist_ok=True)

# Root logger setup — captures all worker.* loggers
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)

# Console handler
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
_root_logger.addHandler(_console)

# Rotating file handler: 5 MB per file, keep 5 backups
_file_handler = logging.handlers.RotatingFileHandler(
    filename=str(_log_dir / "worker.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
_root_logger.addHandler(_file_handler)

logger = logging.getLogger("worker")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_shutdown_event = threading.Event()


def _signal_handler(sig, frame):
    logger.info(f"Received signal {sig}, shutting down gracefully...")
    _shutdown_event.set()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# ---------------------------------------------------------------------------
# Heartbeat thread
# ---------------------------------------------------------------------------


def _run_heartbeat(
    report_id: str,
    worker_token: uuid.UUID,
    stop_event: threading.Event,
):
    """Background thread that sends heartbeats while a job is running."""
    client = db_helpers.get_client()
    while not stop_event.is_set():
        stop_event.wait(HEARTBEAT_SECONDS)
        if stop_event.is_set():
            break
        try:
            ok = db_helpers.heartbeat(client, report_id, worker_token)
            if not ok:
                logger.warning(f"Heartbeat rejected for {report_id} — claim lost?")
                break
        except Exception as exc:
            logger.error(f"Heartbeat error for {report_id}: {exc}")


def _exception_chain_messages(exc: BaseException) -> str:
    """Flatten exception + cause/context chain into one lowercase string."""
    parts: List[str] = []
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        try:
            parts.append(str(cur))
        except Exception:
            pass
        nxt = cur.__cause__ or cur.__context__
        cur = nxt if isinstance(nxt, BaseException) else None
    return " | ".join(parts).lower()


def _is_windows_paging_file_error(exc: BaseException) -> bool:
    """
    Detect Windows virtual-memory exhaustion symptoms (WinError 1455).
    Typical markers:
      - WinError 1455
      - 'paging file is too small'
      - OSError errno 22 with Win32 payload 1455 in chained messages
    """
    msg = _exception_chain_messages(exc)
    return (
        "winerror 1455" in msg
        or "paging file is too small" in msg
        or ("oserror(22" in msg and "1455" in msg)
    )


# ---------------------------------------------------------------------------
# Job processing
# ---------------------------------------------------------------------------


def _get_listing_url(job: Dict[str, Any]) -> Optional[str]:
    """Extract listing URL from the job row (multiple possible locations)."""
    url = job.get("input_listing_url")
    if url:
        return url.strip()
    attrs = job.get("input_attributes") or {}
    url = attrs.get("listingUrl") or attrs.get("listing_url")
    if url:
        return str(url).strip()
    return None


def _merge_extracted_specs_into_attributes(
    current_attributes: Dict[str, Any],
    transparent_result: Dict[str, Any],
) -> Dict[str, Any]:
    """
    For URL mode, overwrite placeholder form values with extracted Airbnb specs
    so downstream UI shows real listing details.

    Reads from transparent_result["targetSpec"] (new format).
    """
    merged = dict(current_attributes or {})
    specs = (transparent_result or {}).get("targetSpec") or {}
    if not isinstance(specs, dict):
        return merged

    if specs.get("propertyType"):
        merged["propertyType"] = specs["propertyType"]
    if isinstance(specs.get("accommodates"), int) and specs["accommodates"] > 0:
        merged["maxGuests"] = specs["accommodates"]
    if isinstance(specs.get("bedrooms"), int) and specs["bedrooms"] >= 0:
        merged["bedrooms"] = specs["bedrooms"]
    if isinstance(specs.get("baths"), (int, float)) and specs["baths"] > 0:
        merged["bathrooms"] = float(specs["baths"])
    if isinstance(specs.get("beds"), int) and specs["beds"] > 0:
        merged["beds"] = specs["beds"]
    if isinstance(specs.get("amenities"), list):
        merged_amenities: list[str] = []
        seen_amenities: set[str] = set()
        existing_amenities = merged.get("amenities")
        amenity_sources = [specs["amenities"]]
        if isinstance(existing_amenities, list):
            amenity_sources.append(existing_amenities)

        for amenity_source in amenity_sources:
            for item in amenity_source:
                if not isinstance(item, str):
                    continue
                amenity = item.strip()
                if not amenity or amenity in seen_amenities:
                    continue
                seen_amenities.add(amenity)
                merged_amenities.append(amenity)

        if merged_amenities:
            merged["amenities"] = merged_amenities

    return merged


def _should_bypass_precache_for_url_mode(
    input_mode: str,
    listing_url: Optional[str],
) -> bool:
    """
    URL mode must scrape at least once to replace placeholder attributes
    (e.g. 1 bed / 1 bath / 2 guests) with real listing specs.
    """
    return input_mode == "url" and bool(listing_url)


def _build_scrape_calendar(
    daily_results: list,
    start_date: str,
    end_date: str,
    discount_policy: Dict[str, Any],
    transparent_result: Dict[str, Any],
) -> tuple:
    """
    Build summary + calendar from day-by-day scrape results.

    Each entry in daily_results is a dict with at least:
      date, median_price, is_weekend, flags

    Uses per-day median_price as basePrice.  Falls back to the overall
    median for days with no data.  Returns (None, None) if ALL days
    have no price data.
    """
    from datetime import datetime as dt, timedelta as td, timezone as tz
    import statistics as _stats

    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    start = dt.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz.utc)
    end = dt.strptime(end_date, "%Y-%m-%d").replace(tzinfo=tz.utc)
    today = dt.now(tz.utc).date()
    total_days = max(1, (end - start).days)

    def _policy_num(*keys: str) -> Optional[float]:
        for key in keys:
            if key in discount_policy and discount_policy.get(key) is not None:
                try:
                    return float(discount_policy[key])
                except Exception:
                    continue
        return None

    def _cap_price(price_value: int, floor_value: Optional[float], ceiling_value: Optional[float]) -> int:
        out = float(price_value)
        if floor_value is not None:
            out = max(out, float(floor_value))
        if ceiling_value is not None:
            out = min(out, float(ceiling_value))
        return round(out)

    min_price_floor = _policy_num("minPriceFloor", "min_price_floor")
    max_price_ceiling = _policy_num("maxPriceCeiling", "max_price_ceiling")

    # Build a date -> daily_result lookup
    dr_map: Dict[str, Dict] = {}
    for dr in daily_results:
        dr_map[dr["date"]] = dr

    # Compute overall median for fallback
    valid_prices = [
        dr["median_price"] for dr in daily_results
        if dr.get("median_price") is not None
    ]
    if not valid_prices:
        return None, None

    overall_median = round(_stats.median(valid_prices))

    # Build calendar inputs used by the unified dynamic adjustment pipeline.
    calendar_inputs = []
    for i in range(total_days):
        d = start + td(days=i)
        ds = d.strftime("%Y-%m-%d")
        dr = dr_map.get(ds)
        raw_base = dr.get("median_price") if dr else None
        base_daily_price = round(raw_base) if raw_base is not None else None
        calendar_inputs.append(
            {
                "date": d.date(),
                "baseDailyPrice": base_daily_price,
                "compsUsed": int((dr or {}).get("comps_used") or 0),
                "priceDistribution": (dr or {}).get("price_distribution") or {},
                "flags": list((dr or {}).get("flags") or []),
            }
        )

    dynamic_rows = compute_dynamic_pricing_adjustment(today, calendar_inputs)

    # Build calendar days with discounts after dynamic layer.
    calendar = []
    for i, dynamic in enumerate(dynamic_rows):
        d = start + td(days=i)
        ds = d.strftime("%Y-%m-%d")
        dow = d.weekday()
        is_weekend = dow >= 4  # Fri, Sat

        base_daily_price = dynamic.get("baseDailyPrice")
        price_after_time_adjustment = dynamic.get("priceAfterTimeAdjustment")
        flags = list(dynamic.get("flags") or [])

        if price_after_time_adjustment is not None:
            disc = apply_discount(price_after_time_adjustment, total_days, discount_policy)
            effective_refundable = _cap_price(
                disc["refundablePrice"],
                min_price_floor,
                max_price_ceiling,
            )
            effective_non_refundable = _cap_price(
                disc["nonRefundablePrice"],
                min_price_floor,
                max_price_ceiling,
            )
            legacy_base_price = price_after_time_adjustment
            legacy_refundable = effective_refundable
            legacy_non_refundable = effective_non_refundable
        else:
            # Keep legacy fields numeric for backward-compatible UI rendering.
            legacy_base_price = overall_median
            legacy_disc = apply_discount(legacy_base_price, total_days, discount_policy)
            legacy_refundable = _cap_price(
                legacy_disc["refundablePrice"],
                min_price_floor,
                max_price_ceiling,
            )
            legacy_non_refundable = _cap_price(
                legacy_disc["nonRefundablePrice"],
                min_price_floor,
                max_price_ceiling,
            )
            effective_refundable = None
            effective_non_refundable = None

        # ── Canonical daily recommendation field ─────────────────────────
        # The single user-facing recommended listing price for this date.
        # Derived as: market median × demand adjustment (no time/last-minute
        # discount applied — that is an internal strategy, not the host's
        # recommended list price).
        #
        # demandAdjustment range: 0.90 (low-demand) → 1.05 (peak/weekend)
        # — weekends +8%, peak/event +15%, low-demand −15%, tight spread slight boost
        # This makes the recommendation genuinely distinct from the raw market
        # reference (baseDailyPrice) and day-variation-aware.
        _demand_adj = (dynamic.get("dynamicAdjustment") or {}).get("demandAdjustment", 1.0)
        _rec_base = base_daily_price if base_daily_price is not None else overall_median
        _recommended_daily = round(_rec_base * _demand_adj)
        _flags_lc = {str(f).strip().lower() for f in flags}
        _is_target_only_fallback = "target_listing_only_fallback" in _flags_lc
        _user_listing_price = (
            round(base_daily_price)
            if (_is_target_only_fallback and isinstance(base_daily_price, (int, float)) and base_daily_price > 0)
            else None
        )

        entry: Dict[str, Any] = {
            "date": ds,
            "dayOfWeek": DAY_NAMES[dow],
            "isWeekend": is_weekend,
            "flags": flags,

            # ── CANONICAL USER-FACING RECOMMENDATION ───────────────────────
            # Primary price for all dashboard/report/alert surfaces.
            # = baseDailyPrice × demandAdjustment (no time/last-minute discount).
            "recommendedDailyPrice": _recommended_daily,

            # ── MARKET REFERENCE ───────────────────────────────────────────
            # Raw per-day market median.  Use for market-line in charts and
            # transparency displays.  NOT the canonical recommendation.
            "baseDailyPrice": base_daily_price,
            # User listing nightly price for this exact day when available.
            # Populated in target-only fallback mode here; live day-0 capture is
            # attached later via _attach_user_listing_prices_and_log().
            "userListingPrice": _user_listing_price,

            # ── INTERNAL ADJUSTMENT PIPELINE ──────────────────────────────
            # These fields are internal pipeline stages and transparency data.
            # Do NOT surface them as "recommended price" in new UI work.
            "dynamicAdjustment": dynamic.get("dynamicAdjustment"),
            # timeMultiplier alias — last-minute discount factor; excluded from recommendation
            "lastMinuteMultiplier": (dynamic.get("dynamicAdjustment") or {}).get(
                "timeMultiplier"
            ),
            # baseDailyPrice × finalMultiplier (time + demand combined) — includes LM discount
            "priceAfterTimeAdjustment": price_after_time_adjustment,
            # Full discount stack applied on top of priceAfterTimeAdjustment — internal only
            "effectiveDailyPriceRefundable": effective_refundable,
            "effectiveDailyPriceNonRefundable": effective_non_refundable,

            # ── LEGACY COMPATIBILITY ───────────────────────────────────────
            # Retained so old UI readers do not break.  New code must use
            # recommendedDailyPrice instead of these fields.
            "basePrice": legacy_base_price,           # = priceAfterTimeAdjustment or overallMedian
            "refundablePrice": legacy_refundable,     # basePrice with discount stack
            "nonRefundablePrice": legacy_non_refundable,  # refundablePrice + non-refundable
        }
        calendar.append(entry)

    # ── Summary stats ─────────────────────────────────────────────────────────
    # Market proxy statistics derived from the legacy basePrice field
    # (= priceAfterTimeAdjustment, or overallMedian for missing days).
    # These are backward-compatible market reference metrics, not guaranteed to be
    # raw unadjusted per-day market medians — near-term dates may reflect time/demand
    # multipliers from the dynamic pricing pipeline.
    # These stats (nightlyMin/Median/Max, weekdayAvg, weekendAvg, revenue estimates)
    # are market proxy / reference values — NOT the canonical recommendation.
    # The canonical recommendation is pinned separately below as
    # summary["recommendedPrice"]["nightly"] = calendar[0]["recommendedDailyPrice"].
    base_prices = [d["basePrice"] for d in calendar]
    sorted_p = sorted(base_prices)
    median = sorted_p[len(sorted_p) // 2]  # market median reference (nightlyMedian)
    min_p = sorted_p[0]
    max_p = sorted_p[-1]

    # Weekday/weekend averages: use actual market comparable medians from the
    # transparent result when available.  These represent what comparable
    # listings charge on those days — not the user's recommended prices.
    # NOTE: rec here is the pre-pin engine recommendation (transparent_result["nightly"]),
    # used only for insightHeadline generation.  After the canonical pin block below,
    # summary["recommendedPrice"]["nightly"] will be replaced with day-0
    # recommendedDailyPrice (which may differ from rec).
    rec_price_info = (transparent_result or {}).get("recommendedPrice") or {}
    rec = rec_price_info.get("nightly")   # engine's pre-pin recommendation (for headline only)
    market_weekday = rec_price_info.get("weekdayEstimate")
    market_weekend = rec_price_info.get("weekendEstimate")

    weekday_p = [d["basePrice"] for d in calendar if not d["isWeekend"]]
    weekend_p = [d["basePrice"] for d in calendar if d["isWeekend"]]
    weekday_avg = (
        round(market_weekday) if market_weekday
        else (round(sum(weekday_p) / len(weekday_p)) if weekday_p else median)
    )
    weekend_avg = (
        round(market_weekend) if market_weekend
        else (round(sum(weekend_p) / len(weekend_p)) if weekend_p else median)
    )

    # Occupancy: there is no way to determine true booking occupancy from search
    # data alone.  Use a data-quality proxy: higher market data coverage
    # (fraction of days where we found valid comparable prices) indicates a
    # more active, higher-demand market → higher occupancy estimate.
    valid_day_count = sum(1 for dr in daily_results if dr.get("median_price") is not None)
    coverage_pct = valid_day_count / max(1, len(daily_results))
    if coverage_pct >= 0.80:
        occupancy = 73
    elif coverage_pct >= 0.60:
        occupancy = 67
    elif coverage_pct >= 0.40:
        occupancy = 60
    else:
        occupancy = 53

    selected_range_avg = average_refundable_price_for_stay(
        base_prices, total_days, discount_policy
    )
    est_monthly = round(selected_range_avg * 30 * (occupancy / 100))
    if rec and median:
        diff = round(median - rec)
        if abs(diff) <= 5:
            headline = f"Your recommended price of ${round(rec)}/night is well-aligned with the local market."
        elif diff > 5:
            headline = f"At ${round(rec)}/night you're competitively positioned against the ${median} market median."
        else:
            headline = f"Comparable listings average ${median}/night. Your recommended price factors in market positioning."
    else:
        headline = f"Based on nearby comparable listings, the median nightly price is ${median}."

    weekly_avg = average_refundable_price_for_stay(
        base_prices, min(7, total_days), discount_policy
    )
    monthly_avg = average_refundable_price_for_stay(
        base_prices, min(28, total_days), discount_policy
    )
    stay_length_averages = build_stay_length_averages(
        base_prices, total_days, discount_policy
    )

    summary = {
        "insightHeadline": headline,
        "nightlyMin": min_p,
        "nightlyMedian": median,
        "nightlyMax": max_p,
        "occupancyPct": occupancy,
        "weekdayAvg": weekday_avg,
        "weekendAvg": weekend_avg,
        "estimatedMonthlyRevenue": est_monthly,
        "weeklyStayAvgNightly": weekly_avg,
        "monthlyStayAvgNightly": monthly_avg,
        "selectedRangeNights": total_days,
        "selectedRangeAvgNightly": selected_range_avg,
        "stayLengthAverages": stay_length_averages,
    }

    return summary, calendar


def _build_target_listing_only_daily_results(
    *,
    listing_url: str,
    start_date: str,
    end_date: str,
    minimum_booking_nights: int,
    report_id: str,
) -> List[Dict[str, Any]]:
    """
    Fallback: build day-level rows using only the user's own listing price.

    Used when URL-mode comp scraping returns no usable daily market prices.
    This keeps the report/calendar renderable so the dashboard heatmap appears.
    """
    from datetime import datetime as _dt, timedelta as _td

    out: List[Dict[str, Any]] = []
    prices_payload = _capture_user_listing_prices_for_range(
        report_id=report_id,
        listing_url=listing_url,
        start_date=start_date,
        end_date=end_date,
        minimum_booking_nights=minimum_booking_nights,
    )
    by_date: Dict[str, int] = prices_payload.get("priceByDate") or {}

    start = _dt.strptime(start_date, "%Y-%m-%d")
    end = _dt.strptime(end_date, "%Y-%m-%d")
    total_days = max(1, (end - start).days)

    for i in range(total_days):
        checkin_dt = start + _td(days=i)
        checkin = checkin_dt.strftime("%Y-%m-%d")
        _price = by_date.get(checkin)
        if isinstance(_price, (int, float)) and _price > 0:
            price = round(float(_price))
            out.append(
                {
                    "date": checkin,
                    "median_price": price,
                    "is_weekend": checkin_dt.weekday() >= 4,  # Fri/Sat
                    "flags": ["target_listing_only_fallback"],
                    "comps_used": 1,
                    "price_distribution": {
                        "min": price,
                        "p25": price,
                        "median": price,
                        "p75": price,
                        "max": price,
                        "currency": "USD",
                    },
                }
            )
        else:
            out.append(
                {
                    "date": checkin,
                    "median_price": None,
                    "is_weekend": checkin_dt.weekday() >= 4,
                    "flags": ["target_listing_only_fallback", "missing_data"],
                    "comps_used": 0,
                    "price_distribution": {},
                }
            )

    logger.info(
        f"[{report_id}] Target-only fallback daily capture: "
        f"{prices_payload.get('capturedDays', 0)}/{total_days} days priced"
    )
    return out


def _capture_user_listing_prices_for_range(
    *,
    report_id: str,
    listing_url: str,
    start_date: str,
    end_date: str,
    minimum_booking_nights: int,
) -> Dict[str, Any]:
    """
    Capture user-listing nightly prices for each report day using the same
    day-query threading settings as compset scraping.
    """
    from datetime import datetime as _dt, timedelta as _td
    from worker.core.concurrent_runner import execute_day_queries_concurrently
    from worker.scraper.browser_runtime import (
        build_warmed_browser_client_pool,
        close_browser_client_pool,
    )
    from worker.scraper.target_extractor import capture_target_live_price

    start = _dt.strptime(start_date, "%Y-%m-%d")
    end = _dt.strptime(end_date, "%Y-%m-%d")
    total_days = max(1, (end - start).days)
    nights = max(1, int(minimum_booking_nights or 1))
    worker_count = DAY_QUERY_MAX_WORKERS
    browser_pool = build_warmed_browser_client_pool(
        base_config={
            "CHECKIN": start_date,
            "CHECKOUT": end_date,
            "ADULTS": 1,
            "USE_DEEPBNB_BACKEND": False,
            "CDP_URL": CDP_URL,
        },
        requested_size=max(1, min(worker_count, total_days)),
        pool_name="self_price_capture",
    )
    browser_locks = [threading.Lock() for _ in browser_pool]
    browser_count = len(browser_pool)
    query_args = [
        {"day_index": i, "browser_slot": i % browser_count}
        for i in range(total_days)
    ]

    def _capture_for_index(query_arg: Dict[str, int]) -> Dict[str, Any]:
        i = int(query_arg["day_index"])
        browser_slot = int(query_arg["browser_slot"])
        playwright_live_client = browser_pool[browser_slot]
        day_lock = browser_locks[browser_slot]
        checkin_dt = start + _td(days=i)
        checkin = checkin_dt.strftime("%Y-%m-%d")
        checkout = (checkin_dt + _td(days=nights)).strftime("%Y-%m-%d")
        logger.info(
            f"[{report_id}] [self_price_capture] day_start "
            f"index={i} browser_slot={browser_slot} checkin={checkin} checkout={checkout} nights={nights}"
        )
        time.sleep(RATE_LIMIT_SECONDS)
        try:
            with day_lock:
                live = capture_target_live_price(
                    listing_url=listing_url,
                    checkin=checkin,
                    checkout=checkout,
                    cdp_url=playwright_live_client.cdp_url,
                    cdp_connect_timeout_ms=CDP_CONNECT_TIMEOUT_MS,
                    client=playwright_live_client,
                    allow_retry_matrix=False,
                )
            logger.info(
                f"[{report_id}] [self_price_capture] day_result_raw "
                f"date={checkin} status={live.get('livePriceStatus')} "
                f"reason={str(live.get('livePriceStatusReason') or '')[:200]} "
                f"observed={live.get('observedListingPrice')} "
                f"source={live.get('observedListingPriceSource')} "
                f"confidence={live.get('observedListingPriceConfidence')}"
            )
        except Exception as exc:
            logger.warning(
                f"[{report_id}] User-listing daily capture failed for {checkin}: {exc}"
            )
            live = {
                "observedListingPrice": None,
                "livePriceStatus": "scrape_failed",
                "livePriceStatusReason": str(exc)[:300],
            }

        obs = live.get("observedListingPrice")
        price = round(float(obs)) if isinstance(obs, (int, float)) and obs > 0 else None
        logger.info(
            f"[{report_id}] [self_price_capture] day_result_normalized "
            f"date={checkin} normalized_price={price} status={live.get('livePriceStatus')}"
        )
        return {
            "date": checkin,
            "price": price,
            "status": str(live.get("livePriceStatus") or ""),
            "reason": str(live.get("livePriceStatusReason") or ""),
            "source": live.get("observedListingPriceSource"),
            "confidence": live.get("observedListingPriceConfidence"),
            "captured_at": live.get("observedListingPriceCapturedAt"),
        }

    logger.info(
        f"[{report_id}] user-listing daily capture phase start "
        f"(after daily-query phase complete): workers={worker_count}, "
        f"browsers={browser_count}, dates={total_days}"
    )
    try:
        rows, _state = execute_day_queries_concurrently(
            query_func=_capture_for_index,
            args_list=query_args,
            max_workers=min(worker_count, browser_count),
            early_stop_threshold=None,
            progress_callback=None,
        )
    finally:
        close_browser_client_pool(browser_pool)

    price_by_date: Dict[str, int] = {}
    first_day_row: Optional[Dict[str, Any]] = None
    first_captured_row: Optional[Dict[str, Any]] = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        day_date = str(row.get("date") or "")
        if day_date == start_date and first_day_row is None:
            first_day_row = row
        day_price = row.get("price")
        if isinstance(day_price, (int, float)) and day_price > 0:
            price_by_date[day_date] = round(float(day_price))
            if first_captured_row is None:
                first_captured_row = row

    _first_day_price = (first_day_row or {}).get("price")
    if isinstance(_first_day_price, (int, float)) and _first_day_price > 0:
        picked = first_day_row or {}
    else:
        picked = first_captured_row or first_day_row or {}
    captured_days = len(price_by_date)
    live_status = "captured" if captured_days > 0 else "no_price_found"
    live_reason = (
        f"Captured {captured_days}/{total_days} day-level user listing prices"
        if captured_days > 0
        else "No nightly price found across the selected report range"
    )

    return {
        "priceByDate": price_by_date,
        "capturedDays": captured_days,
        "totalDays": total_days,
        "observedListingPrice": picked.get("price"),
        "observedListingPriceDate": picked.get("date") or start_date,
        "observedListingPriceCapturedAt": picked.get("captured_at"),
        "observedListingPriceSource": picked.get("source"),
        "observedListingPriceConfidence": picked.get("confidence"),
        "livePriceStatus": live_status,
        "livePriceStatusReason": live_reason,
    }


def _attach_user_listing_prices_and_log(
    report_id: str,
    calendar: List[Dict[str, Any]],
    summary: Dict[str, Any],
) -> None:
    """
    Ensure each calendar day has a day-level userListingPrice field and emit
    one diagnostic log line per day explaining price presence/absence.
    """
    if not isinstance(calendar, list):
        return

    observed_date = summary.get("observedListingPriceDate")
    observed_price = summary.get("observedListingPrice")
    has_observed = isinstance(observed_price, (int, float)) and observed_price > 0

    for day in calendar:
        if not isinstance(day, dict):
            continue
        day_date = str(day.get("date") or "")
        flags = [str(f).strip().lower() for f in (day.get("flags") or [])]
        is_target_only = "target_listing_only_fallback" in flags
        has_missing_flag = "missing_data" in flags

        user_price = day.get("userListingPrice")
        if not (isinstance(user_price, (int, float)) and user_price > 0):
            if has_observed and isinstance(observed_date, str) and day_date == observed_date:
                user_price = round(float(observed_price))
                day["userListingPrice"] = user_price
            else:
                day["userListingPrice"] = None
                user_price = None

        if isinstance(user_price, (int, float)) and user_price > 0:
            source = (
                "target_only_fallback"
                if is_target_only
                else ("live_capture_day_match" if has_observed and day_date == observed_date else "other")
            )
            logger.info(
                f"[{report_id}] [user_day_price] date={day_date} price={round(float(user_price))} source={source}"
            )
        else:
            if has_missing_flag:
                reason = "missing_data_flag"
            elif is_target_only:
                reason = "target_only_no_price"
            elif has_observed and isinstance(observed_date, str):
                reason = (
                    "live_price_captured_for_different_day"
                    if day_date != observed_date
                    else "live_price_missing_on_observed_day"
                )
            else:
                reason = "no_user_listing_price_signal"
            logger.info(
                f"[{report_id}] [user_day_price] date={day_date} price=None reason={reason}"
            )


def process_job(job: Dict[str, Any], worker_token: uuid.UUID) -> None:
    """
    Dispatcher: resolves execution policy and routes to the correct pipeline.

    forecast_snapshot jobs are rejected immediately (deprecated).
    All live_analysis jobs are routed to run_nightly_job() or
    run_interactive_job() based on the resolved execution_policy.
    """
    report_id = job["id"]
    job_lane = job.get("job_lane", "interactive")

    # Safeguard: forecast_snapshot jobs must never be executed.
    # The creation API routes now return 410 Gone, so no new ones should arrive.
    # Any residual queued forecast_snapshot rows from before the deprecation
    # are failed here so they don't block the queue.
    if job.get("report_type") == "forecast_snapshot":
        logger.warning(
            f"[{report_id}] Received deprecated forecast_snapshot job — failing immediately. "
            "No new forecast_snapshot jobs should be created."
        )
        client = db_helpers.get_client()
        db_helpers.fail_job(
            client, report_id, worker_token,
            error_message="forecast_snapshot has been removed. Please run a live analysis instead.",
            debug={
                "error": "forecast_snapshot_deprecated",
                "worker_host": socket.gethostname(),
                "worker_version": WORKER_VERSION,
            },
        )
        return

    policy = resolve_execution_policy(job)
    is_nightly = policy in NIGHTLY_POLICIES
    pipeline = "nightly" if is_nightly else "interactive"

    logger.info(
        f"[{report_id}] dispatch: job_lane={job_lane} "
        f"execution_policy={policy} pipeline={pipeline}"
    )

    if is_nightly:
        run_nightly_job(job, worker_token)
    else:
        run_interactive_job(job, worker_token)


def run_nightly_job(job: Dict[str, Any], worker_token: uuid.UUID) -> None:
    """
    Nightly pipeline entry point (execution_policy=nightly_board_refresh).

    Delegates to _execute_analysis() with is_nightly=True.
    This function is an explicit hook for nightly-only pre/post logic in
    future phases (e.g., routing nightly_alert_training_refresh separately).
    """
    _execute_analysis(job, worker_token, is_nightly=True)


def run_interactive_job(job: Dict[str, Any], worker_token: uuid.UUID) -> None:
    """
    Interactive pipeline entry point (execution_policy=interactive_live_report).

    Delegates to _execute_analysis() with is_nightly=False.
    Interactive reports use snapshot semantics — inputs are not reloaded
    from saved_listings at execution time.
    """
    _execute_analysis(job, worker_token, is_nightly=False)


def _normalize_auto_apply_calendar(raw_calendar: Any) -> Dict[str, int]:
    if not isinstance(raw_calendar, dict):
        raise ValueError("calendar payload is not an object")

    normalized: Dict[str, int] = {}
    for date, price in raw_calendar.items():
        if not isinstance(date, str):
            raise ValueError("calendar date keys must be strings")
        if price is None:
            continue
        normalized[date] = int(round(float(price)))
    return normalized


def _extract_airbnb_listing_id_from_url(listing_url: Optional[str]) -> Optional[str]:
    if not listing_url:
        return None
    m = re.search(r"/rooms/(\d+)", str(listing_url))
    return m.group(1) if m else None


def _resolve_airbnb_listing_id_for_price_update(client, saved_listing_id: str) -> Optional[str]:
    """
    price_update_jobs.listing_id stores saved_listings.id (UUID), but Airbnb mutation
    requires numeric Airbnb room/listing id. Resolve via saved_listings.input_attributes.
    """
    try:
        row = (
            client.table("saved_listings")
            .select("input_attributes")
            .eq("id", saved_listing_id)
            .single()
            .execute()
        )
        data = row.data or {}
        attrs = data.get("input_attributes") or {}
        listing_url = attrs.get("listingUrl") or attrs.get("listing_url")
        return _extract_airbnb_listing_id_from_url(listing_url)
    except Exception:
        return None


def process_price_update_job(
    job: Dict[str, Any],
    worker_token: uuid.UUID,
    client,
) -> None:
    """Process one queued price_update_jobs record (auto-apply writeback)."""
    job_id = str(job["id"])
    saved_listing_id = str(job.get("listing_id") or "")
    attempts = int(job.get("worker_attempts") or 0)

    if not saved_listing_id:
        db_helpers.fail_price_update_job(
            client,
            job_id,
            worker_token,
            error_message="Missing listing_id on queued job.",
            result_payload={"ok": False, "error": "missing listing_id"},
        )
        return

    airbnb_listing_id = _resolve_airbnb_listing_id_for_price_update(client, saved_listing_id)
    if not airbnb_listing_id:
        db_helpers.fail_price_update_job(
            client,
            job_id,
            worker_token,
            error_message=(
                "Unable to resolve Airbnb listing id from saved listing URL. "
                "Ensure input_attributes.listingUrl is a valid airbnb.com/rooms/{id} URL."
            ),
            result_payload={"ok": False, "error": "missing_airbnb_listing_id"},
        )
        return

    if attempts > AUTO_APPLY_MAX_ATTEMPTS:
        db_helpers.fail_price_update_job(
            client,
            job_id,
            worker_token,
            error_message=f"Job exceeded max attempts ({attempts}).",
            result_payload={"ok": False, "error": "max attempts exceeded"},
        )
        return

    try:
        calendar = _normalize_auto_apply_calendar(job.get("calendar"))
        if not calendar:
            db_helpers.fail_price_update_job(
                client,
                job_id,
                worker_token,
                error_message="No calendar prices found in queued job.",
                result_payload={"ok": False, "error": "empty calendar"},
            )
            return

        logger.info(
            f"[{job_id}] Applying {len(calendar)} prices for saved_listing={saved_listing_id} "
            f"(airbnb_listing_id={airbnb_listing_id})"
        )
        result = assign_prices_calendar(
            listing_id=airbnb_listing_id,
            calendar=calendar,
            cdp_url=AUTO_APPLY_CDP_URL,
        )

        if result.get("ok"):
            db_helpers.complete_price_update_job(
                client,
                job_id,
                worker_token,
                result_payload=result,
            )
            logger.info(f"[{job_id}] Auto-apply completed successfully")
            return

        error_message = str(result.get("error") or "Price assignment failed.")
        db_helpers.fail_price_update_job(
            client,
            job_id,
            worker_token,
            error_message=error_message[:500],
            result_payload=result,
        )
        logger.error(f"[{job_id}] Auto-apply failed: {error_message}")
    except Exception as exc:
        db_helpers.fail_price_update_job(
            client,
            job_id,
            worker_token,
            error_message=f"Worker exception: {str(exc)[:400]}",
            result_payload={"ok": False, "error": str(exc)},
        )
        logger.exception(f"[{job_id}] Auto-apply unexpected worker error")


def _execute_analysis(job: Dict[str, Any], worker_token: uuid.UUID, *, is_nightly: bool) -> None:
    """
    Shared analysis engine for both nightly and interactive pipelines.

    Manages heartbeat, cache lookup, scrape execution (Mode A/B/C),
    result completion, alert evaluation (nightly only), geocoding,
    pool seeding, and cache writes.

    is_nightly controls:
      - Whether saved_listing inputs are live-reloaded before execution
      - Whether the cache key is recomputed after reload (nightly) or
        kept from the queued snapshot (interactive)
      - Whether refreshed inputs are written back to the report row
      - Whether alert evaluation runs after completion
    """
    report_id = job["id"]
    client = db_helpers.get_client()
    start_time = time.time()

    # Start heartbeat thread
    hb_stop = threading.Event()
    hb_thread = threading.Thread(
        target=_run_heartbeat,
        args=(report_id, worker_token, hb_stop),
        daemon=True,
    )
    hb_thread.start()

    def _progress(pct: int, stage: str, message: str, est: Optional[int] = None) -> None:
        """Update progress metadata in DB.  Non-fatal on error."""
        try:
            db_helpers.update_progress(
                client, report_id, worker_token,
                pct=pct, stage=stage, message=message,
                est_seconds_remaining=est,
            )
        except Exception as _pe:
            logger.warning(f"[{report_id}] Progress update failed (non-fatal): {_pe}")

    def _make_day_callback(start_pct: int, end_pct: int, stage: str, message_prefix: str) -> Callable[[int, int], None]:
        """Factory: maps (completed, total) day counts to a pct range and calls _progress."""
        def _cb(completed: int, total: int) -> None:
            if total <= 0:
                return
            frac = completed / total
            pct = round(start_pct + frac * (end_pct - start_pct))
            remaining_days = total - completed
            est = round(remaining_days * MAX_RUNTIME_SECONDS / max(total, 1)) if remaining_days > 0 else None
            _progress(pct, stage, f"{message_prefix} ({completed}/{total} days)", est)
        return _cb

    _progress(5, "connecting", "Connecting to browser...")

    try:
        # ── Nightly live-reload ───────────────────────────────────────────────
        # Scheduled nightly jobs use the saved listing as source-of-truth at
        # execution time, not the snapshot baked in at queue time.  This means
        # any benchmark / comp / setting changes the host makes after queuing
        # are still reflected in tonight's report.
        #
        # Interactive jobs keep snapshot semantics — their input_attributes
        # represent the user's deliberate choices at the moment they clicked Run.
        if is_nightly and job.get("listing_id"):
            try:
                _reload_row = (
                    client.table("saved_listings")
                    .select("input_address, input_attributes, default_discount_policy, minimum_booking_nights")
                    .eq("id", job["listing_id"])
                    .single()
                    .execute()
                )
                _fresh = _reload_row.data or {}
                if _fresh:
                    job = dict(job)  # shallow copy — do not mutate the original
                    if _fresh.get("input_address"):
                        job["input_address"] = _fresh["input_address"]
                    if _fresh.get("input_attributes") is not None:
                        _fresh_attrs = _fresh["input_attributes"] or {}
                        job["input_attributes"] = _fresh_attrs
                        # Keep input_listing_url in sync with refreshed attributes
                        job["input_listing_url"] = (
                            _fresh_attrs.get("listingUrl")
                            or _fresh_attrs.get("listing_url")
                            or job.get("input_listing_url")
                        )
                    if _fresh.get("default_discount_policy") is not None:
                        job["discount_policy"] = _fresh["default_discount_policy"]
                    if _fresh.get("minimum_booking_nights") is not None:
                        job["minimum_booking_nights"] = _fresh["minimum_booking_nights"]
                    logger.info(
                        f"[{report_id}] Nightly live-reload: refreshed inputs "
                        f"from saved_listing {job['listing_id']}"
                    )
            except Exception as _reload_exc:
                logger.warning(
                    f"[{report_id}] Nightly live-reload failed (non-fatal, using snapshot): "
                    f"{_reload_exc}"
                )
        
        address = job.get("input_address", "")
        attributes = job.get("input_attributes") or {}
        start_date = str(job.get("input_date_start", ""))
        end_date = str(job.get("input_date_end", ""))
        discount_policy = job.get("discount_policy") or {}
        listing_url = _get_listing_url(job)
        minimum_booking_nights = int(job.get("minimum_booking_nights") or 1)
        input_mode = attributes.get("inputMode", "criteria")
        # Extract preferred comps list — only keep items that are enabled
        preferred_comps_raw = attributes.get("preferredComps")
        preferred_comps: Optional[list] = None
        primary_benchmark_url: Optional[str] = None

        if isinstance(preferred_comps_raw, list):
            enabled = [
                pc for pc in preferred_comps_raw
                if isinstance(pc, dict) and pc.get("enabled", True)
            ]
            preferred_comps = enabled if enabled else None
        if preferred_comps:
            urls_preview = ", ".join(pc.get("listingUrl", "?") for pc in preferred_comps)
            logger.info(f"[{job.get('id', '?')}] Preferred comps ({len(preferred_comps)}): {urls_preview}")
            # The first enabled preferred comp is the primary benchmark
            first_url = str(preferred_comps[0].get("listingUrl") or "").strip()
            if first_url:
                primary_benchmark_url = first_url
                logger.info(f"[{job.get('id', '?')}] Primary benchmark URL: {primary_benchmark_url}")

        # Secondary benchmark URLs — preferredComps[1:] used for consensus signal only
        secondary_benchmark_urls: List[str] = [
            str(pc.get("listingUrl") or "").strip()
            for pc in (preferred_comps or [])[1:]
            if isinstance(pc, dict) and str(pc.get("listingUrl") or "").strip()
        ]

        if secondary_benchmark_urls:
            logger.info(
                f"[{job.get('id', '?')}] Secondary benchmark URLs "
                f"({len(secondary_benchmark_urls)}): {', '.join(secondary_benchmark_urls)}"
            )

        # Extract excluded comp roomIds (per-listing user blacklist).
        # Used by day-query / benchmark / criteria-anchor pipelines to skip
        # specific Airbnb listings the user has marked as bad comps.
        excluded_comps_raw = attributes.get("excludedComps")
        excluded_room_ids: set[str] = set()
        if isinstance(excluded_comps_raw, list):
            for ec in excluded_comps_raw:
                if not isinstance(ec, dict):
                    continue
                rid = str(ec.get("roomId") or "").strip()
                if rid:
                    excluded_room_ids.add(rid)
        if excluded_room_ids:
            logger.info(
                f"[{job.get('id', '?')}] Excluded comps ({len(excluded_room_ids)}): "
                f"{', '.join(sorted(excluded_room_ids))}"
            )

        finalized_input_attributes = dict(attributes)
        finalized_input_attributes["inputMode"] = input_mode
        finalized_input_attributes["listingUrl"] = listing_url or None
        bypass_precache = _should_bypass_precache_for_url_mode(
            str(input_mode), listing_url
        )

        # Check cache first (except URL mode where we must scrape to extract specs,
        # or explicit force_rerun jobs submitted from the dashboard).
        # For nightly jobs the queued cache_key was computed from the snapshot
        # at queue time — after live-reload, execution inputs may differ so we
        # must recompute from the actual values used.  Interactive jobs keep
        # the original key (snapshot semantics, no reload).
        if is_nightly:
            cache_key = compute_cache_key(
                address, attributes, start_date, end_date, discount_policy, listing_url, input_mode
            )
        else:
            cache_key = job.get("cache_key") or compute_cache_key(
                address, attributes, start_date, end_date, discount_policy, listing_url, input_mode
            )
        force_rerun = bool((job.get("result_core_debug") or {}).get("force_rerun"))
        cached = None if (bypass_precache or force_rerun) else get_cached(client, cache_key)
        if force_rerun:
            logger.info(f"[{report_id}] force_rerun=true — skipping cache lookup")
        if cached:
            summary, calendar = cached
            logger.info(f"[{report_id}] Cache hit for key={cache_key[:12]}...")
            # Live price is time-sensitive — always re-capture fresh even on a cache hit.
            # Shallow-copy summary so we don't mutate the cached object.
            summary = dict(summary)
            # Cache key already encodes excluded_room_ids, so the cached row
            # was generated under the same exclusion set. Write the field
            # explicitly to ensure backward compat with any pre-bump cached row.
            summary["excludedRoomIdsAtRun"] = sorted(excluded_room_ids)
            if listing_url:
                _cache_live_info = _capture_user_listing_prices_for_range(
                    report_id=report_id,
                    listing_url=listing_url,
                    start_date=start_date,
                    end_date=end_date,
                    minimum_booking_nights=minimum_booking_nights,
                )
                _cache_price_by_date = _cache_live_info.get("priceByDate") or {}
                for day in calendar:
                    if not isinstance(day, dict):
                        continue
                    _date = str(day.get("date") or "")
                    _p = _cache_price_by_date.get(_date)
                    day["userListingPrice"] = (
                        round(float(_p))
                        if isinstance(_p, (int, float)) and _p > 0
                        else None
                    )
                logger.info(
                    f"[{report_id}] Cache-hit user-listing daily capture: "
                    f"status={_cache_live_info.get('livePriceStatus')} "
                    f"captured_days={_cache_live_info.get('capturedDays')}/{_cache_live_info.get('totalDays')}"
                )
                summary.update(_cache_live_info)
                _observed = _cache_live_info.get("observedListingPrice")
                if isinstance(_observed, (int, float)) and _observed > 0:
                    _market_median = summary.get("nightlyMedian")
                    _recommended = (summary.get("recommendedPrice") or {}).get("nightly")
                    if isinstance(_market_median, (int, float)) and _market_median > 0:
                        _obs_vs_mkt_diff = round(_observed - _market_median)
                        _obs_vs_mkt_pct = round((_observed / _market_median - 1) * 100)
                        summary["observedVsMarketDiff"] = _obs_vs_mkt_diff
                        summary["observedVsMarketDiffPct"] = _obs_vs_mkt_pct
                        if _obs_vs_mkt_pct < -3:
                            summary["pricingPosition"] = "below_market"
                        elif _obs_vs_mkt_pct > 3:
                            summary["pricingPosition"] = "above_market"
                        else:
                            summary["pricingPosition"] = "at_market"
                    if isinstance(_recommended, (int, float)) and _recommended > 0:
                        _obs_vs_rec_diff = round(_observed - _recommended)
                        _obs_vs_rec_pct = round((_observed / _recommended - 1) * 100)
                        summary["observedVsRecommendedDiff"] = _obs_vs_rec_diff
                        summary["observedVsRecommendedDiffPct"] = _obs_vs_rec_pct
                        if _obs_vs_rec_diff > 10:
                            summary["pricingAction"] = "lower"
                            summary["pricingActionTarget"] = int(round(_recommended))
                        elif _obs_vs_rec_diff < -10:
                            summary["pricingAction"] = "raise"
                            summary["pricingActionTarget"] = int(round(_recommended))
                        else:
                            summary["pricingAction"] = "keep"
                            summary["pricingActionTarget"] = int(round(_observed))
            else:
                summary["livePriceStatus"] = "no_listing_url"
                summary["livePriceStatusReason"] = "No Airbnb listing URL configured for this property"
            _attach_user_listing_prices_and_log(report_id, calendar, summary)
            db_helpers.complete_job(
                client, report_id, worker_token,
                summary=summary,
                calendar=calendar,
                core_version=WORKER_VERSION + "+cache",
                debug={
                    "cache_hit": True,
                    "cache_key": cache_key,
                    "worker_host": socket.gethostname(),
                    "worker_version": WORKER_VERSION,
                    "total_ms": round((time.time() - start_time) * 1000),
                },
                input_attributes=finalized_input_attributes,
                # For nightly jobs: write all refreshed execution inputs back to the
                # report row so it fully reflects actual inputs, not queued snapshot.
                input_address=address if is_nightly else None,
                # write_input_listing_url=True allows explicit NULL-clear when no URL.
                input_listing_url=listing_url if is_nightly else None,
                write_input_listing_url=is_nightly,
                discount_policy=discount_policy if is_nightly else None,
                # Sync the recomputed execution cache key to the report row.
                cache_key=cache_key if is_nightly else None,
                queue_ml_forecast=is_nightly and bool(job.get("listing_id")) and NIGHTLY_QUEUE_ML_FORECAST,
                ml_training_scope=NIGHTLY_ML_TRAINING_SCOPE,
                ml_force_retrain=NIGHTLY_ML_FORCE_RETRAIN,
            )

            # ── Alert evaluation — nightly only (cache-hit path) ─────────────
            # Must run AFTER complete_job() so the report is already marked ready.
            # Non-fatal: alert failures never affect the job outcome.
            if is_nightly and job.get("listing_id"):
                try:
                    from worker.alerts import run_alert_evaluation
                    run_alert_evaluation(
                        job, summary, client,
                        listing_url=listing_url,
                        calendar=calendar,
                        cdp_url=CDP_URL,
                        cdp_connect_timeout_ms=CDP_CONNECT_TIMEOUT_MS,
                    )
                except Exception as _alert_exc:
                    logger.warning(
                        f"[{report_id}] Alert evaluation failed (non-fatal, cache-hit): {_alert_exc}"
                    )

            # ── Observation write — nightly only (cache-hit path) ─────────────
            # Writes normalized per-date observations to the Phase-5A tables
            # after the pricing_reports row is already marked ready.
            # Non-fatal: observation failures never affect the job outcome.
            if is_nightly and job.get("listing_id"):
                try:
                    from worker.core.observations import write_nightly_observations
                    write_nightly_observations(
                        client,
                        saved_listing_id=job["listing_id"],
                        pricing_report_id=report_id,
                        captured_at=datetime.utcnow(),
                        summary=summary,
                        calendar=calendar,
                    )
                except Exception as _obs_exc:
                    logger.warning(
                        f"[{report_id}] Observation write failed (non-fatal, cache-hit): {_obs_exc}"
                    )
            return

        # transparent_result holds the new structured output from scrape/criteria
        transparent_result: Optional[Dict[str, Any]] = None

        def _fail(error_msg: str, detail: str = "") -> None:
            """Mark job as error — no mock fallback."""
            effective_msg = error_msg
            debug_error = detail or error_msg

            # Convert host-memory crashes into an explicit actionable error.
            # This avoids opaque "service busy" failures when WinError 1455 occurs.
            if detail:
                low = str(detail).lower()
                if (
                    "winerror 1455" in low
                    or "paging file is too small" in low
                    or ("oserror(22" in low and "1455" in low)
                ):
                    effective_msg = (
                        "Worker host is under memory pressure (Windows virtual memory exhausted). "
                        "Please increase page file size and retry."
                    )
                    debug_error = "host_memory_pressure_winerror_1455"

            logger.warning(f"[{report_id}] Failing: {detail or error_msg}")
            db_helpers.fail_job(
                client, report_id, worker_token,
                error_message=effective_msg,
                debug={
                    "error": debug_error,
                    "worker_host": socket.gethostname(),
                    "worker_version": WORKER_VERSION,
                    "total_ms": round((time.time() - start_time) * 1000),
                },
            )

        # ── Phase 3A: Resolve target coordinates ─────────────────────────
        # Fetch existing coords from the saved listing, or geocode the address
        # if they are missing.  Best-effort: failure is logged and skipped.
        _job_target_lat: Optional[float] = None
        _job_target_lng: Optional[float] = None
        _geocoded_now = False  # True if we just geocoded (need to write back)
        _db_timezone: Optional[str] = None

        _listing_id_for_geocode = job.get("listing_id")
        if _listing_id_for_geocode:
            try:
                _geo_row = (
                    client.table("saved_listings")
                    .select("target_lat, target_lng, listing_timezone")
                    .eq("id", _listing_id_for_geocode)
                    .single()
                    .execute()
                )
                _geo_data = (_geo_row.data or {})
                _db_lat = _geo_data.get("target_lat")
                _db_lng = _geo_data.get("target_lng")
                _db_timezone = _geo_data.get("listing_timezone")
                if _db_lat is not None and _db_lng is not None:
                    _job_target_lat = float(_db_lat)
                    _job_target_lng = float(_db_lng)
                    logger.info(
                        f"[{report_id}] Target coords from DB: "
                        f"({_job_target_lat:.5f}, {_job_target_lng:.5f})"
                    )
                else:
                    # Coords not yet stored — geocode the input address
                    from worker.core.geocoding import geocode_address
                    _gc = geocode_address(address)
                    if _gc:
                        _job_target_lat, _job_target_lng = _gc
                        _geocoded_now = True
                        logger.info(
                            f"[{report_id}] Geocoded target: "
                            f"({_job_target_lat:.5f}, {_job_target_lng:.5f})"
                        )
            except Exception as _geo_exc:
                logger.warning(
                    f"[{report_id}] Target coord resolution failed (non-fatal): {_geo_exc}"
                )

        # ── Phase 3B: Adaptive radius selection ──────────────────────────
        _job_radius_km: float = 30.0  # default; overwritten below if pool data available
        if _listing_id_for_geocode:
            try:
                from worker.core.geo_radius import select_adaptive_radius
                _pool_rows = (
                    client.table("comparable_pool_entries")
                    .select("distance_to_target_km")
                    .eq("saved_listing_id", _listing_id_for_geocode)
                    .eq("status", "active")
                    .execute()
                )
                _pool_distances = [
                    r.get("distance_to_target_km")
                    for r in (_pool_rows.data or [])
                ]
                _pool_active_size = len([d for d in _pool_distances if d is not None])
                _job_radius_km, _radius_reason = select_adaptive_radius(
                    pool_distances=_pool_distances,
                    active_pool_size=_pool_active_size or None,
                )
                logger.info(
                    f"[{report_id}] Adaptive radius: {_job_radius_km:.0f} km — {_radius_reason}"
                )
            except Exception as _radius_exc:
                logger.warning(
                    f"[{report_id}] Radius selection failed (using 30 km default): {_radius_exc}"
                )

        _mode_c_succeeded = False
        # benchmarkInfo is preserved here even when Mode C falls back to Mode B/A.
        # Without this, a failed-but-attempted benchmark run would lose all transparency.
        _saved_benchmark_info: Optional[Dict[str, Any]] = None

        # ── Nightly crawl plans ───────────────────────────────────────────────
        # Build tiered date-selection plans for nightly jobs before entering the
        # Mode C/A/B selection.  Interactive jobs leave both plans as None so all
        # three scrape functions keep their existing interactive behavior unchanged.
        #
        # Two plans are built:
        #   _nightly_plan           — standard plan for Mode A/B (criteria/URL)
        #   _nightly_plan_benchmark — tighter plan for Mode C (benchmark), keeping
        #                             benchmark volume at parity with the pre-Phase-3
        #                             BENCHMARK_MAX_SAMPLE_QUERIES=10 path.
        _nightly_plan = None
        _nightly_plan_benchmark = None
        if is_nightly:
            try:
                from datetime import date as _date
                from worker.core.nightly_strategy import build_nightly_crawl_plan
                _d_start = _date.fromisoformat(start_date)
                _d_end = _date.fromisoformat(end_date)
                _total_nights = max(1, (_d_end - _d_start).days)
                _nightly_plan = build_nightly_crawl_plan(_total_nights, mode="standard")
                _nightly_plan_benchmark = build_nightly_crawl_plan(_total_nights, mode="benchmark")
                logger.info(
                    f"[{report_id}] Nightly crawl plan (standard): "
                    f"observe={len(_nightly_plan.observe_indices)} "
                    f"infer={len(_nightly_plan.infer_indices)} "
                    f"of {_total_nights} nights | "
                    f"scroll_rounds={_nightly_plan.scroll_rounds} "
                    f"max_cards={_nightly_plan.max_cards} "
                    f"early_stop={_nightly_plan.early_stop_threshold}"
                )
                logger.info(
                    f"[{report_id}] Nightly crawl plan (benchmark): "
                    f"observe={len(_nightly_plan_benchmark.observe_indices)} "
                    f"of {_total_nights} nights | "
                    f"scroll_rounds={_nightly_plan_benchmark.scroll_rounds} "
                    f"max_cards={_nightly_plan_benchmark.max_cards}"
                )
            
            except Exception as _plan_exc:
                logger.warning(
                    f"[{report_id}] Failed to build nightly crawl plan (non-fatal, "
                    f"falling back to interactive sampling): {_plan_exc}"
                )
                _nightly_plan = None
                _nightly_plan_benchmark = None

        # ── Phase 6A: Observation-first reuse (interactive, criteria modes) ─────
        # Before live-scraping, attempt to assemble the report from stored
        # nightly observations.  Only applies when:
        #   - this is NOT a nightly job (is_nightly=False)
        #   - the job has a saved listing_id  (listing shorthand / rerun flows)
        #   - input_mode is criteria-based   (url mode must scrape for specs)
        # Fails non-fatally: any exception falls back to the live scrape path.
        _obs_reuse_succeeded = False
        _obs_assessment = None
        if (
            not is_nightly
            and job.get("listing_id")
            and input_mode in ("criteria", "criteria-by-city", "criteria-by-zip")
        ):
            try:
                from worker.core.observation_reuse import (
                    assess_observation_coverage,
                    REUSE_ELIGIBLE_MODES,
                )
                _obs_assessment = assess_observation_coverage(
                    client,
                    saved_listing_id=job["listing_id"],
                    start_date=start_date,
                    end_date=end_date,
                )
                logger.info(
                    f"[{report_id}] Observation reuse: "
                    f"eligible={_obs_assessment.eligible}  "
                    f"dates={len(_obs_assessment.dates_requested)}  "
                    f"reason={_obs_assessment.reason!r}"
                )
                if _obs_assessment.eligible:
                    _progress(15, "assembling", "Assembling report from stored market observations...")
                    _reu_result = _build_scrape_calendar(
                        _obs_assessment.assembled_rows,
                        start_date, end_date, discount_policy,
                        None,  # transparent_result — not available from observations
                    )
                    if _reu_result[0] is not None and _reu_result[1] is not None:
                        summary, calendar = _reu_result
                        core_version = WORKER_VERSION + "+obs_reuse"
                        transparent_result = None
                        _obs_reuse_succeeded = True
                        logger.info(
                            f"[{report_id}] Observation reuse succeeded: "
                            f"{len(_obs_assessment.dates_reusable)} dates served from "
                            f"stored observations (live scrape skipped)"
                        )
                    else:
                        logger.warning(
                            f"[{report_id}] Observation reuse calendar build returned no "
                            f"valid prices — falling back to live scrape"
                        )
            except Exception as _reu_exc:
                logger.warning(
                    f"[{report_id}] Observation reuse check failed (non-fatal, "
                    f"falling back to live scrape): {_reu_exc}"
                )

        if not _obs_reuse_succeeded and primary_benchmark_url:
            # Mode C: Benchmark-first — use pinned comp as primary anchor.
            # Runs for any input mode when a benchmark is pinned.
            logger.info(
                f"[{report_id}] Mode C (benchmark-first): {primary_benchmark_url}"
            )
            _progress(10, "fetching_benchmark", "Fetching benchmark listing data...")
            try:
                from worker.scraper.price_estimator import run_benchmark_scrape

                daily_results, transparent_result = run_benchmark_scrape(
                    benchmark_url=primary_benchmark_url,
                    checkin=start_date,
                    checkout=end_date,
                    cdp_url=CDP_URL,
                    max_scroll_rounds=MAX_SCROLL_ROUNDS,
                    max_cards=MAX_CARDS,
                    max_runtime_seconds=MAX_RUNTIME_SECONDS,
                    rate_limit_seconds=RATE_LIMIT_SECONDS,
                    cdp_connect_timeout_ms=CDP_CONNECT_TIMEOUT_MS,
                    target_url=listing_url,
                    secondary_benchmark_urls=secondary_benchmark_urls or None,
                    user_attributes=attributes,
                    fallback_address=address,
                    target_lat=_job_target_lat,
                    target_lng=_job_target_lng,
                    max_radius_km=_job_radius_km,
                    excluded_room_ids=excluded_room_ids or None,
                    progress_callback=_make_day_callback(15, 75, "searching_comps", "Searching comparable listings"),
                    nightly_plan=_nightly_plan_benchmark,
                )

                # Save benchmark transparency now — before any fallback overwrites transparent_result.
                _saved_benchmark_info = (transparent_result or {}).get("benchmarkInfo") or None

                valid_prices = [r["median_price"] for r in daily_results if r.get("median_price")]
                if daily_results and valid_prices:
                    result = _build_scrape_calendar(
                        daily_results, start_date, end_date, discount_policy, transparent_result,
                    )
                    if result[0] is not None and result[1] is not None:
                        summary, calendar = result
                        core_version = WORKER_VERSION + "+benchmark"
                        _mode_c_succeeded = True
                    else:
                        logger.warning(
                            f"[{report_id}] Benchmark pipeline returned no valid prices, "
                            "falling back to criteria search"
                        )
                else:
                    logger.warning(
                        f"[{report_id}] Benchmark pipeline returned empty results, "
                        "falling back to criteria search"
                    )

            except Exception as exc:
                logger.warning(
                    f"[{report_id}] Benchmark pipeline failed ({exc}), "
                    "falling back to criteria search"
                )
                transparent_result = None

        if not _obs_reuse_succeeded and not _mode_c_succeeded and listing_url:
            # Mode A: URL scrape — user provided a listing URL
            logger.info(f"[{report_id}] Mode A (URL scrape): {listing_url}")
            _progress(10, "extracting_target", "Extracting listing details...")
            try:
                from worker.scraper.price_estimator import run_scrape

                daily_results, transparent_result = run_scrape(
                    listing_url=listing_url,
                    checkin=start_date,
                    checkout=end_date,
                    cdp_url=CDP_URL,
                    max_scroll_rounds=MAX_SCROLL_ROUNDS,
                    max_cards=MAX_CARDS,
                    max_runtime_seconds=MAX_RUNTIME_SECONDS,
                    rate_limit_seconds=RATE_LIMIT_SECONDS,
                    cdp_connect_timeout_ms=CDP_CONNECT_TIMEOUT_MS,
                    preferred_comps=preferred_comps,
                    excluded_room_ids=excluded_room_ids or None,
                    target_lat=_job_target_lat,
                    target_lng=_job_target_lng,
                    max_radius_km=_job_radius_km,
                    progress_callback=_make_day_callback(15, 75, "searching_comps", "Searching comparable listings"),
                    nightly_plan=_nightly_plan,
                    # Backfill any target spec fields Airbnb failed to return
                    # (bedrooms, baths, accommodates, propertyType) from the
                    # saved listing inputs so comparable matching stays effective.
                    fallback_attributes=attributes,
                    # Last-resort location fallback when both page extraction and
                    # title-based heuristics fail to yield a search location.
                    fallback_address=address,
                )

                valid_prices = [r["median_price"] for r in daily_results if r.get("median_price")]
                if daily_results and valid_prices:
                    result = _build_scrape_calendar(
                        daily_results, start_date, end_date, discount_policy, transparent_result,
                    )
                    if result[0] is not None and result[1] is not None:
                        summary, calendar = result
                        finalized_input_attributes = _merge_extracted_specs_into_attributes(
                            finalized_input_attributes, transparent_result
                        )
                        core_version = WORKER_VERSION + "+scrape"
                    else:
                        logger.warning(
                            f"[{report_id}] URL scrape calendar build returned no valid prices; "
                            "retrying with Playwright location-search daily comps"
                        )
                        try:
                            daily_results_pw, transparent_result_pw = run_scrape(
                                listing_url=listing_url,
                                checkin=start_date,
                                checkout=end_date,
                                cdp_url=CDP_URL,
                                max_scroll_rounds=MAX_SCROLL_ROUNDS,
                                max_cards=MAX_CARDS,
                                max_runtime_seconds=MAX_RUNTIME_SECONDS,
                                rate_limit_seconds=RATE_LIMIT_SECONDS,
                                cdp_connect_timeout_ms=CDP_CONNECT_TIMEOUT_MS,
                                preferred_comps=preferred_comps,
                                target_lat=_job_target_lat,
                                target_lng=_job_target_lng,
                                max_radius_km=_job_radius_km,
                                progress_callback=_make_day_callback(15, 75, "searching_comps", "Searching comparable listings"),
                                nightly_plan=_nightly_plan,
                                fallback_attributes=attributes,
                                fallback_address=address,
                                force_playwright_daily_search=True,
                            )
                            valid_prices_pw = [r["median_price"] for r in daily_results_pw if r.get("median_price")]
                            if daily_results_pw and valid_prices_pw:
                                daily_results = daily_results_pw
                                transparent_result = transparent_result_pw
                                result_pw = _build_scrape_calendar(
                                    daily_results, start_date, end_date, discount_policy, transparent_result,
                                )
                                if result_pw[0] is not None and result_pw[1] is not None:
                                    summary, calendar = result_pw
                                    finalized_input_attributes = _merge_extracted_specs_into_attributes(
                                        finalized_input_attributes, transparent_result
                                    )
                                    core_version = WORKER_VERSION + "+scrape_playwright_retry"
                                    logger.info(
                                        f"[{report_id}] URL scrape recovered via Playwright location-search daily retry"
                                    )
                                else:
                                    logger.warning(
                                        f"[{report_id}] Playwright daily retry still returned no valid calendar; "
                                        "trying target-listing-only fallback"
                                    )
                            else:
                                logger.warning(
                                    f"[{report_id}] Playwright daily retry returned no valid prices; "
                                    "trying target-listing-only fallback"
                                )
                        except Exception as _pw_retry_exc:
                            logger.warning(
                                f"[{report_id}] Playwright daily retry failed ({_pw_retry_exc}); "
                                "trying target-listing-only fallback"
                            )
                        if summary is not None and calendar is not None:
                            pass
                        else:
                            fallback_daily = _build_target_listing_only_daily_results(
                                listing_url=listing_url,
                                start_date=start_date,
                                end_date=end_date,
                                minimum_booking_nights=minimum_booking_nights,
                                report_id=report_id,
                            )
                            fallback_valid = [r["median_price"] for r in fallback_daily if r.get("median_price")]
                            if not (fallback_daily and fallback_valid):
                                _fail(
                                    "Service is busy. Could not collect enough pricing data — please try again later.",
                                    "Both comps scrape and target-listing fallback returned no valid prices",
                                )
                                return
                            if not isinstance(transparent_result, dict):
                                transparent_result = {}
                            transparent_result.setdefault("debug", {})
                            if isinstance(transparent_result.get("debug"), dict):
                                transparent_result["debug"]["target_only_fallback"] = True
                            result = _build_scrape_calendar(
                                fallback_daily, start_date, end_date, discount_policy, transparent_result,
                            )
                            if result[0] is None or result[1] is None:
                                _fail(
                                    "Service is busy. Could not collect enough pricing data — please try again later.",
                                    "Both comps scrape and target-listing fallback returned no valid prices",
                                )
                                return
                            summary, calendar = result
                            finalized_input_attributes = _merge_extracted_specs_into_attributes(
                                finalized_input_attributes, transparent_result
                            )
                            core_version = WORKER_VERSION + "+self_only"
                else:
                    scrape_err = ((transparent_result or {}).get("debug") or {}).get("error") or "No results"
                    logger.warning(
                        f"[{report_id}] URL scrape produced no daily results ({scrape_err}); "
                        "retrying with Playwright location-search daily comps"
                    )
                    try:
                        daily_results_pw, transparent_result_pw = run_scrape(
                            listing_url=listing_url,
                            checkin=start_date,
                            checkout=end_date,
                            cdp_url=CDP_URL,
                            max_scroll_rounds=MAX_SCROLL_ROUNDS,
                            max_cards=MAX_CARDS,
                            max_runtime_seconds=MAX_RUNTIME_SECONDS,
                            rate_limit_seconds=RATE_LIMIT_SECONDS,
                            cdp_connect_timeout_ms=CDP_CONNECT_TIMEOUT_MS,
                            preferred_comps=preferred_comps,
                            target_lat=_job_target_lat,
                            target_lng=_job_target_lng,
                            max_radius_km=_job_radius_km,
                            progress_callback=_make_day_callback(15, 75, "searching_comps", "Searching comparable listings"),
                            nightly_plan=_nightly_plan,
                            fallback_attributes=attributes,
                            fallback_address=address,
                            force_playwright_daily_search=True,
                        )
                        valid_prices_pw = [r["median_price"] for r in daily_results_pw if r.get("median_price")]
                        if daily_results_pw and valid_prices_pw:
                            daily_results = daily_results_pw
                            transparent_result = transparent_result_pw
                            result_pw = _build_scrape_calendar(
                                daily_results, start_date, end_date, discount_policy, transparent_result,
                            )
                            if result_pw[0] is not None and result_pw[1] is not None:
                                summary, calendar = result_pw
                                finalized_input_attributes = _merge_extracted_specs_into_attributes(
                                    finalized_input_attributes, transparent_result
                                )
                                core_version = WORKER_VERSION + "+scrape_playwright_retry"
                                logger.info(
                                    f"[{report_id}] URL scrape recovered via Playwright location-search daily retry"
                                )
                    except Exception as _pw_retry_exc:
                        logger.warning(
                            f"[{report_id}] Playwright daily retry failed ({_pw_retry_exc}); "
                            "trying target-listing-only fallback"
                        )
                    if summary is not None and calendar is not None:
                        pass
                    else:
                        fallback_daily = _build_target_listing_only_daily_results(
                            listing_url=listing_url,
                            start_date=start_date,
                            end_date=end_date,
                            minimum_booking_nights=minimum_booking_nights,
                            report_id=report_id,
                        )
                        fallback_valid = [r["median_price"] for r in fallback_daily if r.get("median_price")]
                        if not (fallback_daily and fallback_valid):
                            _fail(
                                "Service is busy. Could not reach Airbnb data — please try again later.",
                                f"Scrape and target-listing fallback produced no valid prices: {scrape_err}",
                            )
                            return
                        if not isinstance(transparent_result, dict):
                            transparent_result = {}
                        transparent_result.setdefault("debug", {})
                        if isinstance(transparent_result.get("debug"), dict):
                            transparent_result["debug"]["target_only_fallback"] = True
                            transparent_result["debug"]["fallback_reason"] = f"no_comp_results: {scrape_err}"
                        result = _build_scrape_calendar(
                            fallback_daily, start_date, end_date, discount_policy, transparent_result,
                        )
                        if result[0] is None or result[1] is None:
                            _fail(
                                "Service is busy. Could not reach Airbnb data — please try again later.",
                                f"Scrape and target-listing fallback produced no valid prices: {scrape_err}",
                            )
                            return
                        summary, calendar = result
                        finalized_input_attributes = _merge_extracted_specs_into_attributes(
                            finalized_input_attributes, transparent_result
                        )
                        core_version = WORKER_VERSION + "+self_only"

            except ValueError as exc:
                logger.exception(f"[{report_id}] ValueError in URL mode")
                _fail(str(exc), str(exc))
                return

            except Exception as exc:
                _fail(
                    "Service is busy. An error occurred during analysis — please try again later.",
                    str(exc),
                )
                return

        elif not _obs_reuse_succeeded and not _mode_c_succeeded and input_mode in ("criteria", "criteria-by-city", "criteria-by-zip"):
            # Mode B: Criteria search — find best matching listing, then scrape comps
            logger.info(f"[{report_id}] Mode B (criteria search, mode={input_mode}): {address}")
            _progress(10, "searching_comps", "Searching for comparable listings...")
            try:
                from worker.scraper.price_estimator import run_criteria_search

                daily_results, transparent_result = run_criteria_search(
                    address=address,
                    attributes=attributes,
                    checkin=start_date,
                    checkout=end_date,
                    cdp_url=CDP_URL,
                    max_scroll_rounds=MAX_SCROLL_ROUNDS,
                    max_cards=MAX_CARDS,
                    max_runtime_seconds=MAX_RUNTIME_SECONDS,
                    rate_limit_seconds=RATE_LIMIT_SECONDS,
                    cdp_connect_timeout_ms=CDP_CONNECT_TIMEOUT_MS,
                    preferred_comps=preferred_comps,
                    excluded_room_ids=excluded_room_ids or None,
                    target_lat=_job_target_lat,
                    target_lng=_job_target_lng,
                    max_radius_km=_job_radius_km,
                    progress_callback=_make_day_callback(15, 75, "searching_comps", "Searching comparable listings"),
                    nightly_plan=_nightly_plan,
                )

                valid_prices = [r["median_price"] for r in daily_results if r.get("median_price")]
                if daily_results and valid_prices:
                    result = _build_scrape_calendar(
                        daily_results, start_date, end_date, discount_policy, transparent_result,
                    )
                    if result[0] is not None and result[1] is not None:
                        summary, calendar = result
                        core_version = WORKER_VERSION + "+criteria"
                    else:
                        _fail(
                            "Service is busy. Could not collect enough pricing data — please try again later.",
                            "All day-queries returned no valid prices in criteria mode",
                        )
                        return
                else:
                    criteria_err = ((transparent_result or {}).get("debug") or {}).get("error") or "No results"
                    _fail(
                        "Service is busy. Could not reach Airbnb data — please try again later.",
                        f"Criteria search produced no daily results: {criteria_err}",
                    )
                    return

            except ValueError as exc:
                logger.exception(f"[{report_id}] ValueError in criteria mode")
                _fail(str(exc), str(exc))
                return

            except Exception as exc:
                _fail(
                    "Service is busy. An error occurred during analysis — please try again later.",
                    str(exc),
                )
                return

        elif not _obs_reuse_succeeded and not _mode_c_succeeded:
            # No listing URL and no criteria — cannot proceed
            _fail(
                "Please provide either a listing URL or search criteria.",
                "No listing URL and input mode is not criteria",
            )
            return

        _progress(80, "pricing", "Computing final pricing estimates...")

        total_ms = round((time.time() - start_time) * 1000)

        debug = (transparent_result or {}).get("debug") or {}
        debug.update({
            "cache_hit": False,
            "cache_key": cache_key,
            "cache_bypassed_for_url_mode": bypass_precache,
            "worker_host": socket.gethostname(),
            "worker_version": WORKER_VERSION,
            "total_ms": total_ms,
        })

        # Promote nightly crawl debug from timings into the top-level debug dict
        # so it is visible in result_core_debug without digging into timingsMs.
        if _nightly_plan is not None:
            _ncd = (debug.get("timingsMs") or {}).get("nightly_crawl_debug")
            if _ncd:
                debug["nightly_crawl"] = _ncd

        # Phase 6A: merge observation reuse metadata into debug so the reuse
        # decision is fully inspectable without reading the observation tables.
        if _obs_assessment is not None:
            debug.update(_obs_assessment.to_debug_dict())

        # Enrich summary with transparency fields
        if transparent_result:
            summary["targetSpec"] = transparent_result.get("targetSpec")
            summary["queryCriteria"] = transparent_result.get("queryCriteria")
            summary["compsSummary"] = transparent_result.get("compsSummary")
            summary["priceDistribution"] = transparent_result.get("priceDistribution")
            summary["recommendedPrice"] = transparent_result.get("recommendedPrice")
            summary["comparableListings"] = transparent_result.get("comparableListings")
            # Use benchmarkInfo from transparent_result if present; otherwise use the
            # one saved before Mode C fell back (preserves benchmark data across fallbacks).
            bm_info = transparent_result.get("benchmarkInfo") or _saved_benchmark_info
            if bm_info:
                summary["benchmarkInfo"] = bm_info

        # Snapshot of excluded comp roomIds active at run time. Frontend uses
        # superset-check vs current excludedComps to decide banner wording:
        # "X hidden locally" (subset / not yet rerun) vs "Pricing excludes X
        # hidden" (superset / already rerun). Always write — even empty list —
        # so older reports without this field can be detected (undefined).
        summary["excludedRoomIdsAtRun"] = sorted(excluded_room_ids)

        # ── Canonical pricing contract: pin recommendedPrice.nightly ─────────
        # summary.recommendedPrice.nightly must equal the calendar's canonical
        # daily recommendation for the report start date (day 0).
        # This unifies dashboard, report, chart, and alert pricing surfaces so
        # they all display the same "Recommended Price" number.
        #
        # The pricing-engine similarity-weighted recommendation (if present) is
        # preserved as recommendedPrice.windowMedian for secondary context only.
        if calendar:
            _day0_rec = calendar[0].get("recommendedDailyPrice")
            if _day0_rec is not None:
                _existing_rec = summary.get("recommendedPrice") or {}
                _rec_dict = dict(_existing_rec) if isinstance(_existing_rec, dict) else {}
                # Archive pricing-engine nightly as windowMedian before overwriting
                _engine_nightly = _rec_dict.get("nightly")
                if _engine_nightly is not None:
                    _rec_dict.setdefault("windowMedian", _engine_nightly)
                # Pin nightly to the canonical day-0 demand-adjusted recommendation.
                # This value = day-0 baseDailyPrice × demandAdjustment (no time/LM discount).
                _rec_dict["nightly"] = _day0_rec
                _rec_dict.setdefault("weekdayEstimate", None)
                _rec_dict.setdefault("weekendEstimate", None)
                _rec_dict.setdefault("discountApplied", 0)
                _rec_dict.setdefault(
                    "notes",
                    "Market-based recommended daily price for report start date",
                )
                summary["recommendedPrice"] = _rec_dict

        # ── Live price capture (target listing) ──────────────────────────────
        # Attempt to read the host's current listed nightly price from Airbnb
        # for the first night of the report window.
        # Date basis: checkin = start_date, checkout = start_date + minimum_booking_nights.
        # Uses the listing's minimum stay setting so Airbnb shows a price.
        # Non-fatal: failure is logged and recorded in summary but does not
        # block the job from completing.
        logger.info(
            f"[{report_id}] [self_price_capture] gate "
            f"listing_url_present={bool(listing_url)} is_nightly={is_nightly}"
        )
        if listing_url:
            _progress(85, "capturing_live_price", "Capturing your current listing prices from Airbnb...")
            live_price_info = _capture_user_listing_prices_for_range(
                report_id=report_id,
                listing_url=listing_url,
                start_date=start_date,
                end_date=end_date,
                minimum_booking_nights=minimum_booking_nights,
            )
            _live_price_by_date = live_price_info.get("priceByDate") or {}
            for day in calendar:
                if not isinstance(day, dict):
                    continue
                _date = str(day.get("date") or "")
                _p = _live_price_by_date.get(_date)
                day["userListingPrice"] = (
                    round(float(_p))
                    if isinstance(_p, (int, float)) and _p > 0
                    else None
                )
            logger.info(
                f"[{report_id}] User-listing daily capture: "
                f"status={live_price_info.get('livePriceStatus')} "
                f"captured_days={live_price_info.get('capturedDays')}/{live_price_info.get('totalDays')}"
            )

            # Merge live price fields into summary
            summary.update(live_price_info)

            # Compute comparison intelligence when observed price is available
            _observed = live_price_info.get("observedListingPrice")
            if isinstance(_observed, (int, float)) and _observed > 0:
                _market_median = summary.get("nightlyMedian")
                _recommended = (summary.get("recommendedPrice") or {}).get("nightly")

                if isinstance(_market_median, (int, float)) and _market_median > 0:
                    _obs_vs_mkt_diff = round(_observed - _market_median)
                    _obs_vs_mkt_pct = round((_observed / _market_median - 1) * 100)
                    summary["observedVsMarketDiff"] = _obs_vs_mkt_diff
                    summary["observedVsMarketDiffPct"] = _obs_vs_mkt_pct
                    if _obs_vs_mkt_pct < -3:
                        summary["pricingPosition"] = "below_market"
                    elif _obs_vs_mkt_pct > 3:
                        summary["pricingPosition"] = "above_market"
                    else:
                        summary["pricingPosition"] = "at_market"

                if isinstance(_recommended, (int, float)) and _recommended > 0:
                    _obs_vs_rec_diff = round(_observed - _recommended)
                    _obs_vs_rec_pct = round((_observed / _recommended - 1) * 100)
                    summary["observedVsRecommendedDiff"] = _obs_vs_rec_diff
                    summary["observedVsRecommendedDiffPct"] = _obs_vs_rec_pct

                    # Pricing action: >$10 from recommendation triggers suggest
                    if _obs_vs_rec_diff > 10:
                        summary["pricingAction"] = "lower"
                        summary["pricingActionTarget"] = int(round(_recommended))
                    elif _obs_vs_rec_diff < -10:
                        summary["pricingAction"] = "raise"
                        summary["pricingActionTarget"] = int(round(_recommended))
                    else:
                        summary["pricingAction"] = "keep"
                        summary["pricingActionTarget"] = int(round(_observed))
        else:
            # No listing URL configured
            summary["livePriceStatus"] = "no_listing_url"
            summary["livePriceStatusReason"] = "No Airbnb listing URL configured for this property"

        _attach_user_listing_prices_and_log(report_id, calendar, summary)
        _progress(90, "saving_results", "Saving results...")

        # Write results
        db_helpers.complete_job(
            client, report_id, worker_token,
            summary=summary,
            calendar=calendar,
            core_version=core_version,
            debug=debug,
            input_attributes=finalized_input_attributes,
            # For nightly jobs: write all refreshed execution inputs back to the
            # report row so it fully reflects actual inputs, not queued snapshot.
            input_address=address if is_nightly else None,
            # write_input_listing_url=True allows explicit NULL-clear when no URL.
            input_listing_url=listing_url if is_nightly else None,
            write_input_listing_url=is_nightly,
            discount_policy=discount_policy if is_nightly else None,
            # Sync the recomputed execution cache key to the report row.
            cache_key=cache_key if is_nightly else None,
            queue_ml_forecast=is_nightly and bool(job.get("listing_id")) and NIGHTLY_QUEUE_ML_FORECAST,
            ml_training_scope=NIGHTLY_ML_TRAINING_SCOPE,
            ml_force_retrain=NIGHTLY_ML_FORCE_RETRAIN,
        )

        # ── Alert evaluation — nightly only (fresh-scrape path) ──────────────
        # Must run AFTER complete_job() so the report is already marked ready.
        # Non-fatal: alert failures never affect the job outcome.
        if is_nightly and job.get("listing_id"):
            try:
                from worker.alerts import run_alert_evaluation
                run_alert_evaluation(
                    job, summary, client,
                    listing_url=listing_url,
                    calendar=calendar,
                    cdp_url=CDP_URL,
                    cdp_connect_timeout_ms=CDP_CONNECT_TIMEOUT_MS,
                )
            except Exception as _alert_exc:
                logger.warning(
                    f"[{report_id}] Alert evaluation failed (non-fatal): {_alert_exc}"
                )

        # ── Observation write — nightly only (fresh-scrape path) ──────────────
        # Writes normalized per-date observations to the Phase-5A tables
        # after the pricing_reports row is already marked ready.
        # Non-fatal: observation failures never affect the job outcome.
        if is_nightly and job.get("listing_id"):
            try:
                from worker.core.observations import write_nightly_observations
                write_nightly_observations(
                    client,
                    saved_listing_id=job["listing_id"],
                    pricing_report_id=report_id,
                    captured_at=datetime.utcnow(),
                    summary=summary,
                    calendar=calendar,
                )
            except Exception as _obs_exc:
                logger.warning(
                    f"[{report_id}] Observation write failed (non-fatal): {_obs_exc}"
                )

        if listing_url:
            try:
                db_helpers.sync_linked_listing_attributes(
                    client, report_id, finalized_input_attributes
                )
            except Exception as exc:
                logger.warning(f"[{report_id}] Failed to sync linked listing attributes: {exc}")

        # Write back geocoded target coords to saved_listings (Phase 3A)
        if _geocoded_now and _listing_id_for_geocode and _job_target_lat is not None:
            try:
                client.table("saved_listings").update({
                    "target_lat": _job_target_lat,
                    "target_lng": _job_target_lng,
                }).eq("id", _listing_id_for_geocode).execute()
                logger.info(
                    f"[{report_id}] Saved geocoded coords to listing {_listing_id_for_geocode}"
                )
            except Exception as exc:
                logger.warning(f"[{report_id}] Failed to write geocoded coords (non-fatal): {exc}")

        # Phase 3B: write back page-extracted coords when the listing page gave us
        # better coordinates than geocoding (URL mode and benchmark mode).
        # _final_lat/_final_lng track the best coords known after all sources;
        # they start from geocoded/DB coords and are superseded by page coords
        # when available — this drives the timezone enrichment below.
        _final_lat: Optional[float] = _job_target_lat
        _final_lng: Optional[float] = _job_target_lng

        if _listing_id_for_geocode and transparent_result:
            _page_coords = (transparent_result or {}).get("pageExtractedCoords")
            if _page_coords and _page_coords.get("source") == "page":
                _pc_lat = _page_coords.get("lat")
                _pc_lng = _page_coords.get("lng")
                if _pc_lat is not None and _pc_lng is not None:
                    try:
                        client.table("saved_listings").update({
                            "target_lat": _pc_lat,
                            "target_lng": _pc_lng,
                        }).eq("id", _listing_id_for_geocode).execute()
                        logger.info(
                            f"[{report_id}] Saved page-extracted coords "
                            f"({_pc_lat:.5f}, {_pc_lng:.5f}) to listing {_listing_id_for_geocode}"
                        )
                        # Page coords are more trustworthy — use them as final coords
                        _final_lat = _pc_lat
                        _final_lng = _pc_lng
                    except Exception as exc:
                        logger.warning(
                            f"[{report_id}] Failed to write page-extracted coords (non-fatal): {exc}"
                        )

        # Write back listing timezone using the best coords known this run.
        # Runs after all coord sources (geocoded AND page-extracted) so that
        # page-extracted coordinates also trigger same-run timezone persistence.
        # Does not overwrite an already-stored timezone.
        if (
            _listing_id_for_geocode
            and _final_lat is not None
            and _final_lng is not None
            and not _db_timezone
        ):
            try:
                from timezonefinder import TimezoneFinder
                _tf = TimezoneFinder()
                _resolved_tz = _tf.timezone_at(lat=_final_lat, lng=_final_lng)
                if _resolved_tz:
                    client.table("saved_listings").update({
                        "listing_timezone": _resolved_tz,
                    }).eq("id", _listing_id_for_geocode).execute()
                    logger.info(
                        f"[{report_id}] Saved listing_timezone={_resolved_tz} "
                        f"to listing {_listing_id_for_geocode} "
                        f"(coords source: {'page' if _final_lat != _job_target_lat else 'geocode/db'})"
                    )
            except Exception as exc:
                logger.warning(f"[{report_id}] Failed to resolve/write listing timezone (non-fatal): {exc}")

        # Phase 3B: write back the adaptive radius used this run
        if _listing_id_for_geocode:
            try:
                client.table("saved_listings").update({
                    "comp_pool_target_radius_km": _job_radius_km,
                }).eq("id", _listing_id_for_geocode).execute()
            except Exception as exc:
                logger.warning(
                    f"[{report_id}] Failed to write target radius (non-fatal): {exc}"
                )

        # Seed comparable pool (Phase 2) — non-fatal, runs after job is marked ready
        _listing_id = job.get("listing_id")
        if _listing_id and transparent_result:
            try:
                from worker.core.pool_seeding import seed_pool_from_report
                seed_pool_from_report(
                    client,
                    saved_listing_id=_listing_id,
                    comparable_listings=transparent_result.get("comparableListings") or [],
                )
            except Exception as exc:
                logger.warning(f"[{report_id}] Pool seeding failed (non-fatal): {exc}")

        # Store in cache (enriched summary includes transparency)
        try:
            source = debug.get("source", "unknown")
            comps_count = 0
            if transparent_result:
                comps_count = (transparent_result.get("compsSummary") or {}).get("collected", 0)
            meta = {
                "source": source,
                "listing_url": listing_url or "",
                "comps_count": comps_count,
            }
            # Strip live-price fields before caching — they are time-sensitive
            # and must be re-captured fresh on every job run (including cache hits).
            _LIVE_PRICE_KEYS = {
                "observedListingPrice", "observedListingPriceDate",
                "observedListingPriceCapturedAt", "observedListingPriceSource",
                "observedListingPriceConfidence", "observedVsMarketDiff",
                "observedVsMarketDiffPct", "observedVsRecommendedDiff",
                "observedVsRecommendedDiffPct", "pricingPosition",
                "pricingAction", "pricingActionTarget",
                "livePriceStatus", "livePriceStatusReason",
                "priceByDate", "capturedDays", "totalDays",
            }
            _cache_safe_summary = {k: v for k, v in summary.items() if k not in _LIVE_PRICE_KEYS}
            set_cached(client, cache_key, _cache_safe_summary, calendar, meta=meta)
        except Exception as exc:
            logger.warning(f"[{report_id}] Failed to write cache: {exc}")

        logger.info(f"[{report_id}] Completed in {total_ms}ms ({core_version})")

    except Exception as exc:
        elapsed_ms = round((time.time() - start_time) * 1000)
        error_msg = f"Processing failed: {str(exc)[:200]}"
        logger.error(f"[{report_id}] {error_msg}")
        is_mem_pressure = _is_windows_paging_file_error(exc)

        try:
            user_error = (
                "Worker host is under memory pressure (Windows virtual memory exhausted). "
                "Please increase page file size and retry."
                if is_mem_pressure
                else "We encountered an issue processing your report. Please try again."
            )
            debug_error = "host_memory_pressure_winerror_1455" if is_mem_pressure else str(exc)
            db_helpers.fail_job(
                client, report_id, worker_token,
                error_message=user_error,
                debug={
                    "error": debug_error,
                    "worker_host": socket.gethostname(),
                    "worker_version": WORKER_VERSION,
                    "total_ms": elapsed_ms,
                },
            )
        except Exception as db_exc:
            logger.error(f"[{report_id}] Failed to mark job as error: {db_exc}")

    finally:
        hb_stop.set()
        hb_thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Main worker loop
# ---------------------------------------------------------------------------


def main():
    logger.info(f"AriaHost Worker starting (version={WORKER_VERSION})")
    logger.info(f"  env={WORKER_ENV}, lane={WORKER_LANE}, poll={POLL_SECONDS}s, stale={STALE_MINUTES}min, max_attempts={MAX_ATTEMPTS}")
    logger.info(f"  heartbeat={HEARTBEAT_SECONDS}s, max_runtime={MAX_RUNTIME_SECONDS}s")
    logger.info(f"  CDP={CDP_URL}, connect_timeout={CDP_CONNECT_TIMEOUT_MS}ms")
    logger.info(
        f"  day_query_workers={DAY_QUERY_MAX_WORKERS}, "
        f"benchmark_day_query_workers={BENCHMARK_DAY_QUERY_MAX_WORKERS}, "
        f"fixed_pool_workers={FIXED_POOL_MAX_WORKERS}, "
        f"scrape_rate_limit={RATE_LIMIT_SECONDS}s"
    )
    logger.info(
        f"disable_map_search={AIRBNB_DISABLE_MAP_SEARCH}, "
        f"enable_ai_search={AIRBNB_ENABLE_AI_SEARCH}"
    )
    logger.info(
        f"  auto_apply: stale={AUTO_APPLY_STALE_MINUTES}min, "
        f"max_attempts={AUTO_APPLY_MAX_ATTEMPTS}, cdp={AUTO_APPLY_CDP_URL}"
    )

    client = db_helpers.get_client()
    backoff = POLL_SECONDS
    max_backoff = POLL_SECONDS * 12  # 60s at default

    while not _shutdown_event.is_set():
        try:
            worker_token = uuid.uuid4()
            job = db_helpers.claim_job(client, worker_token, STALE_MINUTES, WORKER_ENV, WORKER_LANE)

            if job is None:
                # No pricing_reports work for this lane/env. Try auto-apply queue.
                auto_job = db_helpers.claim_price_update_job(
                    client, worker_token, AUTO_APPLY_STALE_MINUTES
                )
                if auto_job is None:
                    # No work — wait with current backoff
                    _shutdown_event.wait(backoff)
                    backoff = min(backoff * 1.5, max_backoff)
                    continue

                # Got auto-apply work — reset backoff and process.
                backoff = POLL_SECONDS
                logger.info(f"[{auto_job['id']}] Claimed auto-apply price update job")
                process_price_update_job(auto_job, worker_token, client)
                continue

            # Got work — reset backoff
            backoff = POLL_SECONDS
            report_id = job["id"]
            attempts = job.get("worker_attempts", 0)
            logger.info(
                f"[{report_id}] claimed "
                f"(job_lane={job.get('job_lane', '?')}, target_env={job.get('target_env', '?')}, "
                f"worker_env={WORKER_ENV}, worker_lane={WORKER_LANE}, attempt={attempts})"
            )
            if str(job.get("job_lane") or "").strip().lower() == "nightly":
                claimed_at_raw = str(job.get("worker_claimed_at") or "").strip()
                claim_date = (
                    db_helpers.utc_date_from_iso(claimed_at_raw)
                    or datetime.now(timezone.utc).date().isoformat()
                )
                try:
                    queued_nightly = db_helpers.list_queued_nightly_jobs(client, WORKER_ENV)
                    logger.info(
                        f"[{report_id}] nightly queue snapshot: queued_count={len(queued_nightly)} claim_date={claim_date}"
                    )
                    older_start_rows = []
                    for row in queued_nightly:
                        start_date = str(row.get("input_date_start") or "").strip()
                        if start_date and start_date < claim_date:
                            older_start_rows.append(row)
                    if older_start_rows:
                        logger.warning(
                            f"[{report_id}] queued nightly tasks with input_date_start earlier than claim_date={claim_date}: "
                            f"count={len(older_start_rows)}"
                        )
                        for row in older_start_rows:
                            logger.warning(
                                "[nightly_queued_older_start] id=%s input_date_start=%s created_at=%s worker_claimed_at=%s target_env=%s",
                                row.get("id"),
                                row.get("input_date_start"),
                                row.get("created_at"),
                                row.get("worker_claimed_at"),
                                row.get("target_env"),
                            )
                except Exception as nightly_diag_exc:
                    logger.warning(f"[{report_id}] nightly queue diagnostics failed: {nightly_diag_exc}")

            if attempts > MAX_ATTEMPTS:
                logger.warning(f"[{report_id}] Exceeded max attempts ({attempts}), marking error")
                db_helpers.fail_job(
                    client, report_id, worker_token,
                    error_message="This report failed after multiple attempts. Please create a new one.",
                    debug={
                        "error": f"Exceeded max attempts ({attempts})",
                        "worker_host": socket.gethostname(),
                        "worker_version": WORKER_VERSION,
                    },
                )
                continue

            logger.info(f"Claimed job {report_id} (attempt {attempts})")
            process_job(job, worker_token)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.error(f"Worker loop error: {exc}")
            _shutdown_event.wait(backoff)
            backoff = min(backoff * 2, max_backoff)

    logger.info("Worker shut down.")


if __name__ == "__main__":
    main()
