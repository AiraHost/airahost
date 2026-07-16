"""
Benchmark-first pricing engine.

When a user provides a preferred comparable (pinned comp) the system uses
it as the primary benchmark rather than as a similarity-score boost.

Two-stage model
---------------
Stage 1 — Benchmark anchor
  Fetch the benchmark listing's own nightly price for each sample day.
  Primary strategy: navigate to the benchmark listing page directly with dates
  and read the booking widget's discounted/current nightly price.
  Fallback strategy: use the 1-night search-result card price only if direct-page
  extraction fails.

Stage 2 — Market validation / adjustment
  Collect a smaller set of other market comps from the same search.
  Compute the raw market offset vs the benchmark price, cap it at
  ±BENCHMARK_MAX_ADJ, then apply only BENCHMARK_MARKET_WEIGHT of the
  offset so the benchmark price stays dominant.

  final_price = benchmark_price × (1 + capped_adj × MARKET_WEIGHT)

Fast-path settings
------------------
Benchmark mode requests fewer scroll rounds, fewer cards, and fewer
sample days than the standard pipeline, making it faster overall.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from worker.core.comp_utils import (
    build_comp_id,
    build_comp_prices_dict,
    compute_price_distribution,
    to_comparable_payload,
)
from worker.core.geo_filter import DEFAULT_MAX_RADIUS_KM, apply_geo_filter
from worker.core.price_band import apply_price_band_filter
from worker.core.price_sanity import apply_price_sanity
from worker.core.similarity import (
    comp_urls_match,
    filter_similar_candidates,
    similarity_score,
)
from worker.scraper.comp_collection import (
    _compute_bbox_from_radius_km,
    _map_search_row_to_spec,
    collect_search_comps,
)
from worker.scraper.parsers import (
    parse_pdp_response,
    parse_search_listing_context,
)
from worker.scraper.price_normalizer import nightly_price_from_parsed_pdp
from worker.scraper.target_extractor import ListingSpec, extract_listing_id_from_url, safe_domain_base

logger = logging.getLogger("worker.core.benchmark")

# ── Tuning constants ─────────────────────────────────────────────────────────

# Fast-path scraping limits (less than standard day-query defaults)
BENCHMARK_SCROLL_ROUNDS: int = 1     # standard: DAY_SCROLL_ROUNDS = 2
BENCHMARK_MAX_CARDS: int = 15        # standard: DAY_MAX_CARDS = 30
BENCHMARK_TOP_K: int = 5             # standard: top_k = 10
BENCHMARK_MAX_SAMPLE_QUERIES: int = 10  # standard: MAX_SAMPLE_QUERIES = 20
MAP_RADIUS_CAP_KM: float = 5.0 * 1.609344  # exactly 5 miles

# Micro map-search radius around the benchmark's own coordinates.
# Airbnb publishes fuzzed listing coords (up to ~150m off), so the box must be
# wide enough to still contain the pin while staying small enough that the
# benchmark card is guaranteed onto the first results page.
MICRO_SEARCH_RADIUS_KM: float = 0.25

# Benchmark mode needs ~5 priced market comps for a reliable median
# (_MIN_COMPS_FOR_FULL_WEIGHT below); stop paging once we have a few spare
# instead of sweeping deep offsets toward a full 15-card page.
BENCHMARK_MIN_PRICED_TARGET: int = 8

# Pricing formula weights
BENCHMARK_MARKET_WEIGHT: float = 0.30   # 30 % weight to market adjustment (high-confidence baseline)
BENCHMARK_MAX_ADJ: float = 0.25         # cap raw market offset at ±25 %

# Confidence-adjusted market weight:
# Lower benchmark confidence → market has more pull (benchmark trusted less)
_CONFIDENCE_EFFECTIVE_WEIGHTS: Dict[str, float] = {
    "high":   BENCHMARK_MARKET_WEIGHT,   # 0.30 — search card price, most reliable
    "medium": 0.42,                       # direct-page L2 DOM extraction
    "low":    0.55,                       # direct-page L3 body-text regex, least reliable
    "failed": BENCHMARK_MARKET_WEIGHT,    # unused — falls through to pure-market path
}

# Below this many market comps the market median is unreliable;
# scale effective weight down proportionally.
_MIN_COMPS_FOR_FULL_WEIGHT: int = 5

# Market-vs-benchmark gap (fraction) that triggers the outlier guard.
# When exceeded, effective weight is halved and "benchmark_outlier" is flagged.
BENCHMARK_OUTLIER_THRESHOLD: float = 0.40  # 40 %

# Benchmark-to-target structural similarity thresholds and weight factors.
# Computed once per job (benchmark spec vs user's property attributes).
# Passed into each day query so the per-day effective_weight is reduced when
# the benchmark listing is a poor structural match for the target property.
_BM_SIMILARITY_HIGH_MATCH: float = 0.70      # ≥ this → no penalty
_BM_SIMILARITY_STRONG_MISMATCH: float = 0.45 # < this → strong penalty
_BM_MISMATCH_MODERATE_FACTOR: float = 0.85   # ×0.85 when 0.45 ≤ similarity < 0.70
_BM_MISMATCH_STRONG_FACTOR: float = 0.65     # ×0.65 when similarity < 0.45

# Per-day secondary consensus → effective_weight multipliers.
# "support" → secondary comps cluster near benchmark → market adjusts less.
# "oppose"  → secondary comps cluster near market   → market adjusts more.
# Intentionally small range so secondary comps cannot override primary signals.
_SECONDARY_SIGNAL_MULTIPLIERS: Dict[str, float] = {
    "support": 0.90,   # secondary comps agree with benchmark → trust benchmark more
    "neutral": 1.00,   # no clear signal (or no secondary data) → no change
    "oppose":  1.15,   # secondary comps agree with market    → trust market more
}

# A secondary comp's mean price must be within this fraction of the reference
# price (benchmark or market) to be classified as "supporting" or "opposing".
_SECONDARY_CONSENSUS_THRESHOLD: float = 0.20  # 20 %

# Fetch status codes
FETCH_STATUS_SEARCH_HIT = "search_hit"       # benchmark appeared in search results
FETCH_STATUS_DIRECT_PAGE = "direct_page"     # obtained via listing-page scrape
FETCH_STATUS_FAILED = "failed"               # price unavailable for this day


def _resolve_query_center(target: ListingSpec) -> Tuple[Optional[float], Optional[float]]:
    if isinstance(target.lat, (int, float)) and isinstance(target.lng, (int, float)):
        return float(target.lat), float(target.lng)
    if not str(target.location or "").strip():
        return None, None
    try:
        from worker.core.geocode_details import geocode_address_details

        result = geocode_address_details(str(target.location), timeout=3)
        if result and result.get("lat") is not None and result.get("lng") is not None:
            return float(result["lat"]), float(result["lng"])
    except Exception:
        pass
    return None, None



# ── Per-day result ────────────────────────────────────────────────────────────

@dataclass
class BenchmarkDayResult:
    """Day result produced by the benchmark-first pipeline."""

    date: str                                        # "YYYY-MM-DD"
    median_price: Optional[float] = None             # final blended price
    benchmark_price: Optional[float] = None          # Stage-1 anchor price
    market_price: Optional[float] = None             # Stage-2 market median
    market_adj_pct: Optional[float] = None           # raw offset % (market vs benchmark)
    applied_adj_pct: Optional[float] = None          # actual % applied after cap+weight
    benchmark_fetch_status: str = FETCH_STATUS_FAILED
    fetch_confidence: str = "failed"                 # "high"|"medium"|"low"|"failed"
    effective_weight: float = 0.0                    # actual market weight applied in blend
    outlier_action: str = ""                         # "lean_benchmark"|"lean_market"|"conservative"|"minimal"|""
    secondary_signal: str = "neutral"               # per-day: "support"|"neutral"|"oppose"
    secondary_comp_prices: Dict[str, Optional[float]] = field(default_factory=dict)  # {url: price|None}
    comp_prices: Dict[str, float] = field(default_factory=dict)  # room_id -> nightly_price (all comps)
    comps_collected: int = 0
    comps_used: int = 0
    filter_stage: str = ""
    flags: List[str] = field(default_factory=list)
    is_sampled: bool = True
    is_weekend: bool = False
    price_distribution: Dict[str, Any] = field(default_factory=dict)
    top_comps: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    benchmark_title: Optional[str] = None            # real listing name (user-saved or PDP/search-extracted)


# ── Benchmark Discount Probing (Strategy B) ───────────────────────────────────

def _calculate_discount_pct(base_val: float, discounted_val: float) -> float:
    if base_val <= 0:
        return 0.0
    # If discounted is actually higher (e.g. weekend premium on the probe dates), no discount
    if discounted_val >= base_val:
        return 0.0
    return round(((base_val - discounted_val) / base_val) * 100, 1)


_PDP_UNAVAILABLE_MARKERS = (
    "those dates are not available",
    "dates are not available",
    "date_not_available",
    "not available for these dates",
)


def _pdp_payload_dates_unavailable(data: Any) -> bool:
    """True when a PDP payload explicitly reports the dates as not bookable.

    Mirrors PlaywrightScraper._pdp_dates_unavailable — lets the benchmark
    pipeline distinguish "listing exists but isn't bookable" from a fetch
    failure, so the report can state the real fallback reason.
    """
    if not data:
        return False
    try:
        import json as _json
        payload_text = _json.dumps(data, ensure_ascii=False).lower()
    except Exception:
        payload_text = str(data or "").lower()
    return any(m in payload_text for m in _PDP_UNAVAILABLE_MARKERS)


def _fetch_pdp_payload(client, listing_id: str, checkin: str, checkout: str) -> Dict[str, Any]:
    """
    Fetch a PDP payload for benchmark pricing via direct HTTP only.

    Browser PDP navigation costs 10-15s per attempt (and is frequently
    misclassified as challenged); in the benchmark hot path a failed direct
    fetch is treated as price-unavailable so the day can be interpolated
    instead of paying for a browser round trip.
    """
    direct = getattr(client, "fetch_pdp_price_direct", None)
    if callable(direct):
        try:
            data = direct(str(listing_id), checkin=checkin, checkout=checkout, adults=1)
        except Exception as exc:
            logger.info(f"[benchmark] direct PDP fetch failed for {listing_id}: {exc}")
            return {}
        return data if isinstance(data, dict) else {}
    # Clients without a direct fetcher (tests/fakes) keep the legacy path.
    return client.get_listing_details(listing_id, checkin=checkin, checkout=checkout, adults=1)


def fetch_benchmark_price_via_micro_search(
    client,
    benchmark_url: str,
    bm_lat: Optional[float],
    bm_lng: Optional[float],
    date_i: date,
    *,
    query_nights_sequence: Tuple[int, ...] = (1, 2),
    adults: int = 1,
    search_location: str = "",
) -> Tuple[Optional[float], Optional[str], int]:
    """
    Fetch the benchmark listing's own nightly price from a stays-search zoomed
    to a tiny map box around the benchmark's coordinates.

    StaysSearch replays over direct HTTP are rarely challenged (unlike PDP
    fetches), so this is the fastest reliable per-day benchmark price source:
    one search request returns the benchmark's card with its discounted price.
    A 1-night query misses listings with a minimum stay, so a 2-night query is
    tried next (card totals are normalised back to per-night).

    Returns (nightly_price, card_title, query_nights_used); (None, None, 0)
    when the benchmark did not surface with a usable price.
    """
    bm_id = extract_listing_id_from_url(benchmark_url)
    if not bm_id or bm_lat is None or bm_lng is None:
        return None, None, 0
    ne_lat, ne_lng, sw_lat, sw_lng = _compute_bbox_from_radius_km(
        float(bm_lat), float(bm_lng), MICRO_SEARCH_RADIUS_KM
    )
    checkin_str = date_i.isoformat()
    for nights in query_nights_sequence:
        overrides: Dict[str, Any] = {
            "checkin": checkin_str,
            "checkout": (date_i + timedelta(days=int(nights))).isoformat(),
            "adults": int(adults),
            "dailySearch": True,
            "itemsPerGrid": 10,
            "guestFavorite": False,
            "searchByMap": True,
            "neLat": ne_lat,
            "neLng": ne_lng,
            "swLat": sw_lat,
            "swLng": sw_lng,
            "centerLat": float(bm_lat),
            "centerLng": float(bm_lng),
        }
        if str(search_location or "").strip():
            overrides["query"] = search_location
        # Direct HTTP only: an empty page here just means the benchmark is not
        # bookable for this window — a browser fallback would burn 10-30s to
        # learn the same thing.
        try:
            direct_only = getattr(client, "search_listings_direct_only", None)
            if callable(direct_only):
                result = direct_only(overrides)
                if result is None:
                    logger.info(
                        f"[benchmark] {checkin_str}: micro-search ({nights}n) "
                        f"direct replay unavailable"
                    )
                    continue
                status, data = result
            else:
                status, data = client.search_listings_with_overrides(overrides)
        except Exception as exc:
            logger.info(
                f"[benchmark] {checkin_str}: micro-search ({nights}n) failed: {exc}"
            )
            continue
        if status < 200 or status >= 300:
            continue
        row = parse_search_listing_context(data).get(str(bm_id))
        if not isinstance(row, dict):
            logger.info(
                f"[benchmark] {checkin_str}: micro-search ({nights}n) did not "
                f"surface benchmark {bm_id}"
            )
            continue
        min_nights = row.get("min_nights")
        if isinstance(min_nights, int) and min_nights > int(nights):
            continue
        # Multi-night cards sometimes omit the "for N nights" qualifier, so the
        # search parser records the trip total as a 1-night nightly price
        # (nightly == total, price_nights == 1). Restore per-night before mapping.
        _row_nightly = row.get("nightly_price")
        _row_total = row.get("total_price")
        _row_price_nights = int(row.get("price_nights") or 1)
        if (
            int(nights) > 1
            and isinstance(_row_total, (int, float))
            and _row_total > 0
            and _row_price_nights <= 1
            and (
                not isinstance(_row_nightly, (int, float))
                or float(_row_nightly) == float(_row_total)
            )
        ):
            row = dict(row)
            row["nightly_price"] = round(float(_row_total) / int(nights), 2)
            row["price_nights"] = int(nights)
        spec = _map_search_row_to_spec(
            str(bm_id), row, safe_domain_base(benchmark_url), int(nights)
        )
        if spec.nightly_price and spec.nightly_price > 0:
            title = (spec.title or "").strip() or None
            logger.info(
                f"[benchmark] {checkin_str}: micro-search hit "
                f"(nights={nights}, nightly=${spec.nightly_price:.2f})"
            )
            return round(float(spec.nightly_price), 2), title, int(nights)
    return None, None, 0


# Stay windows tried when discovering how the benchmark can be booked.
# Long-stay-only listings (e.g. 30-night-minimum furnished rentals) never
# surface in 1-2 night searches, PDP price replays, or browser DOM reads —
# a long window is the only way to obtain their real nightly revenue rate.
BENCHMARK_STAY_DISCOVERY_NIGHTS: Tuple[int, ...] = (1, 2, 3, 7, 30)


def discover_benchmark_stay_nights(
    client,
    benchmark_url: str,
    bm_lat: Optional[float],
    bm_lng: Optional[float],
    date_i: date,
    *,
    search_location: str = "",
) -> Optional[int]:
    """
    Find the shortest stay window for which the benchmark appears (with a
    price) in a micro map-search — once per job, ~1s per attempted window.

    Returns the night count to use for per-day benchmark queries, or None
    when the benchmark could not be surfaced (caller falls back to the
    default (1, 2) per-day sequence).
    """
    if bm_lat is None or bm_lng is None:
        return None
    for nights in BENCHMARK_STAY_DISCOVERY_NIGHTS:
        price, _title, used = fetch_benchmark_price_via_micro_search(
            client,
            benchmark_url,
            bm_lat,
            bm_lng,
            date_i,
            query_nights_sequence=(nights,),
            search_location=search_location,
        )
        if price is not None and used:
            logger.info(
                f"[benchmark] stay-window discovery: benchmark bookable at "
                f"{used} night(s) (nightly=${price:.2f})"
            )
            return used
    logger.info(
        "[benchmark] stay-window discovery: benchmark not found in any "
        f"window {BENCHMARK_STAY_DISCOVERY_NIGHTS}"
    )
    return None


def _extract_benchmark_price_with_min_stay_fallback(
    client,
    benchmark_url: str,
    checkin: str,
    checkout: str,
) -> Tuple[Optional[float], str, Optional[str]]:
    """
    Extract benchmark listing-page nightly price with a minimum-stay fallback.

    First try the requested stay window. If it is a 1-night stay and the listing
    does not expose a price, retry as a 2-night stay starting on the same date.
    The underlying extractor returns a nightly price, so the fallback remains
    per-night rather than a total trip price.

    Returns (price, confidence, title) — title is the listing's real name from
    the PDP payload (already parsed for the price; surfaced so the benchmark
    can be displayed by name instead of a placeholder), or None.
    On failure, confidence is "unavailable" when the PDP payload explicitly
    reported the dates as not bookable, otherwise "failed".
    """
    listing_id = extract_listing_id_from_url(benchmark_url)
    if not listing_id:
        return None, "failed", None
    pdp = _fetch_pdp_payload(client, listing_id, checkin, checkout)
    parsed = parse_pdp_response(pdp, listing_id, safe_domain_base(benchmark_url))
    title = str(parsed.get("title") or "").strip() or None
    dates_unavailable = _pdp_payload_dates_unavailable(pdp)
    requested_nights = max(1, (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days)
    price = nightly_price_from_parsed_pdp(
        parsed,
        stay_nights=requested_nights,
        source="pdp",
    )
    if price is not None:
        return price, "high", title

    if requested_nights != 1:
        return None, ("unavailable" if dates_unavailable else "failed"), title

    fallback_checkout = (date.fromisoformat(checkin) + timedelta(days=2)).isoformat()
    pdp_fb = _fetch_pdp_payload(client, listing_id, checkin, fallback_checkout)
    parsed_fb = parse_pdp_response(pdp_fb, listing_id, safe_domain_base(benchmark_url))
    title = title or (str(parsed_fb.get("title") or "").strip() or None)
    dates_unavailable = dates_unavailable or _pdp_payload_dates_unavailable(pdp_fb)
    fallback_price = nightly_price_from_parsed_pdp(
        parsed_fb,
        stay_nights=2,
        source="pdp",
    )
    if fallback_price is not None:
        logger.info(
            f"[benchmark] {checkin}: direct page 1-night unavailable, "
            f"used 2-night minimum-stay fallback (nightly=${fallback_price:.2f})"
        )
        return fallback_price, "high", title

    return None, ("unavailable" if dates_unavailable else "failed"), title

def probe_benchmark_discounts(
    client,
    benchmark_url: str,
    base_origin: str,
    start_date: date,
    *,
    bm_lat: Optional[float] = None,
    bm_lng: Optional[float] = None,
    search_location: str = "",
    bm_stay_nights: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Probes the benchmark listing for the weekly discount (1-night base price
    vs 7-night effective nightly price).

    Both probes go through the micro map-search first (direct-HTTP stays-search
    around the benchmark's coordinates — rarely challenged, ~1s each) with a
    direct-HTTP PDP replay as fallback. No browser navigation: the old
    listing-page probe cost ~30s per job when the PDP fetch was challenged.
    """
    results = {
        "weeklyDiscountPct": 0.0,
        "monthlyDiscountPct": 0.0,
        "lastMinuteDiscountPct": None,
        "details": {}
    }

    # A min-stay beyond a week means there is no 1-night base price and the
    # 7-night window is unbookable — the weekly-discount probe cannot apply.
    if bm_stay_nights and int(bm_stay_nights) > 7:
        logger.info(
            f"[benchmark-probe] skipped: benchmark min stay is "
            f"{bm_stay_nights} nights (no weekly-discount signal)"
        )
        return results

    checkin_iso = start_date.isoformat()

    def _fetch(in_date: str, out_date: str) -> Optional[float]:
        nights = max(1, (date.fromisoformat(out_date) - date.fromisoformat(in_date)).days)
        if bm_lat is not None and bm_lng is not None:
            p, _title, _n = fetch_benchmark_price_via_micro_search(
                client,
                benchmark_url,
                bm_lat,
                bm_lng,
                date.fromisoformat(in_date),
                query_nights_sequence=(nights,) if nights > 1 else (1, 2),
                search_location=search_location,
            )
            if p is not None:
                return p
        p, _conf, _title = _extract_benchmark_price_with_min_stay_fallback(
            client, benchmark_url, in_date, out_date
        )
        return p

    # 1. Base reference (1 night)
    d1_out = (start_date + timedelta(days=1)).isoformat()
    base_price = _fetch(checkin_iso, d1_out)

    if not base_price:
        logger.warning("[benchmark-probe] Could not fetch base 1-night price, skipping probes.")
        return results

    # 2. Weekly probe (7 nights) — most commonly configured discount
    d7_out = (start_date + timedelta(days=7)).isoformat()
    weekly_price = _fetch(checkin_iso, d7_out)

    if weekly_price:
        results["weeklyDiscountPct"] = _calculate_discount_pct(base_price, weekly_price)
        results["details"]["weekly"] = {"base": base_price, "effective": weekly_price}

    logger.info(
        f"[benchmark-probe] Base=${base_price}, "
        f"Weekly=${weekly_price} (-{results['weeklyDiscountPct']}%)"
    )
    return results

