from __future__ import annotations

import logging
import math
import re
import os
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from worker.core.similarity import comp_urls_match
from worker.core.concurrent_runner import MAX_SCRAPER_WORKERS
from worker.scraper.browser_runtime import (
    build_warmed_browser_client_pool,
    close_browser_client_pool,
)
from worker.scraper.parsers import (
    parse_pdp_response,
    parse_search_listing_context,
    parse_search_response,
)
from worker.scraper.price_normalizer import normalize_raw_price
from worker.scraper.target_extractor import (
    ListingSpec,
    normalize_property_type,
    safe_domain_base,
)

logger = logging.getLogger("worker.scraper.comp_collection")
_ROOM_ID_RE = re.compile(r"/rooms/(\d+)")
_PDP_STRUCTURAL_CACHE_LOCK = threading.Lock()
_PDP_STRUCTURAL_CACHE: Dict[str, Tuple[Optional[int], Optional[float], str, List[str]]] = {}
_PDP_STRUCTURAL_CACHE_LOADED = False


def _pdp_structural_cache_path() -> str:
    configured = os.getenv("AIRBNB_PDP_STRUCTURAL_CACHE_PATH")
    if configured is not None:
        return str(configured).strip()
    worker_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(worker_dir, ".airbnb_pdp_structural_cache.json")


def _ensure_pdp_structural_cache_loaded_locked() -> None:
    global _PDP_STRUCTURAL_CACHE_LOADED
    if _PDP_STRUCTURAL_CACHE_LOADED:
        return
    _PDP_STRUCTURAL_CACHE_LOADED = True

    path = _pdp_structural_cache_path()
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:
        logger.info("[comp_collection] PDP structural cache load skipped: %s", exc)
        return
    if not isinstance(raw, dict):
        return
    for listing_id, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        sid = str(listing_id).strip()
        if not sid:
            continue
        accommodates = payload.get("accommodates")
        baths = payload.get("baths")
        property_type = payload.get("property_type")
        amenities = payload.get("amenities")
        _PDP_STRUCTURAL_CACHE[sid] = (
            int(accommodates) if isinstance(accommodates, (int, float)) and int(accommodates) > 0 else None,
            float(baths) if isinstance(baths, (int, float)) else None,
            str(property_type or ""),
            [str(a) for a in amenities if isinstance(a, str)] if isinstance(amenities, list) else [],
        )


def _save_pdp_structural_cache_locked() -> None:
    path = _pdp_structural_cache_path()
    if not path:
        return
    payload = {
        str(listing_id): {
            "accommodates": values[0],
            "baths": values[1],
            "property_type": values[2],
            "amenities": values[3],
        }
        for listing_id, values in _PDP_STRUCTURAL_CACHE.items()
    }
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.info("[comp_collection] PDP structural cache save skipped: %s", exc)


def _build_debug_search_url(base_origin: str, overrides: Dict[str, Any]) -> str:
    """
    Build a human-readable Airbnb search URL for logging/diagnostics.
    """
    base = safe_domain_base(str(base_origin or "https://www.airbnb.com")).rstrip("/")
    params: Dict[str, Any] = {}
    query = (
        overrides.get("query")
        or overrides.get("locationSearch")
        or overrides.get("location")
        or ""
    )
    if query:
        params["query"] = str(query)
    for key in (
        "checkin",
        "checkout",
        "adults",
        "guests",
        "itemsOffset",
        "itemsPerGrid",
        "searchByMap",
        "neLat",
        "neLng",
        "swLat",
        "swLng",
    ):
        val = overrides.get(key)
        if val is not None and str(val) != "":
            params[key] = val
    qs = urlencode(params, doseq=True)
    return f"{base}/s?{qs}" if qs else f"{base}/s"


def _compute_bbox_from_radius_km(
    center_lat: float,
    center_lng: float,
    radius_km: float,
) -> Tuple[float, float, float, float]:
    """Return (ne_lat, ne_lng, sw_lat, sw_lng) for a center/radius."""
    lat_delta = float(radius_km) / 111.32
    cos_lat = max(0.01, math.cos(math.radians(center_lat)))
    lng_delta = float(radius_km) / (111.32 * cos_lat)
    ne_lat = center_lat + lat_delta
    ne_lng = center_lng + lng_delta
    sw_lat = center_lat - lat_delta
    sw_lng = center_lng - lng_delta
    return ne_lat, ne_lng, sw_lat, sw_lng


