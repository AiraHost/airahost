"""
Day-by-day query engine.

Daily sampling strategy:
  1. Query one-night inventory (checkin=day_i, checkout=day_i+1), target 25 comps.
  2. Query two-night inventory (checkin=day_i, checkout=day_i+2), target 25 comps.
  3. Merge pools for pricing and ranking (dedup by listing id).

Per-night normalization is handled in `comp_collection._map_search_row_to_spec`.
2-night totals are converted to nightly values before pricing/display.
"""

from __future__ import annotations

import logging
import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from worker.core.comp_utils import (
    build_comp_id,
    build_comp_prices_dict,
    compute_price_distribution,
    to_comparable_payload,
)
from worker.core.price_band import apply_price_band_filter
from worker.core.price_sanity import apply_price_sanity, build_price_sanity_weights
from worker.core.pricing_engine import recommend_price
from worker.core.similarity import (
    SIMILARITY_FLOOR,
    comp_urls_match,
    filter_similar_candidates,
    similarity_score,
)
from worker.scraper.comp_collection import collect_search_comps
from worker.scraper.parsers import parse_pdp_response, parse_search_listing_context
from worker.scraper.target_extractor import ListingSpec, safe_domain_base

logger = logging.getLogger("worker.scraper.day_query")

# Boost applied to the display similarity of the pinned comp in top_comps list
_PINNED_DISPLAY_MULTIPLIER: float = 2.0
_PINNED_DISPLAY_MAX_SCORE: float = 0.98

# ── Configurable constants ───────────────────────────────────────

MAX_NIGHTS = int(os.getenv("MAX_NIGHTS_PER_REPORT", "30"))
SAMPLE_THRESHOLD = int(os.getenv("SAMPLE_THRESHOLD_NIGHTS", "14"))
MAX_SAMPLE_QUERIES = 20
PER_DAY_TIMEOUT_S = 15
PER_DAY_MAX_RETRIES = 2
DAY_SCROLL_ROUNDS = int(os.getenv("DAY_QUERY_SCROLL_ROUNDS", "2"))
DAY_MAX_CARDS = int(os.getenv("DAY_QUERY_MAX_CARDS", "30"))
FIXED_COMP_DEEP_PAGES = int(os.getenv("FIXED_COMP_DEEP_PAGES", "3"))
FIXED_COMP_DEEP_MIN_HITS = int(os.getenv("FIXED_COMP_DEEP_MIN_HITS", "4"))
FIXED_COMP_MIN_PRICED = int(os.getenv("FIXED_COMP_MIN_PRICED", "4"))
MAP_RADIUS_CAP_KM = 5.0 * 1.609344  # exactly 5 miles
DAY_MIN_SCAN_TOTAL = int(os.getenv("DAY_QUERY_MIN_SCAN_TOTAL", "50"))
DAY_ONE_NIGHT_COMP_TARGET = int(os.getenv("DAY_ONE_NIGHT_COMP_TARGET", "25"))
DAY_TWO_NIGHT_COMP_TARGET = int(os.getenv("DAY_TWO_NIGHT_COMP_TARGET", "25"))
DAY_QUERY_PDP_REVALIDATE_TOP_N = max(
    0, int(os.getenv("DAY_QUERY_PDP_REVALIDATE_TOP_N", "5"))
)

# Relaxed similarity floor — used when the strict floor yields zero comps for a day.
# Comps in range [SIMILARITY_FLOOR_FALLBACK, SIMILARITY_FLOOR) are accepted only when
# the strict pool is empty, and the result is tagged selection_mode="fallback_relaxed"
# with pricing_confidence="low".  Property-type hard gate and price-sanity outlier
# rejection are both retained in fallback mode.  Price-band is skipped (sparse pool).
SIMILARITY_FLOOR_FALLBACK: float = 0.25


# ── Data structures ──────────────────────────────────────────────