# ── Single-day benchmark query ────────────────────────────────────────────────

def estimate_benchmark_price_for_date(
    client,
    target: ListingSpec,
    benchmark_url: str,
    base_origin: str,
    date_i: date,
    adults: int,
    *,
    secondary_benchmark_urls: Optional[List[str]] = None,
    benchmark_target_similarity: float = 1.0,
    max_scroll_rounds: int = BENCHMARK_SCROLL_ROUNDS,
    max_cards: int = BENCHMARK_MAX_CARDS,
    rate_limit_seconds: float = 1.0,
    top_k: int = BENCHMARK_TOP_K,
    max_radius_km: float = DEFAULT_MAX_RADIUS_KM,
    excluded_room_ids: Optional[set[str]] = None,
    benchmark_name: Optional[str] = None,
    benchmark_lat: Optional[float] = None,
    benchmark_lng: Optional[float] = None,
    benchmark_stay_nights: Optional[int] = None,
) -> BenchmarkDayResult:
    """
    Execute a search-first benchmark query for *date_i*.

    1. Run a 1-night-primary Airbnb search (fast-path: fewer rounds/cards) to
       collect market comps and locate the benchmark card if present.
       Falls back to a 2-night search only when the 1-night search returns
       zero priced comps.
    2. Benchmark anchor price, cheapest source first — all direct HTTP, never
       a browser navigation:
         a. the benchmark's own card in the market search results;
         b. a micro map-search zoomed to the benchmark's coordinates
            (guarantees capture regardless of market-search ranking/filters);
         c. a direct-HTTP PDP replay (1-night with 2-night min-stay fallback).
    3. Remaining market comps → compute market adjustment.
    4. Return blended final price.
    """

    checkin_str = date_i.isoformat()
    is_weekend = date_i.weekday() >= 4  # Fri=4, Sat=5

    try:
        query_center_lat, query_center_lng = _resolve_query_center(target)
        map_radius_limit_km = MAP_RADIUS_CAP_KM
        if isinstance(max_radius_km, (int, float)) and float(max_radius_km) > 0:
            map_radius_limit_km = min(float(max_radius_km), MAP_RADIUS_CAP_KM)

        # 2-night-primary / 1-night-fallback search with coord extraction.
        # exclude_url is intentionally None: the benchmark listing must remain
        # in the results so Stage 1 can capture its search-card price as a
        # fallback when direct-page extraction fails.
        comps, query_nights_used = collect_search_comps(
            client,
            target.location,
            base_origin,
            date_i,
            adults,
            max_scroll_rounds=max_scroll_rounds,
            max_cards=max_cards,
            rate_limit_seconds=rate_limit_seconds,
            log_prefix="benchmark",
            target_accommodates=target.accommodates,
            target_beds=target.beds,
            target_baths=target.baths,
            center_lat=query_center_lat,
            center_lng=query_center_lng,
            map_radius_km=(
                map_radius_limit_km
                if query_center_lat is not None and query_center_lng is not None
                else None
            ),
            min_priced_target=BENCHMARK_MIN_PRICED_TARGET,
            enforce_exact_capacity=False,
        )

        # ── Geographic distance filter ────────────────────────────────────
        # Applied before Stage 1.  The benchmark card is excluded from
        # market_comps downstream, so filtering it here is safe.
        # Comps without coords always pass through (never blocked on missing data).
        if target.lat is not None and target.lng is not None:
            try:
                comps, _ = apply_geo_filter(
                    comps, target.lat, target.lng, max_radius_km
                )
            except Exception as _bm_geo_exc:
                logger.warning(
                    f"[benchmark] {checkin_str}: geo filter failed (non-fatal): {_bm_geo_exc}"
                )

        # ── User blacklist filter (per-listing excludedComps) ─────────────
        # Drop user-excluded comps before all_comp_prices and market_comps so
        # transparent priceByDate, market_median, and final_price all stay
        # consistent. Primary benchmark is guarded by the API cross-field check
        # — it cannot also appear in excluded_room_ids.
        if excluded_room_ids:
            pre = len(comps)
            comps = [
                c for c in comps
                if build_comp_id(c.url or "") not in excluded_room_ids
            ]
            dropped = pre - len(comps)
            if dropped > 0:
                logger.info(
                    f"[benchmark] {checkin_str}: excluded_by_user={dropped} "
                    f"(remaining {len(comps)})"
                )

        comps_collected = len(comps)

        # Build full comp_prices map for priceByDate tracking.
        all_comp_prices = build_comp_prices_dict(comps)

        # ── Stage 1: locate benchmark in search results ───────────────────
        benchmark_comp = next(
            (c for c in comps if c.url and comp_urls_match(c.url, benchmark_url)),
            None,
        )
        benchmark_price: Optional[float] = None
        benchmark_fetch_status = FETCH_STATUS_FAILED
        fetch_confidence = "failed"
        direct_page_title: Optional[str] = None
        micro_search_title: Optional[str] = None
        _bm_checkout_str = (date_i + timedelta(days=1)).isoformat()

        # Cheapest source first — search-card prices carry the same discounted
        # rate the card shows a guest, and stays-search over direct HTTP is
        # rarely challenged, unlike PDP fetches (whose browser fallback costs
        # 10-15s per attempt and dominated benchmark report time).
        if benchmark_comp and benchmark_comp.nightly_price:
            benchmark_price = benchmark_comp.nightly_price
            # Per-night normalization: parse_card_to_spec divides by scrape_nights only
            # when price_kind is "trip_total_*" AND price_nights > 1.
            # Detection gap: JS correctly identifies the card as a trip_total but reads
            # the wrong night count (e.g. scrape_nights=1 when query was for 2 nights).
            # Fix: if price_kind is trip_total_* and scrape_nights < query_nights_used,
            # the price is a multi-night total that wasn't divided — divide now.
            # We do NOT divide when price_kind is "nightly_*" because parse_card_to_spec
            # already trusted the per-night value from the JS extractor.
            if (
                query_nights_used > 1
                and benchmark_comp.scrape_nights < query_nights_used
                and benchmark_comp.price_kind.startswith("trip_total")
            ):
                benchmark_price = round(benchmark_price / query_nights_used, 2)
                logger.info(
                    f"[benchmark] {checkin_str}: search-hit price normalized ÷{query_nights_used} "
                    f"nights (price_kind={benchmark_comp.price_kind}) → ${benchmark_price:.2f}"
                )
            benchmark_fetch_status = FETCH_STATUS_SEARCH_HIT
            fetch_confidence = "high"
            logger.info(
                f"[benchmark] {checkin_str}: benchmark found in market search "
                f"(price=${benchmark_price:.2f}, query_nights={query_nights_used})"
            )

        _bm_long_stay_rate = False
        if benchmark_price is None:
            micro_price, micro_search_title, _micro_nights = fetch_benchmark_price_via_micro_search(
                client,
                benchmark_url,
                benchmark_lat,
                benchmark_lng,
                date_i,
                query_nights_sequence=(
                    (int(benchmark_stay_nights),)
                    if benchmark_stay_nights and int(benchmark_stay_nights) > 0
                    else (1, 2)
                ),
                search_location=str(target.location or ""),
            )
            if micro_price:
                benchmark_price = micro_price
                benchmark_fetch_status = FETCH_STATUS_SEARCH_HIT
                # A nightly rate derived from a long stay window (min-stay
                # listings) embeds weekly/monthly discounts — trust it less so
                # the market correction gets more pull.
                _bm_long_stay_rate = _micro_nights > 3
                fetch_confidence = "medium" if _bm_long_stay_rate else "high"

        _bm_dates_unavailable = False
        if benchmark_price is None:
            direct_price, direct_confidence, direct_page_title = _extract_benchmark_price_with_min_stay_fallback(
                client, benchmark_url, checkin_str, _bm_checkout_str
            )
            if direct_price:
                benchmark_price = direct_price
                fetch_confidence = direct_confidence
                benchmark_fetch_status = FETCH_STATUS_DIRECT_PAGE
                logger.info(
                    f"[benchmark] {checkin_str}: direct page price=${benchmark_price:.2f} "
                    f"(confidence={fetch_confidence})"
                )
            else:
                _bm_dates_unavailable = direct_confidence == "unavailable"
                logger.info(
                    f"[benchmark] {checkin_str}: benchmark price unavailable from "
                    f"search, micro-search, and direct page"
                    + (" (dates not bookable)" if _bm_dates_unavailable else "")
                )

        # Real display name for the benchmark: user-saved name → PDP title →
        # search-card / micro-search-card title.
        benchmark_real_title = (
            (benchmark_name or "").strip()
            or (direct_page_title or "")
            or ((benchmark_comp.title or "").strip() if benchmark_comp else "")
            or (micro_search_title or "")
        ) or None

        # ── Secondary comps (Phase 2 — observational only) ───────────────
        # Look up preferredComps[1:] in the already-collected search results.
        # No extra page loads — only search_hit prices are recorded here.
        # These prices feed consensusSignal ONLY and have zero effect on
        # market_median, market_adj_pct, or final_price.
        secondary_prices: Dict[str, Optional[float]] = {}
        for sec_url in (secondary_benchmark_urls or []):
            sec_price: Optional[float] = None
            sec_match = next(
                (c for c in comps if c.url and comp_urls_match(c.url, sec_url)),
                None,
            )
            if sec_match and sec_match.nightly_price and sec_match.nightly_price > 0:
                sec_price = round(float(sec_match.nightly_price), 2)
            secondary_prices[sec_url] = sec_price

        # ── Stage 2: market comps (exclude primary benchmark only) ────────
        # Secondary comps deliberately remain in market_comps so they
        # participate in market_median exactly like any other comp.
        # Excluding them here would change market_adj and final_price,
        # which contradicts the observational-only contract above.
        market_comps = [
            c for c in comps
            if not (c.url and comp_urls_match(c.url, benchmark_url))
        ]

        if comps_collected == 0:
            early_top_comps: List[Dict[str, Any]] = []
            if benchmark_price is not None:
                bm_id = build_comp_id(benchmark_url)
                all_comp_prices[bm_id] = round(benchmark_price, 2)
                early_top_comps.append({
                    "id": bm_id,
                    "title": benchmark_real_title or "Your benchmark listing",
                    "propertyType": target.property_type or "entire_home",
                    "accommodates": None,
                    "bedrooms": None,
                    "baths": None,
                    "nightlyPrice": round(benchmark_price, 2),
                    "currency": "USD",
                    "similarity": 0.98,
                    "rating": None,
                    "reviews": None,
                    "location": None,
                    "url": benchmark_url,
                    "isPinnedBenchmark": True,
                })
            # No market comps at all — price the day at the pure benchmark
            # price, exactly like the market_median-is-None branch below.
            # Leaving median_price unset here discarded a successfully fetched
            # benchmark anchor, made the whole pipeline return empty for sparse
            # markets, and forced a redundant full re-scrape via run_scrape.
            return BenchmarkDayResult(
                date=checkin_str,
                median_price=benchmark_price,
                benchmark_price=benchmark_price,
                benchmark_fetch_status=benchmark_fetch_status,
                fetch_confidence=fetch_confidence,
                secondary_comp_prices=secondary_prices,
                comp_prices=all_comp_prices,
                top_comps=early_top_comps,
                flags=["missing_data"],
                is_sampled=True,
                is_weekend=is_weekend,
                error="No comps found",
                benchmark_title=benchmark_real_title,
            )

        # Filter and score market comps
        filtered_market, filter_debug = filter_similar_candidates(target, market_comps)
        market_scored: List[Tuple[ListingSpec, float]] = [
            (c, similarity_score(target, c)) for c in filtered_market
        ]
        market_scored.sort(key=lambda x: x[1], reverse=True)

        # ── Phase 3B: Price-band filter (benchmark path — pricing only) ──────
        # Applied only to the pricing pool (market_scored_priced), NOT to the
        # display list (market_scored).  Keeping display intact prevents the
        # "only 1 comparable" symptom when the whole market prices above the
        # ±30% anchor band — the user can still see where the market sits.
        #
        # The anchor is benchmark_price (Stage 1); falls back to majority band
        # when benchmark_price is unavailable.
        market_scored_for_pricing = market_scored  # start from full similarity pool
        try:
            _pb_anchor = benchmark_price if (benchmark_price and benchmark_price > 0) else None
            market_scored_for_pricing, _pb_excluded, _pb_info = apply_price_band_filter(
                market_scored, _pb_anchor
            )
            if _pb_excluded:
                logger.info(
                    f"[benchmark] {checkin_str}: price band "
                    f"({_pb_info['anchor_mode']}) "
                    f"${_pb_info.get('lower')}-${_pb_info.get('upper')} "
                    f"excluded={len(_pb_excluded)} from pricing "
                    f"(display unaffected, {len(market_scored)} comps shown)"
                )
        except Exception as _pb_exc:
            logger.warning(f"[benchmark] Price band filter failed (non-fatal): {_pb_exc}")

        # Layer 1 price sanity: remove severe price outliers from market_median
        # computation so they don't skew the benchmark correction signal.
        # market_scored (unfiltered by price band) is kept intact for display.
        _sanity_results, _ps_excl, _ps_down = apply_price_sanity(market_scored_for_pricing)
        market_scored_priced = [
            (r.comp, r.sim_score) for r in _sanity_results if r.weight > 0
        ]
        market_prices = [c.nightly_price for c, _ in market_scored_priced if c.nightly_price]
        market_median = (
            round(statistics.median(market_prices), 2) if market_prices else None
        )

        # ── Per-day secondary consensus signal ────────────────────────────────
        # Look at found secondary comp prices (already collected from this
        # day's search results) and classify them relative to benchmark and
        # market.  This is a per-day signal, not the aggregate consensusSignal.
        #
        #   "support" → secondary comps cluster near benchmark (±20%)
        #   "oppose"  → secondary comps cluster near market    (±20%)
        #   "neutral" → mixed, unclear, or no secondary data
        #
        # The signal is used in Step 2.5 below to nudge effective_weight.
        secondary_signal: str = "neutral"
        found_sec_prices = [p for p in secondary_prices.values() if p is not None]
        if found_sec_prices and benchmark_price is not None:
            sec_mean = statistics.mean(found_sec_prices)
            pct_from_benchmark = abs(sec_mean - benchmark_price) / benchmark_price
            if market_median is not None:
                pct_from_market = abs(sec_mean - market_median) / market_median
                # Classify by which side the secondary mean is *closer* to,
                # and only when it's actually within the threshold of that side.
                # This prevents a secondary comp that sits between both prices
                # from misfiring "support" just because the threshold catches it.
                if pct_from_benchmark <= pct_from_market and pct_from_benchmark <= _SECONDARY_CONSENSUS_THRESHOLD:
                    secondary_signal = "support"
                elif pct_from_market < pct_from_benchmark and pct_from_market <= _SECONDARY_CONSENSUS_THRESHOLD:
                    secondary_signal = "oppose"
                # else: equidistant / both too far → neutral
            elif pct_from_benchmark <= _SECONDARY_CONSENSUS_THRESHOLD:
                secondary_signal = "support"

        # ── Effective market weight: confidence + comp-count + secondary ──────
        # Step 1 — base weight from benchmark fetch confidence.
        #   High (search card)  → 0.30  benchmark-dominant
        #   Medium (DOM)        → 0.42  slightly more market pull
        #   Low (body regex)    → 0.55  benchmark and market roughly equal
        base_weight = _CONFIDENCE_EFFECTIVE_WEIGHTS.get(fetch_confidence, BENCHMARK_MARKET_WEIGHT)

        # Step 2 — scale by market comp count: fewer comps → market median
        # is less representative → pull it back proportionally.
        comp_reliability = min(1.0, len(market_prices) / _MIN_COMPS_FOR_FULL_WEIGHT)
        effective_weight = round(base_weight * comp_reliability, 3)

        # Step 2.5 — secondary consensus nudge.
        # Secondary comps that agree with the benchmark → market has less pull.
        # Secondary comps that agree with the market   → market has more pull.
        # Multipliers are kept small (0.90 / 1.15) so secondary comps can
        # never override the primary benchmark signal — they only nudge.
        # When there are no secondary comps or the signal is neutral, no change.
        secondary_multiplier = _SECONDARY_SIGNAL_MULTIPLIERS.get(secondary_signal, 1.0)
        if secondary_multiplier != 1.0:
            effective_weight = round(effective_weight * secondary_multiplier, 3)

        # Step 2.7 — benchmark-to-target structural similarity guardrail.
        # When the benchmark listing differs significantly from the user's
        # target property (e.g. 1-BR private room vs 3-BR entire home), the
        # benchmark price is a less reliable anchor.  Reduce effective_weight
        # so that the market adjustment corrects more of the structural gap.
        # Default benchmark_target_similarity=1.0 → no adjustment (no user
        # attributes provided or similarity not computed).
        if benchmark_target_similarity < _BM_SIMILARITY_STRONG_MISMATCH:
            effective_weight = round(effective_weight * _BM_MISMATCH_STRONG_FACTOR, 3)
        elif benchmark_target_similarity < _BM_SIMILARITY_HIGH_MATCH:
            effective_weight = round(effective_weight * _BM_MISMATCH_MODERATE_FACTOR, 3)

        # ── Flags ─────────────────────────────────────────────────────────
        flags: List[str] = []
        if benchmark_fetch_status == FETCH_STATUS_FAILED:
            flags.append("benchmark_fetch_failed")
        if _bm_dates_unavailable:
            flags.append("benchmark_dates_unavailable")
        if _bm_long_stay_rate:
            flags.append("benchmark_long_stay_rate")

        # ── Blend: benchmark anchor + confidence-weighted market adjustment ──
        final_price: Optional[float] = None
        market_adj_pct: Optional[float] = None
        applied_adj_pct: Optional[float] = None
        outlier_action: str = ""

        if benchmark_price is not None:
            if market_median is not None:
                raw_adj = (market_median - benchmark_price) / benchmark_price

                # Step 3 — directional outlier guard.
                #
                # When the gap exceeds BENCHMARK_OUTLIER_THRESHOLD (40%) we
                # cannot blindly always shrink market influence — that would
                # systematically bias toward the benchmark even when the
                # benchmark is unreliable and the market is well-sampled.
                #
                # Instead, adjust based on the *relative* reliability of each
                # signal:
                #
                #   benchmark_reliable = fetch_confidence == "high"
                #     (search-card price — authoritative)
                #   market_reliable = enough comps to trust the median
                #
                # Four outcomes:
                #   benchmark better, market weak   → lean_benchmark  (0.50×)
                #   market better, benchmark shaky  → lean_market     (1.00× — do NOT shrink further)
                #   both reliable but diverge       → conservative    (0.75×)
                #   neither reliable                → minimal         (0.40×)
                if abs(raw_adj) > BENCHMARK_OUTLIER_THRESHOLD:
                    # "medium" = direct-page DOM widget extraction — reliable enough
                    # to anchor pricing.  Treating it as "not reliable" caused
                    # lean_market (outlier_factor=1.0) to fire on direct-page fetches,
                    # leaving full market weight applied even at a >40% gap.
                    benchmark_reliable = (fetch_confidence in ("high", "medium"))
                    market_reliable = (len(market_prices) >= _MIN_COMPS_FOR_FULL_WEIGHT)

                    if benchmark_reliable and not market_reliable:
                        # Benchmark is a trustworthy search-card price; market
                        # sample is thin.  Pull final price toward benchmark.
                        outlier_factor = 0.50
                        outlier_action = "lean_benchmark"
                    elif not benchmark_reliable and market_reliable:
                        # Benchmark came from a less reliable extraction method
                        # (DOM / body-text).  Market has enough comps.
                        # Do NOT shrink market influence — leave effective_weight
                        # as-is so the market can correct the uncertain anchor.
                        outlier_factor = 1.00
                        outlier_action = "lean_market"
                    elif benchmark_reliable and market_reliable:
                        # Both signals are credible but sharply diverge.
                        # Apply a mild conservative pull toward the benchmark
                        # (user chose it as anchor) without dismissing market.
                        outlier_factor = 0.75
                        outlier_action = "conservative"
                    else:
                        # Neither signal is highly reliable.
                        # Keep adjustment minimal to avoid compounding errors.
                        outlier_factor = 0.40
                        outlier_action = "minimal"

                    effective_weight = round(effective_weight * outlier_factor, 3)
                    flags.append("benchmark_outlier")
                    logger.info(
                        f"[benchmark] {checkin_str}: outlier — "
                        f"market=${market_median:.2f} vs benchmark=${benchmark_price:.2f} "
                        f"(gap={raw_adj*100:.1f}%), action={outlier_action}, "
                        f"outlier_factor={outlier_factor}, effective_weight={effective_weight}"
                    )

                capped_adj = max(-BENCHMARK_MAX_ADJ, min(BENCHMARK_MAX_ADJ, raw_adj))
                adj_factor = 1.0 + capped_adj * effective_weight
                final_price = round(benchmark_price * adj_factor, 2)
                market_adj_pct = round(raw_adj * 100, 1)
                applied_adj_pct = round((adj_factor - 1.0) * 100, 1)
            else:
                # No market comps — pure benchmark price, no market influence
                final_price = benchmark_price
                effective_weight = 0.0
        elif market_median is not None:
            # Benchmark price unavailable for this day.  Rather than using the
            # raw market median (which would pull the overall recommendation far
            # below the benchmark level on days where the listing is booked or
            # blocked), signal the interpolation layer to fill this day from
            # adjacent benchmark-anchored sampled days.
            #
            # market_price is still recorded so aggregate_benchmark_transparency
            # can report the market signal even when it wasn't used for pricing.
            #
            # NOTE: if ALL sampled days fail to fetch the benchmark, interpolation
            # has no anchors and the pipeline returns an empty recommendation — the
            # caller in run_benchmark_scrape should then fall back to run_scrape.
            final_price = None
            effective_weight = 0.0

        # Build top-comps payload. Prepend the primary benchmark so it always
        # appears in comparableListings with its per-day benchmark_price.
        top_comps_scored = market_scored[: max(3, top_k)]
        top_comps = [
            {**to_comparable_payload(c, s, target=target), "isPinnedBenchmark": False}
            for c, s in top_comps_scored
        ]

        if benchmark_price is not None:
            bm_id = build_comp_id(benchmark_url)
            bm_spec = benchmark_comp  # may be None if not in search results
            bm_payload: Dict[str, Any] = {
                "id": bm_id,
                "title": benchmark_real_title or "Your benchmark listing",
                "propertyType": (bm_spec.property_type if bm_spec else "") or target.property_type or "entire_home",
                "accommodates": int(bm_spec.accommodates) if bm_spec and isinstance(bm_spec.accommodates, (int, float)) else None,
                "bedrooms": int(bm_spec.bedrooms) if bm_spec and isinstance(bm_spec.bedrooms, (int, float)) else None,
                "baths": round(float(bm_spec.baths), 1) if bm_spec and isinstance(bm_spec.baths, (int, float)) else None,
                "nightlyPrice": round(benchmark_price, 2),
                "currency": "USD",
                "similarity": 0.98,
                "rating": round(float(bm_spec.rating), 2) if bm_spec and isinstance(bm_spec.rating, (int, float)) else None,
                "reviews": int(bm_spec.reviews) if bm_spec and isinstance(bm_spec.reviews, (int, float)) else None,
                "amenities": list((bm_spec.amenities if bm_spec else []) or []),
                "location": (bm_spec.location if bm_spec else "") or None,
                "url": benchmark_url,
                "isPinnedBenchmark": True,
            }
            top_comps = [bm_payload] + top_comps
            # Also track benchmark price in comp_prices so priceByDate is populated
            all_comp_prices[bm_id] = round(benchmark_price, 2)

        # Add secondary benchmark comps explicitly to top_comps so they always
        # appear in comparableListings, deduplicated against existing entries.
        for sec_url in (secondary_benchmark_urls or []):
            sec_comp = next(
                (c for c in comps if c.url and comp_urls_match(c.url, sec_url)),
                None,
            )
            if sec_comp and sec_comp.nightly_price:
                sec_id = build_comp_id(sec_url)
                if not any(tc.get("id") == sec_id for tc in top_comps):
                    sec_payload = to_comparable_payload(
                        sec_comp, similarity_score(target, sec_comp), target=target
                    )
                    sec_payload["isPinnedBenchmark"] = True
                    top_comps.append(sec_payload)
                if sec_id and sec_comp.nightly_price:
                    all_comp_prices[sec_id] = round(float(sec_comp.nightly_price), 2)

        # Price distribution (benchmark anchor prepended as a data point)
        dist = compute_price_distribution(market_prices, prepend=benchmark_price)

        # Hard cap: final price must stay within ±15% of benchmark anchor.
        # This prevents market noise from pushing the recommendation too far
        # from the listing the user knows and trusts.
        if benchmark_price is not None and final_price is not None:
            cap_max = round(benchmark_price * 1.15, 2)
            cap_min = round(benchmark_price * 0.85, 2)
            capped = max(cap_min, min(cap_max, final_price))
            if capped != final_price:
                logger.info(
                    f"[benchmark] {checkin_str}: price capped "
                    f"${final_price:.2f} → ${capped:.2f} "
                    f"(benchmark=${benchmark_price:.2f}, ±15% band [{cap_min:.2f}, {cap_max:.2f}])"
                )
                final_price = capped

        logger.info(
            f"[benchmark] {checkin_str}: anchor=${benchmark_price} "
            f"market_median=${market_median} adj={applied_adj_pct}% "
            f"final=${final_price} (status={benchmark_fetch_status})"
        )

        return BenchmarkDayResult(
            date=checkin_str,
            median_price=final_price,
            benchmark_price=benchmark_price,
            market_price=market_median,
            market_adj_pct=market_adj_pct,
            applied_adj_pct=applied_adj_pct,
            benchmark_fetch_status=benchmark_fetch_status,
            fetch_confidence=fetch_confidence,
            effective_weight=effective_weight,
            outlier_action=outlier_action,
            secondary_signal=secondary_signal,
            secondary_comp_prices=secondary_prices,
            comp_prices=all_comp_prices,
            comps_collected=comps_collected,
            comps_used=len(top_comps_scored),
            filter_stage=filter_debug.get("stage", "unknown"),
            flags=flags,
            is_sampled=True,
            is_weekend=is_weekend,
            price_distribution=dist,
            top_comps=top_comps,
            benchmark_title=benchmark_real_title,
        )

    except Exception as exc:
        logger.warning(f"[benchmark] {checkin_str}: error: {exc}")
        return BenchmarkDayResult(
            date=checkin_str,
            benchmark_fetch_status=FETCH_STATUS_FAILED,
            fetch_confidence="failed",
            secondary_comp_prices={},
            comp_prices={},
            flags=["missing_data"],
            is_sampled=True,
            is_weekend=is_weekend,
            error=str(exc)[:200],
        )