def _matches_structural_filters(
    comp: ListingSpec,
    *,
    target_accommodates: Optional[int],
    target_bedrooms: Optional[int],
    target_beds: Optional[int],
    target_baths: Optional[float],
) -> bool:
    """
    Hard filter for comp eligibility.

    Current policy: exact guests/accommodates only.
    Bedrooms/beds/baths remain signals for similarity scoring, but are not
    used to exclude candidates from the comp pool.
    """
    if target_accommodates is not None:
        if comp.accommodates is None or int(comp.accommodates) != int(target_accommodates):
            return False
    return True


def _map_search_row_to_spec(
    listing_id: str,
    row: Dict[str, Any],
    base_origin: str,
    query_nights: int,
) -> ListingSpec:
    nightly = row.get("nightly_price")
    total = row.get("total_price")
    price_nights = int(row.get("price_nights") or 1)
    price_kind = "unknown"
    scrape_nights = max(1, int(query_nights or 1))
    query_total_price = None
    if isinstance(nightly, (int, float)) and nightly > 0:
        effective_nightly = float(nightly)
        price_kind = "nightly_from_search"
        if price_nights > 1:
            scrape_nights = max(scrape_nights, int(price_nights))
    elif isinstance(total, (int, float)) and total > 0:
        nights = max(1, price_nights)
        # If this card came from a 2-night query but the parser did not
        # capture "for 2 nights", force ÷2 normalization for trip totals.
        if int(query_nights or 1) == 2 and nights <= 1:
            nights = 2
        if int(query_nights or 1) == 1 and nights > 1:
            # Strict 1-night mode: reject multi-night totals to avoid 2-night bias.
            effective_nightly = None
            price_kind = "multi_night_total_skipped"
            scrape_nights = nights
        else:
            normalized = normalize_raw_price(
                total,
                qualifier="total",
                stay_nights=nights,
                source="search",
                produce_nightly_from_total=True,
            )
            effective_nightly = normalized.nightly_price if normalized is not None else None
            price_kind = "trip_total_from_search"
            scrape_nights = nights
            query_total_price = float(total)
    else:
        effective_nightly = None

    return ListingSpec(
        url=f"{base_origin.rstrip('/')}/rooms/{listing_id}",
        title=str(row.get("title") or ""),
        location=str(row.get("location") or ""),
        accommodates=row.get("accommodates"),
        bedrooms=row.get("bedrooms"),
        beds=row.get("beds"),
        baths=row.get("baths"),
        property_type=str(row.get("property_type") or ""),
        nightly_price=effective_nightly,
        query_total_price=query_total_price,
        currency=str(row.get("currency") or "USD"),
        rating=row.get("rating"),
        reviews=row.get("reviews"),
        amenities=list(row.get("amenities") or []),
        lat=row.get("lat"),
        lng=row.get("lng"),
        scrape_nights=scrape_nights,
        price_kind=price_kind,
    )


def _extract_listing_id_from_url(url: str) -> Optional[str]:
    if not isinstance(url, str) or not url:
        return None
    m = _ROOM_ID_RE.search(url)
    return m.group(1) if m else None


def _tabs_per_browser() -> int:
    try:
        value = int(os.getenv("AIRBNB_PLAYWRIGHT_MAX_TABS", "4"))
    except Exception:
        value = 4
    return max(1, min(value, 5))