@dataclass
class DayResult:
    """Result of a single day query (2-night primary, 1-night fallback) or interpolated placeholder."""

    date: str                                        # "YYYY-MM-DD"
    median_price: Optional[float] = None             # weighted mean from comps
    comps_collected: int = 0
    comps_used: int = 0
    below_similarity_floor: int = 0                  # comps excluded by similarity floor
    price_outliers_excluded: int = 0                 # comps rejected by Layer 1 price sanity
    price_outliers_downweighted: int = 0             # comps downweighted (0.5×) by price sanity
    geo_excluded: int = 0                            # comps rejected by distance filter (Phase 3A)
    price_band_excluded: int = 0                     # comps rejected by price band filter (Phase 3B)
    filter_stage: str = ""
    flags: List[str] = field(default_factory=list)   # peak, low_demand, missing_data, interpolated, low_comp_confidence
    is_sampled: bool = True                          # False if interpolated
    is_weekend: bool = False
    selection_mode: str = "strict"       # "strict" | "fallback_relaxed" | "strict_empty"
    pricing_confidence: str = "high"     # "high" | "medium" | "low"
    price_distribution: Dict[str, Any] = field(default_factory=dict)
    top_comps: List[Dict[str, Any]] = field(default_factory=list)
    # All priced comps for this day (room_id -> nightly_price).
    # Used to populate priceByDate for every comp, not just top_k.
    comp_prices: Dict[str, float] = field(default_factory=dict)
    error: Optional[str] = None


# ── Date utilities ───────────────────────────────────────────────

def daterange_nights(start_date: date, end_date: date) -> List[date]:
    """
    Return each night in [start_date, end_date).
    E.g., start=Mar 1, end=Mar 4 => [Mar 1, Mar 2, Mar 3].
    """
    nights: List[date] = []
    current = start_date
    while current < end_date:
        nights.append(current)
        current += timedelta(days=1)
    return nights


def compute_sample_dates(total_nights: int, max_queries: int = MAX_SAMPLE_QUERIES) -> List[int]:
    """
    For ranges > SAMPLE_THRESHOLD, return ~max_queries evenly-spaced indices.
    Always includes index 0 (first night) and total_nights-1 (last night).
    """
    if total_nights <= max_queries:
        return list(range(total_nights))

    step = math.ceil(total_nights / max_queries)
    indices: set[int] = {0, total_nights - 1}
    i = 0
    while i < total_nights:
        indices.add(i)
        i += step
    return sorted(indices)


# ── Single-day query ─────────────────────────────────────────────

def _derive_canonical_search_location(target: ListingSpec) -> str:
    if isinstance(target.lat, (int, float)) and isinstance(target.lng, (int, float)):
        return f"{float(target.lat):.5f},{float(target.lng):.5f}"
    city = str(target.city or "").strip()
    state = str(target.state or "").strip()
    if city and state:
        return f"{city}, {state}"
    if city:
        return city
    return str(target.location or "").strip()


def _resolve_query_center(search_location: str, target: ListingSpec) -> Tuple[Optional[float], Optional[float]]:
    """
    Resolve map-search center coordinates.

    Priority:
      1) Target listing coordinates (when available)
      2) Best-effort geocode of canonical search location
    """
    if isinstance(target.lat, (int, float)) and isinstance(target.lng, (int, float)):
        return float(target.lat), float(target.lng)
    if not str(search_location or "").strip():
        return None, None
    try:
        from worker.core.geocode_details import geocode_address_details
        result = geocode_address_details(str(search_location), timeout=3)
        if result and result.get("lat") is not None and result.get("lng") is not None:
            lat = float(result["lat"])
            lng = float(result["lng"])
            logger.info(
                "[day_query] map center geocoded from search_location=%r -> (%.6f, %.6f)",
                search_location,
                lat,
                lng,
            )
            return lat, lng
    except Exception as exc:
        logger.info(f"[day_query] map center geocode skipped/failed: {exc}")
    return None, None


def _get_locked_search_location(client, target: ListingSpec) -> str:
    locked = getattr(client, "_locked_search_location", None)
    if isinstance(locked, str) and locked.strip():
        return locked
    canonical = _derive_canonical_search_location(target)
    if canonical:
        setattr(client, "_locked_search_location", canonical)
        logger.info(f"[day_query] locked canonical search_location={canonical!r}")
    return canonical