# ── Aggregate transparency stats ─────────────────────────────────────────────

def aggregate_benchmark_transparency(
    benchmark_url: str,
    day_results: List[BenchmarkDayResult],
) -> Dict[str, Any]:
    """
    Aggregate per-day benchmark stats into the transparency block
    surfaced to the frontend.
    """
    total = len(day_results)
    search_hits = sum(
        1 for r in day_results if r.benchmark_fetch_status == FETCH_STATUS_SEARCH_HIT
    )
    direct_fetches = sum(
        1 for r in day_results if r.benchmark_fetch_status == FETCH_STATUS_DIRECT_PAGE
    )
    failed = sum(
        1 for r in day_results if r.benchmark_fetch_status == FETCH_STATUS_FAILED
    )

    benchmark_prices = [r.benchmark_price for r in day_results if r.benchmark_price is not None]
    market_prices = [r.market_price for r in day_results if r.market_price is not None]
    adj_pcts = [r.market_adj_pct for r in day_results if r.market_adj_pct is not None]

    avg_benchmark = round(statistics.mean(benchmark_prices), 2) if benchmark_prices else None
    avg_market = round(statistics.mean(market_prices), 2) if market_prices else None
    avg_adj = round(statistics.mean(adj_pcts), 1) if adj_pcts else None

    benchmark_used = len(benchmark_prices) > 0
    fallback_reason: Optional[str] = None
    if not benchmark_used:
        # Distinguish "listing exists but isn't bookable for these dates"
        # (host calendar blocked/closed) from a genuine fetch failure.
        if any("benchmark_dates_unavailable" in r.flags for r in day_results):
            fallback_reason = "benchmark_unavailable_for_dates"
        else:
            fallback_reason = "benchmark_fetch_failed"

    # Determine primary fetch method used
    if search_hits >= direct_fetches and search_hits > 0:
        primary_method = FETCH_STATUS_SEARCH_HIT
    elif direct_fetches > 0:
        primary_method = FETCH_STATUS_DIRECT_PAGE
    else:
        primary_method = FETCH_STATUS_FAILED

    # Confidence breakdown
    high_confidence_days = sum(1 for r in day_results if r.fetch_confidence == "high")
    medium_confidence_days = sum(1 for r in day_results if r.fetch_confidence == "medium")
    low_confidence_days = sum(1 for r in day_results if r.fetch_confidence == "low")

    # Secondary comp aggregation (observational — pricing formula unchanged)
    all_secondary_urls: set = set()
    for r in day_results:
        all_secondary_urls.update(r.secondary_comp_prices.keys())

    secondary_comps_agg: List[Dict[str, Any]] = []
    for sec_url in sorted(all_secondary_urls):
        prices = [
            r.secondary_comp_prices[sec_url]
            for r in day_results
            if sec_url in r.secondary_comp_prices
            and r.secondary_comp_prices[sec_url] is not None
        ]
        secondary_comps_agg.append({
            "url": sec_url,
            "avgPrice": round(statistics.mean(prices), 2) if prices else None,
            "daysFound": len(prices),
            "totalDays": total,
        })

    # Consensus signal: do secondary comps cluster near benchmark or near market?
    consensus_signal: Optional[str] = None
    if secondary_comps_agg and avg_benchmark is not None:
        found_avgs = [
            s["avgPrice"] for s in secondary_comps_agg if s["avgPrice"] is not None
        ]
        if found_avgs:
            secondary_mean = statistics.mean(found_avgs)
            pct_from_benchmark = abs(secondary_mean - avg_benchmark) / avg_benchmark
            if avg_market is not None:
                pct_from_market = abs(secondary_mean - avg_market) / avg_market
                if pct_from_benchmark <= 0.20:
                    consensus_signal = "strong"     # secondary comps agree with benchmark
                elif pct_from_market <= 0.20 and pct_from_benchmark > 0.20:
                    consensus_signal = "divergent"  # secondary comps agree with market, not benchmark
                else:
                    consensus_signal = "mixed"
            else:
                consensus_signal = "strong" if pct_from_benchmark <= 0.20 else "mixed"

    # Effective weight stats (only from days where benchmark was used)
    eff_weights = [
        r.effective_weight
        for r in day_results
        if r.benchmark_price is not None and r.market_price is not None
    ]
    avg_eff_weight = round(statistics.mean(eff_weights), 3) if eff_weights else BENCHMARK_MARKET_WEIGHT

    # Outlier days: benchmark and market diverged ≥ BENCHMARK_OUTLIER_THRESHOLD
    outlier_days = sum(1 for r in day_results if "benchmark_outlier" in r.flags)

    # Anchor/interpolation breakdown
    days_with_benchmark_anchor = len(benchmark_prices)  # days where benchmark_price was fetched
    days_benchmark_failed = sum(
        1 for r in day_results if "benchmark_fetch_failed" in r.flags
    )
    days_interpolated = sum(
        1 for r in day_results
        if r.median_price is not None and "benchmark_fetch_failed" in r.flags
    )  # failed days that were filled by interpolation

    # Conflict detected when:
    #   - outlier days exceed 30% of sampled days, OR
    #   - secondary comps clearly agree with market rather than benchmark
    conflict_detected = bool(
        (total > 0 and outlier_days / total > 0.30)
        or consensus_signal == "divergent"
    )

    # Real listing name — first day that resolved one (user-saved name, PDP
    # title, or search-card title). None keeps the frontend placeholder.
    benchmark_title = next(
        (r.benchmark_title for r in day_results if r.benchmark_title), None
    )

    return {
        "benchmarkUsed": benchmark_used,
        "benchmarkUrl": benchmark_url,
        "benchmarkTitle": benchmark_title,
        "benchmarkFetchStatus": primary_method,
        "benchmarkFetchMethod": primary_method,
        "avgBenchmarkPrice": avg_benchmark,
        "avgMarketPrice": avg_market,
        "marketAdjustmentPct": avg_adj,
        "appliedMarketWeight": BENCHMARK_MARKET_WEIGHT,
        "effectiveMarketWeight": avg_eff_weight,
        "maxAdjCap": BENCHMARK_MAX_ADJ,
        "outlierDays": outlier_days,
        "conflictDetected": conflict_detected,
        "fallbackReason": fallback_reason,
        "fetchStats": {
            "searchHits": search_hits,
            "directFetches": direct_fetches,
            "failed": failed,
            "totalDays": total,
            "highConfidenceDays": high_confidence_days,
            "mediumConfidenceDays": medium_confidence_days,
            "lowConfidenceDays": low_confidence_days,
        },
        "secondaryComps": secondary_comps_agg or None,
        "consensusSignal": consensus_signal,
        "anchorStats": {
            "daysWithBenchmarkAnchor": days_with_benchmark_anchor,
            "daysBenchmarkFailed": days_benchmark_failed,
            "daysInterpolated": days_interpolated,
            "totalSampledDays": total,
        },
    }


# ── Convert BenchmarkDayResult → plain dict for pipeline compatibility ────────

def benchmark_day_result_to_dict(r: BenchmarkDayResult) -> Dict[str, Any]:
    return {
        "date": r.date,
        "median_price": r.median_price,
        "benchmark_price": r.benchmark_price,
        "market_price": r.market_price,
        "market_adj_pct": r.market_adj_pct,
        "applied_adj_pct": r.applied_adj_pct,
        "benchmark_fetch_status": r.benchmark_fetch_status,
        "fetch_confidence": r.fetch_confidence,
        "effective_weight": r.effective_weight,
        "outlier_action": r.outlier_action,
        "secondary_signal": r.secondary_signal,
        "secondary_comp_prices": r.secondary_comp_prices,
        "comps_collected": r.comps_collected,
        "comps_used": r.comps_used,
        "filter_stage": r.filter_stage,
        "flags": r.flags,
        "is_sampled": r.is_sampled,
        "is_weekend": r.is_weekend,
        "price_distribution": r.price_distribution,
        "top_comps": r.top_comps,
        "error": r.error,
        "benchmark_title": r.benchmark_title,
    }