def _enrich_comps_baths_and_property_type_from_pdp(
    client,
    comps: List[ListingSpec],
    *,
    checkin: str,
    checkout: str,
    adults: int,
) -> None:
    """
    Enrich comps with PDP fields from individual listing-detail fetches.
    Uses full PDP parsing so amenities are populated for each compset listing.
    """
    detail_fetcher = (
        getattr(client, "get_listing_details_fast", None)
        or getattr(client, "get_listing_details", None)
    )
    if not comps or detail_fetcher is None:
        return

    # Pre-load cache once before starting enrichment work
    with _PDP_STRUCTURAL_CACHE_LOCK:
        _ensure_pdp_structural_cache_loaded_locked()

    work_items: List[Tuple[int, ListingSpec, str]] = []
    for idx, comp in enumerate(comps):
        lid = _extract_listing_id_from_url(str(comp.url or ""))
        if not lid:
            continue
        work_items.append((idx, comp, lid))

    attempted = len(work_items)
    if attempted == 0:
        return

    max_workers = max(1, min(int(os.getenv("PDP_DETAIL_MAX_WORKERS", str(MAX_SCRAPER_WORKERS))), MAX_SCRAPER_WORKERS))
    effective_workers = min(max_workers, attempted)

    use_fast_detail = hasattr(client, "get_listing_details_fast")
    use_pool = (
        (not use_fast_detail)
        and hasattr(client, "config")
        and isinstance(getattr(client, "config", None), dict)
    )
    if use_fast_detail:
        browser_pool = [
            client.fork() if hasattr(client, "fork") else client
            for _ in range(effective_workers)
        ]
        browser_locks = [threading.BoundedSemaphore(_tabs_per_browser()) for _ in browser_pool]
    elif use_pool:
        browser_pool = build_warmed_browser_client_pool(
            base_config=dict(client.config),  # type: ignore[attr-defined]
            requested_size=effective_workers,
            pool_name="comp_pdp_enrichment",
        )
        browser_locks = [threading.BoundedSemaphore(_tabs_per_browser()) for _ in browser_pool]
    else:
        browser_pool = [client]
        browser_locks = [threading.BoundedSemaphore(_tabs_per_browser())]

    work_args: List[Dict[str, Any]] = []
    for i, item in enumerate(work_items):
        work_args.append(
            {
                "item": item,
                "browser_slot": i % len(browser_pool),
            }
        )

    cache_updates: Dict[str, Tuple[Optional[int], Optional[float], str, List[str]]] = {}

    def _fetch(query_arg: Dict[str, Any]) -> Tuple[int, Optional[int], Optional[float], str, List[str]]:
        idx, _comp, lid = query_arg["item"]
        cached = _PDP_STRUCTURAL_CACHE.get(str(lid))
        if cached is not None:
            a_val, b_val, p_val, amenities = cached
            return idx, a_val, b_val, p_val, list(amenities)

        browser_slot = int(query_arg["browser_slot"])
        slot_client = browser_pool[browser_slot]
        slot_lock = browser_locks[browser_slot]
        slot_fetcher = (
            getattr(slot_client, "get_listing_details_fast", None)
            or getattr(slot_client, "get_listing_details", None)
        )
        try:
            with slot_lock:
                pdp_data = slot_fetcher(
                    lid,
                    checkin=checkin,
                    checkout=checkout,
                    adults=adults,
                )
            parsed = parse_pdp_response(
                pdp_data,
                lid,
                safe_domain_base(str(_comp.url or "")),
            )
            baths = parsed.get("baths")
            ptype_raw = parsed.get("property_type")
            ptype_norm = normalize_property_type(str(ptype_raw or ""))
            accommodates = parsed.get("accommodates")
            a_val = int(accommodates) if isinstance(accommodates, (int, float)) and int(accommodates) > 0 else None
            b_val = float(baths) if isinstance(baths, (int, float)) else None
            p_val = ptype_norm.strip() if isinstance(ptype_norm, str) and ptype_norm.strip() else ""
            amenities = [a for a in (parsed.get("amenities") or []) if isinstance(a, str) and a.strip()]
            cache_updates[str(lid)] = (a_val, b_val, p_val, list(amenities))
            return idx, a_val, b_val, p_val, amenities
        except Exception:
            return idx, None, None, "", []

    updated = 0
    amenities_populated = 0
    try:
        with ThreadPoolExecutor(max_workers=effective_workers) as ex:
            futures = [ex.submit(_fetch, query_arg) for query_arg in work_args]
            for f in as_completed(futures):
                idx, a_val, b_val, p_val, amenities = f.result()
                comp = comps[idx]
                changed = False
                if isinstance(a_val, int):
                    if comp.accommodates != int(a_val):
                        comp.accommodates = int(a_val)
                        changed = True
                if isinstance(b_val, (int, float)):
                    if comp.baths != float(b_val):
                        comp.baths = float(b_val)
                        changed = True
                if p_val:
                    if comp.property_type != p_val:
                        comp.property_type = p_val
                        changed = True
                if amenities:
                    existing = set(str(a).strip() for a in (comp.amenities or []) if str(a).strip())
                    merged = list(comp.amenities or [])
                    for amenity in amenities:
                        if amenity not in existing:
                            merged.append(amenity)
                            existing.add(amenity)
                    if merged != list(comp.amenities or []):
                        comp.amenities = merged
                        changed = True
                    if comp.amenities:
                        amenities_populated += 1
                if changed:
                    updated += 1
    finally:
        if use_pool:
            close_browser_client_pool(browser_pool)  # type: ignore[arg-type]

    # Batch update cache and persist once
    if cache_updates:
        with _PDP_STRUCTURAL_CACHE_LOCK:
            _PDP_STRUCTURAL_CACHE.update(cache_updates)
            _save_pdp_structural_cache_locked()

    if attempted:
        logger.info(
            "[comp_collection] PDP enrichment attempted=%s updated=%s amenities_populated=%s workers=%s browsers=%s",
            attempted,
            updated,
            amenities_populated,
            max_workers,
            len(browser_pool),
        )