def _revalidate_top_comp_prices_with_pdp(
    client,
    ranked_comps: List[Tuple[ListingSpec, float]],
    *,
    date_i: date,
    adults: int,
    top_n: int,
) -> Dict[str, int]:
    """
    Revalidate top comparable prices from PDP for the exact queried date window.

    StaysSearch prices can drift from PDP booking-widget nightly prices by a few
    dollars. This updates ListingSpec.nightly_price in place for top-ranked comps.
    """
    if top_n <= 0 or not ranked_comps:
        return {"attempted": 0, "updated": 0}

    attempted = 0
    updated = 0
    for comp, _score in ranked_comps:
        if attempted >= top_n:
            break

        comp_url = str(comp.url or "").strip()
        comp_id = build_comp_id(comp_url)
        if not comp_url or not str(comp_id).isdigit():
            continue

        nights = max(1, int(comp.scrape_nights or 1))
        checkin = date_i.isoformat()
        checkout = (date_i + timedelta(days=nights)).isoformat()
        attempted += 1

        try:
            pdp_data = client.get_listing_details(
                str(comp_id),
                checkin=checkin,
                checkout=checkout,
                adults=adults,
            )
            parsed = parse_pdp_response(
                pdp_data,
                str(comp_id),
                safe_domain_base(comp_url),
            )
            pdp_nightly = parsed.get("nightly_price")
            if not isinstance(pdp_nightly, (int, float)) or pdp_nightly <= 0:
                continue

            old_price = comp.nightly_price
            new_price = round(float(pdp_nightly), 2)
            comp.nightly_price = new_price
            comp.currency = str(parsed.get("currency") or comp.currency or "USD")
            comp.price_kind = "nightly_from_pdp"
            if nights > 1:
                total = parsed.get("total_price")
                if isinstance(total, (int, float)) and total > 0:
                    comp.query_total_price = round(float(total), 2)

            if not isinstance(old_price, (int, float)) or round(float(old_price), 2) != new_price:
                updated += 1
                logger.info(
                    "[day_query] %s: pdp price revalidated listing=%s nights=%s search_price=%s pdp_price=%s delta=%s",
                    checkin,
                    comp_id,
                    nights,
                    old_price,
                    new_price,
                    (
                        round(new_price - float(old_price), 2)
                        if isinstance(old_price, (int, float))
                        else None
                    ),
                )
        except Exception as exc:
            logger.info(
                "[day_query] %s: pdp revalidation skipped listing=%s reason=%s",
                checkin,
                comp_id,
                str(exc)[:200],
            )

    return {"attempted": attempted, "updated": updated}