def collect_search_comps(
    client,
    search_location: str,
    base_origin: str,
    date_i: date,
    adults: int,
    *,
    max_scroll_rounds: int,
    max_cards: int,
    rate_limit_seconds: float,
    timeout_ms: int = 15000,
    exclude_url: Optional[str] = None,
    log_prefix: str = "search",
    page_offsets: Optional[List[int]] = None,
    center_lat: Optional[float] = None,
    center_lng: Optional[float] = None,
    map_radius_km: Optional[float] = None,
    target_accommodates: Optional[int] = None,
    target_bedrooms: Optional[int] = None,
    target_beds: Optional[int] = None,
    target_baths: Optional[float] = None,
    pdp_structural_enrichment: bool = False,
    pdp_structural_enrichment_limit: Optional[int] = None,
    prefer_two_night: bool = False,
    prefer_one_night: bool = False,
) -> Tuple[List[ListingSpec], int]:
    collect_start = time.perf_counter()
    base_origin = safe_domain_base(str(base_origin or "https://www.airbnb.com"))
    checkin_str = date_i.isoformat()
    page_size = max(1, int(max_cards))
    base_offsets = page_offsets or [0]
    # Daily path needs enough priced candidates for a 20-card display compset.
    # Keep scanning a small bounded page window instead of returning after the
    # first priced listing.
    deep_offsets = [i * page_size for i in range(1, max(0, int(max_scroll_rounds)) + 1)]
    offset_sets: List[List[int]] = [base_offsets]
    if page_offsets is None and deep_offsets:
        offset_sets.append(deep_offsets)
    fallback_unpriced_comps: List[ListingSpec] = []
    fallback_unpriced_query_nights: int = 1
    map_bounds: Optional[Tuple[float, float, float, float]] = None
    if (
        center_lat is not None
        and center_lng is not None
        and isinstance(map_radius_km, (int, float))
        and float(map_radius_km) > 0
    ):
        map_bounds = _compute_bbox_from_radius_km(
            float(center_lat),
            float(center_lng),
            float(map_radius_km),
        )

    # Default: 1-night primary, 2-night fallback.
    # Optional modes:
    #   - prefer_two_night=True  -> 2-night only
    #   - prefer_one_night=True  -> 1-night only
    if prefer_two_night:
        query_night_sequence = (2,)
    elif prefer_one_night:
        query_night_sequence = (1,)
    else:
        query_night_sequence = (1, 2)
    for query_nights in query_night_sequence:
        checkout_str = (date_i + timedelta(days=query_nights)).isoformat()
        last_reason = "unknown"
        carry_listing_ids: List[str] = []
        carry_context: Dict[str, Dict[str, Any]] = {}
        carry_seen_ids: set[str] = set()
        for offset_set_idx, offsets in enumerate(offset_sets):
            listing_ids: List[str] = list(carry_listing_ids)
            context: Dict[str, Dict[str, Any]] = dict(carry_context)
            seen_ids: set[str] = set(carry_seen_ids)
            page_ok = False

            for offset in offsets:
                search_adults = (
                    int(target_accommodates)
                    if target_accommodates is not None
                    else int(adults)
                )
                overrides = {
                    "checkin": checkin_str,
                    "checkout": checkout_str,
                    "adults": search_adults,
                    "dailySearch": True,
                    "itemsPerGrid": max_cards,
                }
                if str(search_location or "").strip():
                    overrides["query"] = search_location
                    overrides["locationSearch"] = search_location
                    overrides["location"] = search_location
                if center_lat is not None:
                    overrides["centerLat"] = center_lat
                if center_lng is not None:
                    overrides["centerLng"] = center_lng
                if map_bounds is not None:
                    ne_lat, ne_lng, sw_lat, sw_lng = map_bounds
                    overrides["searchByMap"] = True
                    overrides["neLat"] = ne_lat
                    overrides["neLng"] = ne_lng
                    overrides["swLat"] = sw_lat
                    overrides["swLng"] = sw_lng
                # Explicitly disable Guest Favorite filter for raw search requests.
                overrides["guestFavorite"] = False
                if target_accommodates is not None:
                    overrides["guests"] = int(target_accommodates)
                if offset > 0:
                    overrides["itemsOffset"] = offset
                debug_search_url = _build_debug_search_url(base_origin, overrides)
                logger.info(
                    "[%s] %s: query_nights=%s offset=%s",
                    log_prefix,
                    checkin_str,
                    query_nights,
                    offset,
                )
                logger.info(
                    "[%s] %s: search_url=%s",
                    log_prefix,
                    checkin_str,
                    debug_search_url,
                )
                status, search_data = client.search_listings_with_overrides(overrides)
                if status < 200 or status >= 300:
                    logger.warning(
                        "[%s] %s: search status=%s offset=%s",
                        log_prefix,
                        checkin_str,
                        status,
                        offset,
                    )
                    continue
                page_ok = True

                page_ids = parse_search_response(search_data)
                page_ctx = parse_search_listing_context(search_data)
                priced_page_rows = sum(
                    1
                    for row in page_ctx.values()
                    if (row.get("nightly_price") or 0) > 0
                    or (row.get("total_price") or 0) > 0
                )
                logger.info(
                    "[%s] %s: page_funnel query_nights=%s offset=%s "
                    "ids=%s context=%s priced_rows=%s elapsed_ms=%s",
                    log_prefix,
                    checkin_str,
                    query_nights,
                    offset,
                    len(page_ids),
                    len(page_ctx),
                    priced_page_rows,
                    round((time.perf_counter() - collect_start) * 1000),
                )
                for lid in page_ids:
                    sid = str(lid)
                    row = page_ctx.get(sid, {})
                    if sid not in seen_ids:
                        listing_ids.append(sid)
                        seen_ids.add(sid)
                        logger.info(
                            "[%s] %s: new comp listing id=%s rating=%s reviews=%s",
                            log_prefix,
                            checkin_str,
                            sid,
                            row.get("rating"),
                            row.get("reviews"),
                        )
                    existing = context.get(sid)
                    if existing is None:
                        context[sid] = row
                    else:
                        existing_has_price = bool(
                            (existing.get("nightly_price") or 0) > 0
                            or (existing.get("total_price") or 0) > 0
                        )
                        row_has_price = bool(
                            (row.get("nightly_price") or 0) > 0
                            or (row.get("total_price") or 0) > 0
                        )
                        if row_has_price and not existing_has_price:
                            context[sid] = row

            if not page_ok:
                continue

            if len(offsets) > 1:
                logger.info(
                    "[%s] %s: merged %s search results across %s offsets (query_nights=%s)",
                    log_prefix,
                    checkin_str,
                    len(listing_ids),
                    len(offsets),
                    query_nights,
                )

            parsed_comps: List[ListingSpec] = []
            for listing_id in listing_ids:
                row = context.get(str(listing_id), {})
                spec = _map_search_row_to_spec(str(listing_id), row, base_origin, query_nights)
                parsed_comps.append(spec)

            comps = parsed_comps
            rejected_log = []
            self_excluded = 0
            if exclude_url:
                new_comps = []
                for c in comps:
                    if c.url and comp_urls_match(c.url, exclude_url):
                        rejected_log.append(f"{(c.url or '').rsplit('/', 1)[-1]}: self-excluded")
                    else:
                        new_comps.append(c)
                self_excluded = len(comps) - len(new_comps)
                comps = new_comps
            if pdp_structural_enrichment and comps:
                if pdp_structural_enrichment_limit is not None:
                    comps = comps[: max(0, int(pdp_structural_enrichment_limit))]
                _enrich_comps_baths_and_property_type_from_pdp(
                    client,
                    comps,
                    checkin=checkin_str,
                    checkout=checkout_str,
                    adults=adults,
                )
            structural_excluded = 0
            if target_accommodates is not None:
                before = len(comps)
                new_comps = []
                for c in comps:
                    if _matches_structural_filters(
                        c,
                        target_accommodates=target_accommodates,
                        target_bedrooms=target_bedrooms,
                        target_beds=target_beds,
                        target_baths=target_baths,
                    ):
                        new_comps.append(c)
                    else:
                        rejected_log.append(f"{(c.url or '').rsplit('/', 1)[-1]}: structural-excluded (acc={c.accommodates})")
                comps = new_comps
                structural_excluded = before - len(comps)

            # Keep only listings with a positive nightly price AND marked available.
            priced: List[ListingSpec] = []
            unavailable_count = 0
            min_stay_blocked_count = 0
            no_price_count = 0
            for c in comps:
                lid = c.url.rsplit("/", 1)[-1] if c.url else ""
                row = context.get(str(lid), {})
                is_available = bool(row.get("is_available", True))
                min_nights = row.get("min_nights")
                if isinstance(min_nights, int) and min_nights > int(query_nights):
                    min_stay_blocked_count += 1
                    rejected_log.append(f"{lid}: min-stay-blocked")
                elif not is_available:
                    unavailable_count += 1
                    rejected_log.append(f"{lid}: unavailable")
                elif not bool(c.nightly_price and c.nightly_price > 0):
                    no_price_count += 1
                    rejected_log.append(f"{lid}: no-price")
                elif c.url:
                    priced.append(c)
            
            if rejected_log:
                logger.debug("[%s] %s: Filtered candidates: %s", log_prefix, checkin_str, ", ".join(rejected_log))

            logger.info(
                "[%s] %s: candidate_funnel query_nights=%s offsets=%s "
                "fetched=%s parsed=%s self_excluded=%s structural_excluded=%s "
                "unavailable=%s min_stay_blocked=%s no_price=%s priced=%s "
                "elapsed_ms=%s",
                log_prefix,
                checkin_str,
                query_nights,
                offsets,
                len(listing_ids),
                len(parsed_comps),
                self_excluded,
                structural_excluded,
                unavailable_count,
                min_stay_blocked_count,
                no_price_count,
                len(priced),
                round((time.perf_counter() - collect_start) * 1000),
            )

            priced_target = page_size
            enough_priced = len(priced) >= priced_target
            exhausted_offsets = offset_set_idx >= len(offset_sets) - 1
            explicit_offsets = page_offsets is not None
            if priced and (enough_priced or exhausted_offsets or explicit_offsets):
                if self_excluded > 0 or structural_excluded > 0:
                    logger.info(
                        "[%s] %s: exclusions self=%s structural=%s",
                        log_prefix,
                        checkin_str,
                        self_excluded,
                        structural_excluded,
                    )
                return priced, query_nights
            if priced:
                logger.info(
                    "[%s] %s: only %s priced comps from offsets=%s; "
                    "continuing deeper toward target=%s",
                    log_prefix,
                    checkin_str,
                    len(priced),
                    offsets,
                    priced_target,
                )
            if comps and not fallback_unpriced_comps:
                # Keep first non-empty candidate set even when prices are missing,
                # so downstream can still render a transparent report.
                fallback_unpriced_comps = comps
                fallback_unpriced_query_nights = query_nights

            reason = "unknown"
            if min_stay_blocked_count > 0:
                reason = "minimum_night_requirement_likely"
            elif unavailable_count > 0:
                reason = "sold_out_or_unavailable_likely"
            last_reason = reason

            # Sparse single-page daily query: retry once with deeper offsets.
            if (
                page_offsets is None
                and len(offsets) == 1
                and offset_set_idx == 0
                and len(offset_sets) > 1
            ):
                carry_listing_ids = list(listing_ids)
                carry_context = dict(context)
                carry_seen_ids = set(seen_ids)
                logger.info(
                    "[%s] %s: priced comps below target on offset=0; "
                    "retrying deeper offsets=%s (query_nights=%s, priced=%s, "
                    "target=%s, reason=%s)",
                    log_prefix,
                    checkin_str,
                    deep_offsets,
                    query_nights,
                    len(priced),
                    priced_target,
                    reason,
                )
                continue

            break

        if query_nights == 1 and len(query_night_sequence) > 1:
            logger.info(
                "[%s] %s: no priced comps from 1-night query; retrying 2-night fallback (reason=%s)",
                log_prefix,
                checkin_str,
                last_reason,
            )

    if fallback_unpriced_comps:
        logger.info(
            "[%s] %s: returning %s comps without nightly prices (query_nights=%s)",
            log_prefix,
            checkin_str,
            len(fallback_unpriced_comps),
            fallback_unpriced_query_nights,
        )
        return fallback_unpriced_comps, fallback_unpriced_query_nights

    return [], 1