def estimate_base_price_for_date(
    client,
    target: ListingSpec,
    base_origin: str,
    date_i: date,
    adults: int,
    *,
    max_scroll_rounds: int = DAY_SCROLL_ROUNDS,
    max_cards: int = DAY_MAX_CARDS,
    rate_limit_seconds: float = 1.0,
    top_k: int = 20,
    preferred_comps: Optional[List[Dict[str, Any]]] = None,
    excluded_room_ids: Optional[set[str]] = None,
    max_radius_km: Optional[float] = None,
) -> DayResult:
    """
    Execute a 1-night Airbnb search for date_i -> date_i+1.
    Collect cards, filter by similarity to target, compute price distribution.
    """
    top_k = max(20, int(top_k))
    min_scan_per_mode = max(1, math.ceil(max(1, DAY_MIN_SCAN_TOTAL) / 2))
    one_night_scan_target = max(DAY_ONE_NIGHT_COMP_TARGET, min_scan_per_mode)
    two_night_scan_target = max(DAY_TWO_NIGHT_COMP_TARGET, min_scan_per_mode)
    checkin_str = date_i.isoformat()
    is_weekend = date_i.weekday() >= 4  # Fri=4, Sat=5

    try:
        search_location = _get_locked_search_location(client, target) or target.location
        query_center_lat, query_center_lng = _resolve_query_center(search_location, target)
        map_radius_limit_km = MAP_RADIUS_CAP_KM
        if isinstance(max_radius_km, (int, float)) and float(max_radius_km) > 0:
            map_radius_limit_km = min(float(max_radius_km), MAP_RADIUS_CAP_KM)
        # Daily compset: collect both 1-night and 2-night pools (>=50 total scan target), then merge.
        logger.info(
            f"[day_query] {checkin_str}: scan targets one_night={one_night_scan_target} "
            f"two_night={two_night_scan_target} total_target={one_night_scan_target + two_night_scan_target}"
        )
        one_night_comps, _one_qn = collect_search_comps(
            client,
            search_location,
            base_origin,
            date_i,
            adults,
            max_scroll_rounds=max_scroll_rounds,
            max_cards=one_night_scan_target,
            rate_limit_seconds=rate_limit_seconds,
            timeout_ms=PER_DAY_TIMEOUT_S * 1000,
            exclude_url=target.url,
            log_prefix="everyday_scrape_one_night",
            center_lat=query_center_lat,
            center_lng=query_center_lng,
            map_radius_km=map_radius_limit_km if query_center_lat is not None and query_center_lng is not None else None,
            target_accommodates=target.accommodates,
            prefer_one_night=True,
        )
        two_night_comps, _two_qn = collect_search_comps(
            client,
            search_location,
            base_origin,
            date_i,
            adults,
            max_scroll_rounds=max_scroll_rounds,
            max_cards=two_night_scan_target,
            rate_limit_seconds=rate_limit_seconds,
            timeout_ms=PER_DAY_TIMEOUT_S * 1000,
            exclude_url=target.url,
            log_prefix="everyday_scrape_two_night",
            center_lat=query_center_lat,
            center_lng=query_center_lng,
            map_radius_km=map_radius_limit_km if query_center_lat is not None and query_center_lng is not None else None,
            target_accommodates=target.accommodates,
            prefer_two_night=True,
        )

        comps_by_id: Dict[str, ListingSpec] = {}
        for c in one_night_comps:
            cid = build_comp_id(c.url or "")
            if cid and cid not in comps_by_id:
                comps_by_id[cid] = c
        for c in two_night_comps:
            cid = build_comp_id(c.url or "")
            if cid and cid not in comps_by_id:
                comps_by_id[cid] = c
        comps = list(comps_by_id.values())
        query_nights_used = 1 if one_night_comps else (2 if two_night_comps else 1)
        logger.info(
            f"[day_query] {checkin_str}: daily pools one_night={len(one_night_comps)} "
            f"two_night={len(two_night_comps)} merged={len(comps)}"
        )

        # ── User blacklist filter (per-listing excludedComps) ─────
        # Drop comps whose Airbnb roomId is in the user's exclusion set BEFORE
        # similarity / pricing math AND before build_comp_prices_dict — otherwise
        # transparent priceByDate would still surface the excluded comp.
        if excluded_room_ids:
            pre = len(comps)
            comps = [
                c for c in comps
                if build_comp_id(c.url or "") not in excluded_room_ids
            ]
            dropped = pre - len(comps)
            if dropped > 0:
                logger.info(
                    f"[day_query] {checkin_str}: excluded_by_user={dropped} "
                    f"(remaining {len(comps)})"
                )

        # ── Phase 3A: Geographic distance filter ──────────────────
        # Applied before similarity scoring.  Requires both the target
        # and at least some comps to have coordinates; otherwise skipped.
        # Comps without coords always pass through — see geo_filter.py.
        geo_excluded_count = 0

        comps_collected = len(comps)

        if comps_collected == 0:
            return DayResult(
                date=checkin_str,
                comps_collected=0,
                filter_stage="empty",
                flags=["missing_data"],
                is_sampled=True,
                is_weekend=is_weekend,
                error="No comps found",
            )

        filtered_comps, filter_debug = filter_similar_candidates(target, comps)

        # Score and rank (raw scores stored separately before any boost).
        comps_scored = [(c, similarity_score(target, c)) for c in filtered_comps]
        # Capture raw scores keyed by object id before the boost step overwrites them.
        raw_sim_scores: Dict[int, float] = {id(c): s for c, s in comps_scored}
        comps_scored.sort(key=lambda x: x[1], reverse=True)

        # Build list of enabled preferred comp URLs for boost logic.
        # The pricing boost is handled inside recommend_price via preferred_comp_urls.
        pref_urls: List[str] = []
        if preferred_comps:
            for pc in preferred_comps:
                if pc.get("enabled", True):
                    u = str(pc.get("listingUrl") or "").strip()
                    if u:
                        pref_urls.append(u)

        if pref_urls:
            boosted: List[Tuple[ListingSpec, float]] = []
            pinned_hit = False
            for c, s in comps_scored:
                if c.url and any(comp_urls_match(c.url, pu) for pu in pref_urls):
                    boosted_s = min(s * _PINNED_DISPLAY_MULTIPLIER, _PINNED_DISPLAY_MAX_SCORE)
                    boosted.append((c, boosted_s))
                    pinned_hit = True
                    logger.info(
                        f"[day_query] Pinned comp hit on {date_i}: {c.url} "
                        f"(display score {s:.3f} → {boosted_s:.3f})"
                    )
                else:
                    boosted.append((c, s))
            if pinned_hit:
                comps_scored = sorted(boosted, key=lambda x: x[1], reverse=True)

        # Apply similarity floor using raw (unboosted) scores.
        # Only comps above the floor enter pricing and display.
        above_floor = [
            (c, s) for c, s in comps_scored
            if raw_sim_scores.get(id(c), 0.0) >= SIMILARITY_FLOOR
        ]
        below_floor_count = len(comps_scored) - len(above_floor)
        selection_mode = "strict"
        _using_fallback = False

        if below_floor_count > 0:
            all_scores = [s for _, s in comps_scored]
            logger.info(
                f"[day_query] {checkin_str}: similarity — "
                f"below_floor={below_floor_count}/{len(comps_scored)} "
                f"(floor={SIMILARITY_FLOOR}) "
                f"score_range=[{min(all_scores):.3f}, {max(all_scores):.3f}] "
                f"score_mean={sum(all_scores)/len(all_scores):.3f}"
            )
        if not above_floor:
            logger.warning(
                f"[day_query] {checkin_str}: ALL {len(comps_scored)} comps below similarity floor "
                f"— target: type={target.property_type!r} bedrooms={target.bedrooms} "
                f"accommodates={target.accommodates} baths={target.baths}"
            )

        # ── Graceful fallback: relaxed similarity floor ──────────────────────
        # When the strict floor (SIMILARITY_FLOOR) leaves zero comps, try a
        # conservatively lower threshold (SIMILARITY_FLOOR_FALLBACK) rather than
        # failing the day outright.  The property-type hard gate (enforced inside
        # filter_similar_candidates) and price-sanity outlier rejection are both
        # retained.  Price band is skipped in fallback mode to avoid further
        # shrinking an already sparse pool.
        #
        # "strict"          — normal path, used when strict floor has ≥1 comp
        # "fallback_relaxed"— strict floor empty, fallback floor has ≥1 comp
        # "strict_empty"    — both floors empty, day will have no price
        if not above_floor and comps_scored:
            fallback_pool = [
                (c, s) for c, s in comps_scored
                if raw_sim_scores.get(id(c), 0.0) >= SIMILARITY_FLOOR_FALLBACK
            ]
            if fallback_pool:
                sample_scores = [
                    round(raw_sim_scores.get(id(c), 0.0), 3)
                    for c, _ in fallback_pool[:5]
                ]
                logger.info(
                    f"[day_query] {checkin_str}: strict floor ({SIMILARITY_FLOOR}) empty — "
                    f"fallback_relaxed: {len(fallback_pool)} comps survive "
                    f"floor={SIMILARITY_FLOOR_FALLBACK} "
                    f"(sample scores: {sample_scores})"
                )
                above_floor = fallback_pool
                selection_mode = "fallback_relaxed"
                _using_fallback = True
            else:
                logger.warning(
                    f"[day_query] {checkin_str}: fallback floor {SIMILARITY_FLOOR_FALLBACK} also "
                    f"empty — all {len(comps_scored)} comps below fallback floor; "
                    f"day will have no price"
                )
                selection_mode = "strict_empty"

        # Search-feed nightly prices can drift from PDP exact-night prices.
        # Revalidate top-ranked comps so comparable card prices stay accurate.
        if above_floor and DAY_QUERY_PDP_REVALIDATE_TOP_N > 0:
            _rv = _revalidate_top_comp_prices_with_pdp(
                client,
                above_floor,
                date_i=date_i,
                adults=adults,
                top_n=DAY_QUERY_PDP_REVALIDATE_TOP_N,
            )
            if _rv["updated"] > 0:
                logger.info(
                    "[day_query] %s: pdp revalidation updated=%s attempted=%s top_n=%s",
                    checkin_str,
                    _rv["updated"],
                    _rv["attempted"],
                    DAY_QUERY_PDP_REVALIDATE_TOP_N,
                )

        # ── Layer 1 Price Sanity ──────────────────────────────────
        # Applied after the similarity floor.  Severe price outliers
        # (nd > 4.0) are excluded from pricing entirely; mild outliers
        # (nd 2.5–4.0) are downweighted to 0.5× in the formula.
        # The full above_floor list is still used for top_comps display
        # so that users can see all candidates (outliers flagged).
        sanity_results, ps_excluded, ps_downweighted = apply_price_sanity(above_floor)

        # Comps accepted by price sanity (weight > 0).
        pricing_pool_pre_band = [(r.comp, r.sim_score) for r in sanity_results if r.weight > 0]
        ps_weights = build_price_sanity_weights(sanity_results)

        # Build a set of sanity-excluded comp ids for payload tagging below.
        excluded_ids = {id(r.comp) for r in sanity_results if r.weight == 0.0}

        if ps_excluded or ps_downweighted:
            logger.info(
                f"[day_query] {checkin_str}: price sanity — "
                f"excluded={ps_excluded} downweighted={ps_downweighted} "
                f"accepted={len(pricing_pool_pre_band)}"
            )

        # ── Phase 3B: Price-band filter (pricing only) ────────────
        # Applied to the pricing pool ONLY.  above_floor is kept intact so
        # that comparable display never shrinks to 1 when the whole market
        # sits above the anchor's ±30% band.
        #
        # Anchor priority: primary preferred comp card price → target price → majority band.
        #
        # Skipped entirely in fallback_relaxed mode — the pool is already
        # sparse and we cannot afford to narrow it further.
        price_band_excluded_count = 0
        price_band_anchor: Optional[float] = None
        _band_excluded_ids: set = set()
        if _using_fallback:
            # Fallback mode: retain all price-sanity-accepted comps; no band filter.
            pricing_pool = pricing_pool_pre_band
            logger.info(
                f"[day_query] {checkin_str}: price band skipped (fallback_relaxed, "
                f"pool={len(pricing_pool_pre_band)} comps)"
            )
        else:
            if pref_urls:
                for c in comps:
                    if c.url and any(comp_urls_match(c.url, pu) for pu in pref_urls):
                        if c.nightly_price and c.nightly_price > 0:
                            price_band_anchor = float(c.nightly_price)
                            break
            # Do not anchor price-band to target.nightly_price in daily comps mode.
            # Target PDP price can be promotional/base and become an overly-low anchor,
            # which can incorrectly exclude almost all valid market comps.
            # If no preferred-comp anchor is available, apply_price_band_filter()
            # will derive a majority band from the comp pool itself.
            try:
                pricing_pool, _pb_excluded, _pb_info = apply_price_band_filter(
                    pricing_pool_pre_band, price_band_anchor
                )
                price_band_excluded_count = len(_pb_excluded)
                _band_excluded_ids = {id(c) for c, _ in _pb_excluded}
                if price_band_excluded_count:
                    logger.info(
                        f"[day_query] {checkin_str}: price band "
                        f"({_pb_info['anchor_mode']}) "
                        f"${_pb_info.get('lower')}-${_pb_info.get('upper')} "
                        f"excluded={price_band_excluded_count} from pricing"
                    )
            except Exception as _pb_exc:
                logger.warning(f"[day_query] Price band filter failed (non-fatal): {_pb_exc}")
                pricing_pool = pricing_pool_pre_band

        # No new_listing_discount per-day — discount applied at calendar level
        rec_price, rec_debug = recommend_price(
            target,
            [c for c, _ in pricing_pool],
            top_k=top_k,
            new_listing_discount=0.0,
            preferred_comp_urls=pref_urls if pref_urls else None,
            price_sanity_weights=ps_weights,
        )

        prices = [c.nightly_price for c, _ in pricing_pool if c.nightly_price]
        comps_used = rec_debug.get("picked_n", 0)

        # Pricing confidence: low when using fallback pool, medium when few comps used.
        if selection_mode == "fallback_relaxed":
            pricing_confidence = "low"
        elif comps_used < 3:
            pricing_confidence = "medium"
        else:
            pricing_confidence = "high"

        # top_comps uses the full above_floor list for transparency.
        # Comps excluded by price sanity are tagged priceOutlier=True;
        # comps excluded by price band are tagged priceBandExcluded=True.
        top_comps_scored = above_floor[: min(max(1, top_k), len(above_floor))]
        # Build date-specific deep links using each comp's own queried stay length.
        top_comps = [
            (
                lambda _p: {
                    **_p,
                    "url": (
                        f"{(c.url or '').split('?')[0]}"
                        f"?check_in={checkin_str}&check_out="
                        f"{(date_i + timedelta(days=max(1, int(c.scrape_nights or 1)))).isoformat()}"
                        f"&adults={adults}"
                        if c.url else _p.get("url")
                    ),
                    **({"priceOutlier": True} if id(c) in excluded_ids else {}),
                    **({"priceBandExcluded": True} if id(c) in _band_excluded_ids else {}),
                }
            )(to_comparable_payload(c, s, target=target, include_geo=True))
            for c, s in top_comps_scored
        ]

        # Determine flags
        flags: List[str] = []
        # low_comp_confidence: always set for fallback_relaxed; also when pricing engine signals it.
        if selection_mode == "fallback_relaxed" or rec_debug.get("low_comp_confidence", False):
            flags.append("low_comp_confidence")
        if rec_price is not None and len(prices) >= 3:
            overall_median = statistics.median(prices)
            if rec_price > overall_median * 1.25:
                flags.append("peak")
            elif rec_price < overall_median * 0.75:
                flags.append("low_demand")

        # Price distribution
        dist = compute_price_distribution(prices)
        # Build comp_prices after potential PDP revalidation.
        all_comp_prices = build_comp_prices_dict(comps)

        logger.info(
            f"[day_query] {checkin_str}: comps={comps_collected} filtered={len(filtered_comps)} "
            f"below_floor={below_floor_count} band_excl={price_band_excluded_count} "
            f"used={comps_used} median=${dist['median']} "
            f"mode={selection_mode} confidence={pricing_confidence} "
            f"query_nights={query_nights_used}"
        )

        return DayResult(
            date=checkin_str,
            median_price=round(rec_price, 2) if rec_price else None,
            comps_collected=comps_collected,
            comps_used=comps_used,
            below_similarity_floor=below_floor_count + rec_debug.get("below_floor", 0),
            price_outliers_excluded=ps_excluded,
            price_outliers_downweighted=ps_downweighted,
            geo_excluded=geo_excluded_count,
            price_band_excluded=price_band_excluded_count,
            filter_stage=filter_debug.get("stage", "unknown"),
            flags=flags,
            is_sampled=True,
            is_weekend=is_weekend,
            price_distribution=dist,
            top_comps=top_comps,
            comp_prices=all_comp_prices,
            selection_mode=selection_mode,
            pricing_confidence=pricing_confidence,
        )

    except Exception as exc:
        logger.warning(f"[day_query] {checkin_str}: error: {exc}")
        return DayResult(
            date=checkin_str,
            filter_stage="error",
            flags=["missing_data"],
            is_sampled=True,
            is_weekend=is_weekend,
            error=str(exc)[:200],
        )


# ── Discount evidence (debug only) ──────────────────────────────

def detect_discount_evidence(
    client,
    base_origin: str,
    target: ListingSpec,
    start_date: str,
    end_date: str,
    adults: int,
    *,
    rate_limit_seconds: float = 1.0,
) -> Dict[str, Any]:
    """
    Run ONE full-stay query to detect discount signals.
    This is for debug/transparency only — NOT used for pricing.

    The prices extracted here are total-trip prices (the bug we're fixing).
    We divide by nights to show what the "per-night from total" would be.
    """
    try:
        _, search_data = client.search_listings_with_overrides(
            {
                "checkin": start_date,
                "checkout": end_date,
                "adults": adults,
                "query": target.location,
                "itemsPerGrid": 20,
            }
        )
        context = parse_search_listing_context(search_data)
        prices = [
            row.get("total_price") or row.get("nightly_price")
            for row in context.values()
            if isinstance(row.get("total_price") or row.get("nightly_price"), (int, float))
            and (row.get("total_price") or row.get("nightly_price")) > 0
        ]

        if not prices:
            return {
                "fullStayPricesRaw": [],
                "fullStayNights": 0,
                "perNightFromTotal": [],
                "avgPerNightFromTotal": None,
                "note": "No prices found in full-stay query",
                "error": None,
            }

        from datetime import datetime as dt
        d_start = dt.strptime(start_date, "%Y-%m-%d").date()
        d_end = dt.strptime(end_date, "%Y-%m-%d").date()
        nights = (d_end - d_start).days
        per_night = [round(p / nights, 2) for p in prices] if nights > 0 else prices

        return {
            "fullStayPricesRaw": prices[:10],
            "fullStayNights": nights,
            "perNightFromTotal": per_night[:10],
            "avgPerNightFromTotal": round(statistics.mean(per_night), 2) if per_night else None,
            "note": "Full-stay query for discount evidence only. These are total-trip prices divided by nights.",
            "error": None,
        }

    except Exception as exc:
        return {
            "fullStayPricesRaw": [],
            "fullStayNights": 0,
            "perNightFromTotal": [],
            "avgPerNightFromTotal": None,
            "note": None,
            "error": str(exc)[:200],
        }


# ── Interpolation ────────────────────────────────────────────────

def interpolate_missing_days(
    sampled_results: List[DayResult],
    all_nights: List[date],
) -> List[DayResult]:
    """
    Fill in unsampled/failed days by linear interpolation between nearest
    valid anchors.  Returns a complete list — one DayResult per night.
    """
    # Map of date-str -> DayResult for sampled days WITH valid prices
    valid_map: Dict[str, DayResult] = {}
    for r in sampled_results:
        if r.median_price is not None:
            valid_map[r.date] = r

    # Map of all sampled (including failed)
    all_sampled: Dict[str, DayResult] = {r.date: r for r in sampled_results}

    # Sorted anchors for interpolation
    anchors: List[Tuple[str, float]] = sorted(
        [(d, r.median_price) for d, r in valid_map.items()],
        key=lambda x: x[0],
    )

    result: List[DayResult] = []

    for night in all_nights:
        ds = night.isoformat()
        is_weekend = night.weekday() >= 4

        # Case 1: sampled with valid price — use as-is
        if ds in valid_map:
            result.append(valid_map[ds])
            continue

        # Case 2: needs interpolation
        interp_price = _interpolate(ds, anchors)

        flags = ["interpolated"]
        if ds in all_sampled:
            flags.append("missing_data")

        if interp_price is not None:
            result.append(DayResult(
                date=ds,
                median_price=round(interp_price, 2),
                filter_stage="interpolated",
                flags=flags,
                is_sampled=False,
                is_weekend=is_weekend,
            ))
        else:
            result.append(DayResult(
                date=ds,
                filter_stage="no_data",
                flags=["missing_data"],
                is_sampled=False,
                is_weekend=is_weekend,
                error="No valid anchors for interpolation",
            ))

    return result


def _interpolate(target_ds: str, anchors: List[Tuple[str, float]]) -> Optional[float]:
    """Linear interpolation between two nearest price anchors."""
    if not anchors:
        return None
    if len(anchors) == 1:
        return anchors[0][1]

    before: Optional[Tuple[str, float]] = None
    after: Optional[Tuple[str, float]] = None

    for ds, price in anchors:
        if ds <= target_ds:
            before = (ds, price)
        if ds >= target_ds and after is None:
            after = (ds, price)

    if before is None and after is not None:
        return after[1]
    if after is None and before is not None:
        return before[1]
    if before is None and after is None:
        return None

    if before[0] == after[0]:
        return before[1]

    b_date = date.fromisoformat(before[0])
    a_date = date.fromisoformat(after[0])
    t_date = date.fromisoformat(target_ds)

    span = (a_date - b_date).days
    offset = (t_date - b_date).days
    ratio = offset / span

    return before[1] + ratio * (after[1] - before[1])
