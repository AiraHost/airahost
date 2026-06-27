"""
Playwright-based Airbnb price estimator — orchestrator.

Connects to a local Chrome instance via CDP (Chrome DevTools Protocol)
to scrape target listing specs and nearby comparable listings.

Uses day-by-day 2-night-primary queries to get accurate nightly prices and
maximize comp pool coverage (minimum-stay=2 listings).  See day_query.py
for the rationale and per-night normalisation details.

This module orchestrates the pipeline by delegating to:
  - target_extractor: listing page spec extraction
  - comp_collection + parsers: HTTP search parsing and comp mapping
  - similarity: scoring & filtering
  - pricing_engine: price recommendation & transparent output
  - day_query: day-by-day 1-night query engine
"""

from __future__ import annotations

import logging
import os
import random
import re
import statistics
import time
import json
import itertools
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as dt, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from worker.core.concurrent_runner import execute_day_queries_concurrently
from worker.core.concurrent_runner import MAX_SCRAPER_WORKERS
from worker.core.comp_utils import build_comp_id
from worker.core.geo_filter import apply_geo_filter, haversine_km
from worker.core.similarity import (
    SIMILARITY_FLOOR,
    comp_urls_match,
    filter_similar_candidates,
    similarity_score,
)
from worker.scraper.airbnb_client import AirbnbClient
from worker.scraper.browser_runtime import (
    build_warmed_browser_client_pool,
    close_browser_client_pool,
)
from worker.scraper.parsers import (
    parse_pdp_response,
    parse_search_listing_context,
    parse_search_response,
)
from worker.scraper.comp_collection import (
    collect_search_comps,
    _enrich_comps_baths_and_property_type_from_pdp,
)
from worker.scraper.price_normalizer import normalize_raw_price
from worker.scraper.day_query import (
    DAY_MAX_CARDS,
    DAY_SCROLL_ROUNDS,
    DayResult,
    MAP_RADIUS_CAP_KM,
    MAX_NIGHTS,
    MAX_SAMPLE_QUERIES,
    PER_DAY_MAX_RETRIES,
    SAMPLE_THRESHOLD,
    SIMILARITY_FLOOR_FALLBACK,
    compute_sample_dates,
    daterange_nights,
    estimate_base_price_for_date,
    interpolate_missing_days,
)
from worker.scraper.target_extractor import (
    ListingSpec,
    extract_listing_id_from_url,
    extract_nightly_price_from_listing_page,
    extract_target_spec,
    location_looks_like_placeholder,
    normalize_airbnb_url,
    safe_domain_base,
)

logger = logging.getLogger("worker.scraper")
ROOM_ID_RE = re.compile(r"/rooms/(\d+)")

try:
    MIN_DAILY_COMPS_PER_DAY = int(os.getenv("MIN_DAILY_COMPS_PER_DAY", "10"))
except Exception:
    MIN_DAILY_COMPS_PER_DAY = 10
MIN_DAILY_COMPS_PER_DAY = max(1, MIN_DAILY_COMPS_PER_DAY)

try:
    FIXED_COMP_BASELINE_SIZE = int(os.getenv("FIXED_COMP_BASELINE_SIZE", "20"))
except Exception:
    FIXED_COMP_BASELINE_SIZE = 20
FIXED_COMP_BASELINE_SIZE = max(MIN_DAILY_COMPS_PER_DAY, FIXED_COMP_BASELINE_SIZE)

try:
    FIXED_COMP_POOL_STRIDE_DAYS = int(os.getenv("FIXED_COMP_POOL_STRIDE_DAYS", "7"))
except Exception:
    FIXED_COMP_POOL_STRIDE_DAYS = 7
FIXED_COMP_POOL_STRIDE_DAYS = max(1, FIXED_COMP_POOL_STRIDE_DAYS)

try:
    FIXED_COMP_POOL_GLOBAL_LIMIT = int(os.getenv("FIXED_COMP_POOL_GLOBAL_LIMIT", str(FIXED_COMP_BASELINE_SIZE)))
except Exception:
    FIXED_COMP_POOL_GLOBAL_LIMIT = FIXED_COMP_BASELINE_SIZE
FIXED_COMP_POOL_GLOBAL_LIMIT = max(FIXED_COMP_BASELINE_SIZE, FIXED_COMP_POOL_GLOBAL_LIMIT)

def _bounded_workers(env_name: str, default: int = 2) -> int:
    """Read worker count from env with defensive bounds."""
    raw = os.getenv(env_name)
    try:
        value = int(raw) if raw is not None else int(default)
    except (TypeError, ValueError):
        value = int(default)
    return max(1, min(value, MAX_SCRAPER_WORKERS))


def _tabs_per_browser() -> int:
    # Default tabs-per-browser to the worker cap so tab concurrency matches the
    # configured worker count; keep an 8-tab ceiling to limit anti-bot challenges.
    try:
        value = int(os.getenv("AIRBNB_PLAYWRIGHT_MAX_TABS", str(MAX_SCRAPER_WORKERS)))
    except Exception:
        value = MAX_SCRAPER_WORKERS
    return max(1, min(value, 8))


def _build_browser_pool(
    *,
    base_config: Dict[str, Any],
    requested_workers: int,
    total_tasks: int,
    pool_name: str,
) -> Tuple[List[AirbnbClient], List[Any]]:
    desired_size = max(1, min(int(requested_workers), int(total_tasks) if total_tasks > 0 else 1))
    pool = build_warmed_browser_client_pool(
        base_config=base_config,
        requested_size=desired_size,
        pool_name=pool_name,
    )
    return pool, [threading.BoundedSemaphore(_tabs_per_browser()) for _ in pool]


def _title_looks_suspicious(title: str) -> bool:
    t = (title or "").strip()
    if len(t) < 8:
        return True
    if re.fullmatch(r"(?i)(top guest favorite|guest favorite|superhost|rare find|new|show price breakdown)", t):
        return True
    if re.fullmatch(r"(?i)[A-Za-z]{3,9}\s+\d{1,2}\s+to\s+\d{1,2}", t):
        return True
    if re.fullmatch(
        r"(?i)(?:jan|feb|mar|apr|may|jun|june|jul|july|aug|sep|sept|oct|nov|dec)\s+\d{1,2}\s+to\s+"
        r"(?:jan|feb|mar|apr|may|jun|june|jul|july|aug|sep|sept|oct|nov|dec)\s+\d{1,2}",
        t,
    ):
        return True
    if re.search(r"(?i)\b(show price breakdown|price breakdown|reserve|check in|check out)\b", t):
        return True
    if re.search(r"(?i)\b(?:may|jun|june|jul|july|aug|sep|sept|oct|nov|dec|jan|feb|mar|apr)\s+\d{1,2}\s+to\s+\d{1,2}\b", t):
        return True
    return False


def _looks_non_location_placeholder(value: str) -> bool:
    # Thin wrapper around the shared helper so existing call sites keep working.
    return location_looks_like_placeholder(value)


def _coords_search_query(lat: Optional[float], lng: Optional[float]) -> str:
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        return f"{float(lat):.5f},{float(lng):.5f}"
    return ""


def _resolve_target_search_location(target: ListingSpec) -> str:
    coords_query = _coords_search_query(target.lat, target.lng)
    if coords_query:
        return coords_query
    city = str(target.city or "").strip()
    state = str(target.state or "").strip()
    if city and not _looks_non_location_placeholder(city):
        if state and not _looks_non_location_placeholder(state):
            return f"{city}, {state}"
        return city
    location = str(target.location or "").strip()
    if location and not _looks_non_location_placeholder(location):
        return location
    # Only a listing-title placeholder is available — return "" so the search is
    # anchored by coordinates instead of firing a junk title query.
    if location:
        logger.info(
            "[fixed_pool] dropping placeholder search_location=%r; relying on coords/geocode",
            location,
        )
    return ""


def _repair_suspicious_comparable_titles(
    client,
    transparent_result: Dict[str, Any],
    extraction_warnings: List[str],
    limit: int = 8,
) -> None:
    listings = transparent_result.get("comparableListings")
    if not isinstance(listings, list) or not listings:
        return

    candidates: List[Tuple[Dict[str, Any], str]] = []
    for item in listings:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = normalize_airbnb_url(str(item.get("url") or "").strip())
        if not url or not _title_looks_suspicious(title):
            continue
        candidates.append((item, url))
    if not candidates:
        return

    worker_count = min(_bounded_workers("DAY_QUERY_MAX_WORKERS", 12), len(candidates))
    logger.info(
        "[comp_title] repair start candidates=%s workers=%s limit=%s",
        len(candidates),
        worker_count,
        limit,
    )

    use_pool = hasattr(client, "config") and isinstance(getattr(client, "config", None), dict)
    if use_pool:
        repair_pool, repair_locks = _build_browser_pool(
            base_config=dict(client.config),  # type: ignore[attr-defined]
            requested_workers=worker_count,
            total_tasks=len(candidates),
            pool_name="comp_title_repair",
        )
    else:
        repair_pool = [client]
        repair_locks = [threading.BoundedSemaphore(_tabs_per_browser())]

    query_args = [
        {"item_url": url, "browser_slot": i % len(repair_pool)}
        for i, (_item, url) in enumerate(candidates)
    ]

    def _fetch_title(query_arg: Dict[str, Any]):
        item_url = str(query_arg["item_url"])
        browser_slot = int(query_arg["browser_slot"])
        slot_client = repair_pool[browser_slot]
        slot_lock = repair_locks[browser_slot]
        with slot_lock:
            spec, title_warnings = extract_target_spec(slot_client, item_url)
        return item_url, spec, title_warnings

    repaired = 0
    try:
        with ThreadPoolExecutor(max_workers=worker_count) as ex:
            future_map = {
                ex.submit(_fetch_title, query_arg): (item, url)
                for (item, url), query_arg in zip(candidates, query_args)
            }
            for future in as_completed(future_map):
                item, url = future_map[future]
                try:
                    _, spec, title_warnings = future.result()
                except Exception as exc:
                    extraction_warnings.append(f"Comparable title repair failed for {url}: {exc}")
                    continue
                extraction_warnings.extend(title_warnings)
                if repaired >= limit:
                    continue
                resolved_title = spec.title
                if resolved_title and not _title_looks_suspicious(resolved_title):
                    item["title"] = resolved_title
                    repaired += 1
                    logger.info(f"[comp_title] repaired title for {url} -> {resolved_title!r}")
    finally:
        if use_pool:
            close_browser_client_pool(repair_pool)  # type: ignore[arg-type]


def _repair_incomplete_comparable_specs(
    client,
    transparent_result: Dict[str, Any],
    extraction_warnings: List[str],
    limit: int = 6,
) -> None:
    """Backfill comp metadata from listing pages when search-card fields are incomplete."""
    listings = transparent_result.get("comparableListings")
    if not isinstance(listings, list) or not listings:
        return

    candidates: List[Tuple[Dict[str, Any], str]] = []
    for item in listings:
        if not isinstance(item, dict):
            continue
        url = normalize_airbnb_url(str(item.get("url") or "").strip())
        if not url:
            continue
        needs_repair = any(
            item.get(key) in (None, "", 0)
            for key in ("accommodates", "bedrooms", "baths")
        ) or not str(item.get("location") or "").strip()
        if not needs_repair and not _title_looks_suspicious(str(item.get("title") or "")):
            continue
        candidates.append((item, url))
    if not candidates:
        return

    worker_count = min(_bounded_workers("DAY_QUERY_MAX_WORKERS", 12), len(candidates))
    logger.info(
        "[comp_spec] repair start candidates=%s workers=%s limit=%s",
        len(candidates),
        worker_count,
        limit,
    )

    use_pool = hasattr(client, "config") and isinstance(getattr(client, "config", None), dict)
    if use_pool:
        repair_pool, repair_locks = _build_browser_pool(
            base_config=dict(client.config),  # type: ignore[attr-defined]
            requested_workers=worker_count,
            total_tasks=len(candidates),
            pool_name="comp_spec_repair",
        )
    else:
        repair_pool = [client]
        repair_locks = [threading.BoundedSemaphore(_tabs_per_browser())]

    query_args = [
        {"item_url": url, "browser_slot": i % len(repair_pool)}
        for i, (_item, url) in enumerate(candidates)
    ]

    def _fetch_spec(query_arg: Dict[str, Any]):
        item_url = str(query_arg["item_url"])
        browser_slot = int(query_arg["browser_slot"])
        slot_client = repair_pool[browser_slot]
        slot_lock = repair_locks[browser_slot]
        with slot_lock:
            spec, warnings = extract_target_spec(slot_client, item_url)
        return item_url, spec, warnings

    repaired = 0
    try:
        with ThreadPoolExecutor(max_workers=worker_count) as ex:
            future_map = {
                ex.submit(_fetch_spec, query_arg): (item, url)
                for (item, url), query_arg in zip(candidates, query_args)
            }
            for future in as_completed(future_map):
                item, url = future_map[future]
                try:
                    _, spec, warnings = future.result()
                except Exception as exc:
                    extraction_warnings.append(f"Comparable spec repair failed for {url}: {exc}")
                    continue
                extraction_warnings.extend(warnings)
                if repaired >= limit:
                    continue

                if spec.title and _title_looks_suspicious(str(item.get("title") or "")):
                    item["title"] = spec.title
                if spec.property_type:
                    item["propertyType"] = spec.property_type
                if isinstance(spec.accommodates, (int, float)):
                    item["accommodates"] = int(spec.accommodates)
                if isinstance(spec.bedrooms, (int, float)):
                    item["bedrooms"] = int(spec.bedrooms)
                if isinstance(spec.baths, (int, float)):
                    item["baths"] = round(float(spec.baths), 1)
                if spec.location:
                    item["location"] = spec.location
                if isinstance(spec.rating, (int, float)) and item.get("rating") is None:
                    item["rating"] = round(float(spec.rating), 2)
                if isinstance(spec.reviews, (int, float)) and item.get("reviews") is None:
                    item["reviews"] = int(spec.reviews)

                repaired += 1
                logger.info(
                    "[comp_spec] repaired %s -> accommodates=%s bedrooms=%s baths=%s location=%r",
                    url,
                    item.get("accommodates"),
                    item.get("bedrooms"),
                    item.get("baths"),
                    item.get("location"),
                )
    finally:
        if use_pool:
            close_browser_client_pool(repair_pool)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Helper: assemble transparent result from day-by-day data
# ---------------------------------------------------------------------------


def _build_daily_transparent_result(
    target: ListingSpec,
    query_criteria: Dict[str, Any],
    all_day_results: List[Dict[str, Any]],
    timings_ms: Dict[str, int],
    source: str,
    extraction_warnings: List[str],
    discount_evidence: Optional[Dict[str, Any]] = None,
    benchmark_info: Optional[Dict[str, Any]] = None,
    target_price_confidence: Optional[str] = None,
    spec_backfill: Optional[Dict[str, Any]] = None,
    spec_extraction_meta: Optional[Dict[str, Any]] = None,
    fixed_comp_pool: Optional[Dict[str, Dict[str, Any]]] = None,
    excluded_room_ids: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """
    Assemble the unified transparent result dict from day-by-day results.

    This replaces build_transparent_result for the day-by-day pipeline,
    aggregating per-day stats into the standard output shape.
    """
    # Aggregate prices across all days with valid medians
    valid_prices = [
        r["median_price"] for r in all_day_results
        if r.get("median_price") is not None
    ]

    sampled_days = sum(1 for r in all_day_results if r.get("is_sampled", False))
    interpolated_days = sum(
        1 for r in all_day_results
        if not r.get("is_sampled", False) and r.get("median_price") is not None
    )
    missing_days = sum(
        1 for r in all_day_results if r.get("median_price") is None
    )

    total_comps_collected = sum(r.get("comps_collected", 0) for r in all_day_results)
    total_comps_used = sum(r.get("comps_used", 0) for r in all_day_results)
    total_below_floor = sum(r.get("below_similarity_floor", 0) for r in all_day_results)
    low_comp_confidence_days = sum(
        1 for r in all_day_results
        if "low_comp_confidence" in (r.get("flags") or [])
    )
    fallback_relaxed_days = sum(
        1 for r in all_day_results if r.get("selection_mode") == "fallback_relaxed"
    )

    comparable_index: Dict[str, Dict[str, Any]] = {}

    def _prefer_better_comp_value(current: Any, candidate: Any) -> Any:
        """Keep richer comp metadata when the same listing appears across days."""
        if candidate is None:
            return current
        if isinstance(candidate, str):
            cand = candidate.strip()
            if not cand:
                return current
            cur = current.strip() if isinstance(current, str) else ""
            return candidate if not cur else current
        return candidate if current is None else current

    def _canonical_airbnb_room_url(raw_url: Any) -> Optional[str]:
        s = str(raw_url or "").strip()
        if not s:
            return None
        m = re.search(r"(https?://[^/?#]+)?/rooms/(\d+)", s)
        if not m:
            return s
        origin = (m.group(1) or "https://www.airbnb.com").rstrip("/")
        return f"{origin}/rooms/{m.group(2)}"

    comp_seen_dates: Dict[str, set[str]] = {}
    pending_comp_price_by_date: Dict[str, Dict[str, float]] = {}
    pending_comp_price_details: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _fixed_pool_price(pool_comp: Dict[str, Any]) -> Optional[float]:
        for key in ("nightly_price", "nightlyPrice", "last_nightly_price"):
            value = pool_comp.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return round(float(value), 2)
        return None

    for day_result in all_day_results:
        day_date = day_result.get("date")
        day_comp_prices: Dict[str, float] = day_result.get("comp_prices") or {}
        reused_comp_price_ids = set(str(cid) for cid in (day_result.get("reused_comp_price_ids") or []))
        day_has_valid_sample = bool(
            day_result.get("is_sampled", False)
            and isinstance(day_result.get("median_price"), (int, float))
        )
        seen_top_comp_ids: set[str] = set()

        for comp in day_result.get("top_comps", []) or []:
            comp_id = str(comp.get("id") or comp.get("url") or "").strip()
            if not comp_id:
                continue
            # Safety net: defense in depth against any day-query path that
            # forgot to filter excluded_room_ids upstream. Primary filter is
            # in day_query.estimate_base_price_for_date before pricing math.
            # Filter BEFORE seen_top_comp_ids so the dedup set in the second
            # loop also rejects this comp via the per-loop filter below.
            if excluded_room_ids and comp_id in excluded_room_ids:
                continue
            seen_top_comp_ids.add(comp_id)
            score = float(comp.get("similarity") or 0.0)
            # Prefer comp_prices for the day's price; fall back to top_comps value.
            price = day_comp_prices.get(comp_id) or comp.get("nightlyPrice")
            qn = int(comp.get("queryNights") or 1)
            if comp_id not in comparable_index:
                comparable_index[comp_id] = {
                    "item": dict(comp),
                    "score_sum": score,
                    "count": 1,
                    "price_by_date": {},
                    "price_by_date_details": {},
                    "max_query_nights": qn,
                }
                if comp_id in pending_comp_price_by_date:
                    comparable_index[comp_id]["price_by_date"].update(pending_comp_price_by_date[comp_id])
                    comparable_index[comp_id]["price_by_date_details"].update(
                        pending_comp_price_details.get(comp_id, {})
                    )
            else:
                item = comparable_index[comp_id]["item"]
                for key in ("title", "propertyType", "location", "url"):
                    item[key] = _prefer_better_comp_value(item.get(key), comp.get(key))
                for key in ("accommodates", "bedrooms", "baths", "rating", "reviews"):
                    item[key] = _prefer_better_comp_value(item.get(key), comp.get(key))
                comparable_index[comp_id]["score_sum"] += score
                comparable_index[comp_id]["count"] += 1
                if qn > comparable_index[comp_id]["max_query_nights"]:
                    comparable_index[comp_id]["max_query_nights"] = qn
            if day_has_valid_sample and day_date and isinstance(price, (int, float)) and price > 0:
                _price_rounded = round(float(price), 2)
                comparable_index[comp_id]["price_by_date"][day_date] = _price_rounded
                day_detail: Dict[str, Any] = {"price": _price_rounded}
                _day_url = str(comp.get("url") or "").strip()
                if _day_url:
                    day_detail["url"] = _day_url
                if qn > 1:
                    day_detail["queryNights"] = qn
                _day_total = comp.get("queryTotalPrice")
                if isinstance(_day_total, (int, float)) and _day_total > 0:
                    day_detail["queryTotalPrice"] = round(float(_day_total), 2)
                comparable_index[comp_id]["price_by_date_details"][day_date] = day_detail
            if day_date:
                comp_seen_dates.setdefault(comp_id, set()).add(day_date)

        for comp_id_raw, price in day_comp_prices.items():
            comp_id = str(comp_id_raw or "").strip()
            if not comp_id or comp_id in seen_top_comp_ids:
                continue
            # Safety net mirror: this hydrate-from-pool path can reach
            # comparable_index without going through top_comps, so the
            # excluded filter must apply here too.
            if excluded_room_ids and comp_id in excluded_room_ids:
                continue
            pool_comp = (fixed_comp_pool or {}).get(comp_id) or {}
            # Do not create anonymous comparables from fallback pool hydration.
            # These appear in UI as 0% rows with missing title/url.
            _pool_url = str(pool_comp.get("url") or "").strip()
            _pool_title = str(pool_comp.get("title") or "").strip()
            if not _pool_url and not _pool_title:
                if day_has_valid_sample and day_date and isinstance(price, (int, float)) and price > 0:
                    _price_rounded = round(float(price), 2)
                    pending_comp_price_by_date.setdefault(comp_id, {})[day_date] = _price_rounded
                    pending_comp_price_details.setdefault(comp_id, {})[day_date] = {"price": _price_rounded}
                continue
            pool_item = {
                "id": comp_id,
                "title": _pool_title or None,
                "propertyType": pool_comp.get("propertyType") or pool_comp.get("property_type"),
                "nightlyPrice": price,
                "similarity": pool_comp.get("similarity") or 0.0,
                "url": _pool_url or None,
            }
            for key in ("accommodates", "bedrooms", "beds", "baths", "rating", "reviews", "location", "amenities"):
                if pool_comp.get(key) is not None:
                    pool_item[key] = pool_comp.get(key)
            if comp_id not in comparable_index:
                comparable_index[comp_id] = {
                    "item": pool_item,
                    "score_sum": float(pool_comp.get("similarity") or 0.0),
                    "count": 1 if pool_comp.get("similarity") is not None else 0,
                    "price_by_date": {},
                    "price_by_date_details": {},
                    "max_query_nights": int(pool_comp.get("queryNights") or 1),
                }
                if comp_id in pending_comp_price_by_date:
                    comparable_index[comp_id]["price_by_date"].update(pending_comp_price_by_date[comp_id])
                    comparable_index[comp_id]["price_by_date_details"].update(
                        pending_comp_price_details.get(comp_id, {})
                    )
            if day_has_valid_sample and day_date and isinstance(price, (int, float)) and price > 0:
                _price_rounded = round(float(price), 2)
                comparable_index[comp_id]["price_by_date"][day_date] = _price_rounded
                day_detail: Dict[str, Any] = {"price": _price_rounded}
                if comp_id in reused_comp_price_ids:
                    day_detail["source"] = "fixed_pool_reuse"
                    day_detail["reused"] = True
                comparable_index[comp_id]["price_by_date_details"][day_date] = day_detail

                # Append to top_comps so it shows up in the day's tooltip/list
                if isinstance(day_result.get("top_comps"), list):
                    item_to_add = dict(comparable_index[comp_id]["item"])
                    item_to_add["nightlyPrice"] = _price_rounded
                    if "reused" in day_detail:
                        item_to_add["reused"] = True
                    day_result["top_comps"].append(item_to_add)

            if day_date:
                comp_seen_dates.setdefault(comp_id, set()).add(day_date)

    comparable_listings: List[Dict[str, Any]] = []
    for state in comparable_index.values():
        item = dict(state["item"])
        seen_days = comp_seen_dates.get(str(item.get("id") or ""), set())
        # Similarity should not decay by number of observed days.
        score_count = int(state.get("count") or 0)
        avg_score = state["score_sum"] / max(1, score_count)
        # Surface comps down to the fallback floor — comps used in fallback_relaxed
        # days have scores between SIMILARITY_FLOOR_FALLBACK and SIMILARITY_FLOOR
        # and should still appear in the comparable listings display.
        has_any_price = bool(state.get("price_by_date"))
        if avg_score < SIMILARITY_FLOOR_FALLBACK and not has_any_price:
            continue
        item["similarity"] = round(avg_score, 3)
        item["usedInPricingDays"] = score_count
        price_by_date = state.get("price_by_date", {})
        if price_by_date:
            item["priceByDate"] = price_by_date
        price_by_date_details = state.get("price_by_date_details", {})
        if isinstance(price_by_date_details, dict) and price_by_date_details:
            item["priceByDateDetails"] = price_by_date_details
        _canonical_url = _canonical_airbnb_room_url(item.get("url"))
        if _canonical_url:
            item["url"] = _canonical_url
        max_qn = state.get("max_query_nights", 1)
        if max_qn > 1:
            item["queryNights"] = max_qn
        elif "queryNights" in item:
            del item["queryNights"]
        comparable_listings.append(item)
    # Sort: listings with any priced dates first, then similarity DESC,
    # then price coverage breadth, then reviews.
    comparable_listings.sort(
        key=lambda row: (
            -int(bool(row.get("priceByDate"))),
            -float(row.get("similarity") or 0.0),
            -len(row.get("priceByDate") or {}),
            -int(row.get("reviews") or 0),
        )
    )
    # Cap the comparable set at the global comp limit (priority #3: "total 20
    # comp listings"). The fixed pool is already bounded, but sparse-day dynamic
    # fallback can add extras; keep the best-ranked ones (priced + most similar).
    if len(comparable_listings) > FIXED_COMP_POOL_GLOBAL_LIMIT:
        comparable_listings = comparable_listings[:FIXED_COMP_POOL_GLOBAL_LIMIT]

    # Price distribution from aggregated daily medians
    price_dist: Dict[str, Any] = {
        "min": None, "p25": None, "median": None, "p75": None, "max": None,
        "currency": target.currency or "USD",
    }

    if valid_prices:
        price_dist["min"] = round(min(valid_prices), 2)
        price_dist["max"] = round(max(valid_prices), 2)
        price_dist["median"] = round(statistics.median(valid_prices), 2)
        if len(valid_prices) >= 4:
            q = statistics.quantiles(valid_prices, n=4)
            price_dist["p25"] = round(q[0], 2)
            price_dist["p75"] = round(q[2], 2)

    # Weekday vs weekend estimates from actual data
    weekday_prices = [
        r["median_price"] for r in all_day_results
        if r.get("median_price") is not None and not r.get("is_weekend", False)
    ]
    weekend_prices = [
        r["median_price"] for r in all_day_results
        if r.get("median_price") is not None and r.get("is_weekend", False)
    ]

    overall_median = price_dist["median"]
    weekday_est = round(statistics.median(weekday_prices)) if weekday_prices else (round(overall_median) if overall_median else None)
    weekend_est = round(statistics.median(weekend_prices)) if weekend_prices else (round(overall_median) if overall_median else None)

    unique_collected_comp_ids: set[str] = set(str(cid) for cid in comparable_index.keys() if cid)

    unique_filtered_comp_count = sum(
        1 for row in comparable_listings if isinstance(row.get("priceByDate"), dict) and bool(row.get("priceByDate"))
    )
    unique_used_for_pricing_count = sum(
        1 for row in comparable_listings if int(row.get("usedInPricingDays") or 0) > 0
    )
    summary_collected = len(unique_collected_comp_ids)
    summary_after_filtering = unique_filtered_comp_count
    summary_used_for_pricing = unique_used_for_pricing_count
    if fixed_comp_pool is not None:
        summary_collected = int(query_criteria.get("fixedCompPoolCollectedTotal") or summary_collected)
        summary_after_filtering = int(query_criteria.get("fixedCompPoolFilteredTotal") or summary_after_filtering)
        summary_used_for_pricing = int(query_criteria.get("fixedCompPoolSize") or summary_used_for_pricing)

    return {
        "targetSpec": {
            "title": target.title or "",
            "location": target.location or "",
            "city": target.city or "",
            "state": target.state or "",
            "postalCode": target.postal_code or "",
            "country": target.country or "",
            "countryCode": target.country_code or "",
            "lat": target.lat,
            "lng": target.lng,
            "propertyType": target.property_type or "",
            "accommodates": target.accommodates,
            "bedrooms": target.bedrooms,
            "beds": target.beds,
            "baths": target.baths,
            "amenities": target.amenities or [],
            "rating": target.rating,
            "reviews": target.reviews,
            "nightlyPrice": target.nightly_price,
            "currency": target.currency or "USD",
            "priceConfidence": target_price_confidence,
            # Indicates whether spec fields were backfilled from saved attributes.
            # "live" = fully extracted; "mixed" = some fields from fallback; "partial" = still incomplete
            "specSource": (
                "live" if not spec_backfill or not spec_backfill.get("fields_filled")
                else ("partial" if spec_backfill.get("is_partial") else "mixed")
            ),
            "specFieldsBackfilled": (spec_backfill or {}).get("fields_filled") or [],
            "specFieldsMissing": (spec_backfill or {}).get("fields_still_missing") or [],
        },
        "queryCriteria": query_criteria,
        "compsSummary": {
            # Report-level counts should represent unique comparable listings,
            # not day-summed totals (which can look inflated, e.g. 30 days * N comps).
            "collected": summary_collected,
            "afterFiltering": summary_after_filtering,
            "usedForPricing": summary_used_for_pricing,
            "filterStage": "day_by_day",
            "topSimilarity": None,
            "avgSimilarity": None,
            "sampledDays": sampled_days,
            "interpolatedDays": interpolated_days,
            "missingDays": missing_days,
            "belowSimilarityFloor": total_below_floor,
            "filterFloor": SIMILARITY_FLOOR,
            "fallbackFloor": SIMILARITY_FLOOR_FALLBACK,
            "lowCompConfidenceDays": low_comp_confidence_days,
            "fallbackRelaxedDays": fallback_relaxed_days,
            # Keep day-aggregated totals for diagnostics/debugging.
            "dailyTotals": {
                "collected": total_comps_collected,
                "usedForPricing": total_comps_used,
            },
        },
        "priceDistribution": price_dist,
        "recommendedPrice": {
            "nightly": overall_median,
            "weekdayEstimate": weekday_est,
            "weekendEstimate": weekend_est,
            "discountApplied": 0.0,
            "notes": "",
        },
        "comparableListings": comparable_listings,
        "benchmarkInfo": benchmark_info,
        "debug": {
            "source": source,
            "extractionWarnings": extraction_warnings,
            "timingsMs": timings_ms,
            "pipelineVersion": "day-by-day-v1",
            "discountEvidence": discount_evidence,
            "specBackfill": spec_backfill,
            "specExtractionMeta": spec_extraction_meta,
            "dayQueryStats": {
                "totalNights": len(all_day_results),
                "sampled": sampled_days,
                "interpolated": interpolated_days,
                "missing": missing_days,
                "validPriceCount": len(valid_prices),
            },
        },
    }


def _build_fixed_comp_pool(
    client,
    target: ListingSpec,
    base_origin: str,
    anchor_date,
    adults: int,
    *,
    max_scroll_rounds: int,
    max_cards: int,
    rate_limit_seconds: float,
    max_radius_km: Optional[float],
    pool_size: int = 20,
    page_count: int = 1,
    return_meta: bool = False,
) -> Any:
    """
    Build a fixed comparable pool once, then reuse across all sampled dates.
    """
    search_location = _resolve_target_search_location(target)
    _pages = max(1, int(page_count))
    _page_offsets = [i * max(1, int(max_cards)) for i in range(_pages)]
    query_center_lat = float(target.lat) if isinstance(target.lat, (int, float)) else None
    query_center_lng = float(target.lng) if isinstance(target.lng, (int, float)) else None
    if query_center_lat is None or query_center_lng is None:
        try:
            from worker.core.geocode_details import geocode_address_details
            geocoded = geocode_address_details(str(search_location), timeout=3)
            if geocoded and geocoded.get("lat") is not None and geocoded.get("lng") is not None:
                city = str(geocoded.get("city") or "")
                display_name = str(geocoded.get("display_name") or "")
                if (
                    "county" in city.casefold()
                    and "county" not in str(search_location or "").casefold()
                ):
                    logger.info(
                        "[fixed_pool] map center skipped: search_location=%r resolved to county-level geocode %r",
                        search_location,
                        display_name or city,
                    )
                else:
                    query_center_lat = float(geocoded["lat"])
                    query_center_lng = float(geocoded["lng"])
                    logger.info(
                        "[fixed_pool] map center geocoded from search_location=%r -> (%.6f, %.6f)",
                        search_location,
                        query_center_lat,
                        query_center_lng,
                    )
        except Exception as exc:
            logger.info(f"[fixed_pool] map center geocode skipped/failed: {exc}")
    map_radius_limit_km = MAP_RADIUS_CAP_KM
    if isinstance(max_radius_km, (int, float)) and float(max_radius_km) > 0:
        map_radius_limit_km = min(float(max_radius_km), MAP_RADIUS_CAP_KM)
    fixed_search_cards = max(
        max(1, int(pool_size)),
        max(30, int(max_cards)),
    )

    def _collect_at_radius(radius_km: float, label_suffix: str = "") -> Tuple[List[ListingSpec], List[ListingSpec]]:
        radius_arg = radius_km if query_center_lat is not None and query_center_lng is not None else None
        # Run 1-night and 2-night searches concurrently; no structural filter and no per-search
        # PDP enrichment here — a single shared batch enrichment runs after both merge.
        def _one():
            return collect_search_comps(
                client, search_location, base_origin, anchor_date, adults,
                max_scroll_rounds=max_scroll_rounds, max_cards=fixed_search_cards,
                rate_limit_seconds=rate_limit_seconds, timeout_ms=15000,
                exclude_url=target.url, log_prefix=f"fixed_pool_one_night{label_suffix}",
                page_offsets=_page_offsets, center_lat=query_center_lat,
                center_lng=query_center_lng, map_radius_km=radius_arg,
                target_accommodates=None, pdp_structural_enrichment=False,
                prefer_one_night=True,
            )
        def _two():
            return collect_search_comps(
                client, search_location, base_origin, anchor_date, adults,
                max_scroll_rounds=max_scroll_rounds, max_cards=fixed_search_cards,
                rate_limit_seconds=rate_limit_seconds, timeout_ms=15000,
                exclude_url=target.url, log_prefix=f"fixed_pool_two_night{label_suffix}",
                page_offsets=_page_offsets, center_lat=query_center_lat,
                center_lng=query_center_lng, map_radius_km=radius_arg,
                target_accommodates=None, pdp_structural_enrichment=False,
                prefer_two_night=True,
            )
        with ThreadPoolExecutor(max_workers=2) as _ex:
            _f1, _f2 = _ex.submit(_one), _ex.submit(_two)
            one_night, _ = _f1.result()
            two_night, _ = _f2.result()
        return one_night, two_night

    def _merge_comps(comps_by_id: Dict[str, ListingSpec], comps_in: List[ListingSpec]) -> None:
        for comp in comps_in:
            cid = build_comp_id(comp.url or "")
            if cid and cid not in comps_by_id:
                comps_by_id[cid] = comp

    def _select_comps(comps_in: List[ListingSpec]) -> Tuple[List[Tuple[ListingSpec, float]], int, Dict[str, Any]]:
        filtered, debug = filter_similar_candidates(target, comps_in)
        filtered_ids = {id(c) for c in filtered}
        rejected_log = []
        for c in comps_in:
            if id(c) not in filtered_ids:
                rejected_log.append(f"{(c.url or '').rsplit('/', 1)[-1]}: structural-filtered")

        scored = []
        for c in filtered:
            score, breakdown = similarity_score(target, c, debug=True)
            scored.append((c, score))
            if float(score) < SIMILARITY_FLOOR:
                rejected_log.append(f"{(c.url or '').rsplit('/', 1)[-1]}: low-score={score:.4f}")

        scored.sort(key=lambda x: x[1], reverse=True)
        selected_items = [
            (comp, score)
            for comp, score in scored
            if float(score) >= SIMILARITY_FLOOR
        ][: max(1, int(pool_size))]

        if rejected_log:
            logger.debug(
                "[fixed_pool] %s: Rejected candidates: %s",
                anchor_date.isoformat() if hasattr(anchor_date, "isoformat") else str(anchor_date),
                ", ".join(rejected_log)
            )

        return selected_items, filtered, debug

    _anchor_checkin = anchor_date.isoformat() if hasattr(anchor_date, "isoformat") else str(anchor_date)
    _anchor_checkout = (anchor_date + timedelta(days=1)).isoformat() if hasattr(anchor_date, "isoformat") else _anchor_checkin

    def _enrich_new_comps(all_comps: List[ListingSpec]) -> None:
        """PDP-enrich the full comp list; the PDP cache makes re-visits instant."""
        if all_comps:
            _enrich_comps_baths_and_property_type_from_pdp(
                client, all_comps, checkin=_anchor_checkin, checkout=_anchor_checkout, adults=adults,
            )

    comps_by_id: Dict[str, ListingSpec] = {}
    one_night_comps, two_night_comps = _collect_at_radius(map_radius_limit_km)
    _merge_comps(comps_by_id, one_night_comps)
    _merge_comps(comps_by_id, two_night_comps)
    comps = list(comps_by_id.values())
    logger.info(
        "[fixed_pool] %s: one_night=%s two_night=%s merged=%s radius_km=%s",
        _anchor_checkin,
        len(one_night_comps),
        len(two_night_comps),
        len(comps),
        round(float(map_radius_limit_km), 3),
    )
    # Shared batch PDP enrichment — runs once for all unique comps from both searches.
    _enrich_new_comps(comps)

    selected, filtered_list, _dbg = _select_comps(comps) if comps else ([], [], {"stage": "empty"})
    filtered_count = len(filtered_list)
    expanded = False
    # Only widen the search if there is headroom below the hard radius cap.
    # "Within 8 miles" (priority #1) outranks reaching the pool target (priority
    # #3), so we never expand past MAP_RADIUS_CAP_KM to backfill a sparse pool.
    if (
        len(selected) < max(1, int(pool_size))
        and query_center_lat is not None
        and query_center_lng is not None
        and float(map_radius_limit_km) < MAP_RADIUS_CAP_KM
    ):
        expanded = True
        expanded_radius_km = min(float(map_radius_limit_km) + 1.0, MAP_RADIUS_CAP_KM)
        logger.info(
            "[fixed_pool] %s: selected=%s/%s; expanding search radius %.3fkm -> %.3fkm",
            _anchor_checkin,
            len(selected),
            pool_size,
            float(map_radius_limit_km),
            expanded_radius_km,
        )
        expanded_one, expanded_two = _collect_at_radius(expanded_radius_km, "_geo_expanded")
        before_expand = len(comps_by_id)
        _merge_comps(comps_by_id, expanded_one)
        _merge_comps(comps_by_id, expanded_two)
        comps = list(comps_by_id.values())
        # Enrich only truly new comps (cached ones return instantly).
        _enrich_new_comps(comps)
        selected, filtered_list, _dbg = _select_comps(comps) if comps else ([], [], {"stage": "empty"})
        filtered_count = len(filtered_list)
        logger.info(
            "[fixed_pool] %s: geo_expanded one_night=%s two_night=%s new_unique=%s merged=%s selected=%s",
            _anchor_checkin,
            len(expanded_one),
            len(expanded_two),
            len(comps_by_id) - before_expand,
            len(comps),
            len(selected),
        )

    if not comps:
        empty = {}
        if return_meta:
            return empty, {"collected": 0, "afterFiltering": 0, "selected": 0}
        return empty

    logger.info(
        "[fixed_pool] %s: scoring_funnel collected=%s filtered=%s "
        "stage=%s selected=%s requested_pool=%s expanded=%s",
        _anchor_checkin,
        len(comps),
        filtered_count,
        _dbg.get("stage", "unknown"),
        len(selected),
        pool_size,
        expanded,
    )

    fixed: Dict[str, Dict[str, Any]] = {}
    for comp, score in selected:
        cid = build_comp_id(comp.url or "")
        if not cid:
            continue
        fixed[cid] = {
            "similarity": round(float(score), 3),
            "url": comp.url,
            "title": comp.title,
            "location": comp.location,
            "accommodates": comp.accommodates,
            "bedrooms": comp.bedrooms,
            "beds": comp.beds,
            "baths": comp.baths,
            "property_type": comp.property_type,
            "rating": comp.rating,
            "reviews": comp.reviews,
            "amenities": list(comp.amenities or []),
            "currency": comp.currency,
            "nightly_price": (
                round(float(comp.nightly_price), 2)
                if isinstance(comp.nightly_price, (int, float)) and comp.nightly_price > 0
                else None
            ),
            "query_nights": int(comp.scrape_nights or 1),
            "query_total_price": (
                round(float(comp.query_total_price), 2)
                if isinstance(comp.query_total_price, (int, float)) and comp.query_total_price > 0
                else None
            ),
            "lat": comp.lat,
            "lng": comp.lng,
        }

    if return_meta:
        collected_ids = {cid for c in comps if (cid := build_comp_id(c.url or ""))}
        filtered_ids = {cid for c in filtered_list if (cid := build_comp_id(c.url or ""))}
        return fixed, {
            "collected": len(comps),
            "afterFiltering": filtered_count,
            "selected": len(fixed),
            "collectedIds": collected_ids,
            "afterFilteringIds": filtered_ids,
        }
    return fixed


def _merge_fixed_comp_entry(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(existing)
    if float(incoming.get("similarity") or 0.0) > float(existing.get("similarity") or 0.0):
        out["similarity"] = incoming.get("similarity")
    for key in ("url", "title", "location", "property_type", "currency"):
        if not out.get(key) and incoming.get(key):
            out[key] = incoming.get(key)
    for key in ("nightly_price",):
        if (not isinstance(out.get(key), (int, float)) or float(out.get(key) or 0) <= 0) and isinstance(incoming.get(key), (int, float)):
            out[key] = incoming.get(key)
    # Preserve explicit multi-night provenance for frontend labeling.
    out["query_nights"] = max(
        int(out.get("query_nights") or 1),
        int(incoming.get("query_nights") or 1),
    )
    if (not isinstance(out.get("query_total_price"), (int, float)) or float(out.get("query_total_price") or 0) <= 0) and isinstance(incoming.get("query_total_price"), (int, float)):
        out["query_total_price"] = incoming.get("query_total_price")
    for key in ("accommodates", "bedrooms", "beds", "baths", "rating", "reviews", "lat", "lng"):
        if out.get(key) is None and incoming.get(key) is not None:
            out[key] = incoming.get(key)
    ex_amen = list(out.get("amenities") or [])
    in_amen = list(incoming.get("amenities") or [])
    if in_amen:
        seen = set(ex_amen)
        for a in in_amen:
            if a not in seen:
                ex_amen.append(a)
                seen.add(a)
        out["amenities"] = ex_amen
    return out


def _fetch_pdp_prices_for_pool(
    client,
    pool: Dict[str, Dict[str, Any]],
    date_i,
    adults: int,
) -> Dict[str, float]:
    """
    Directly visit each pool comp's PDP for the given date to get its nightly price.
    Bypasses Airbnb search (which gets rate-limited) by going straight to listing pages.
    Returns {comp_id: nightly_price} for successfully priced comps.
    """
    checkin_str = date_i.isoformat()
    checkout_str = (date_i + timedelta(days=1)).isoformat()

    work_items: List[Tuple[str, str, str]] = []
    for cid, payload in pool.items():
        url = str(payload.get("url") or "")
        lid = extract_listing_id_from_url(url)
        if lid:
            work_items.append((cid, str(lid), url))

    if not work_items:
        return {}

    detail_fetcher = getattr(client, "get_listing_details", None)
    if detail_fetcher is None:
        logger.warning("[pdp_price_pool] client has no get_listing_details")
        return {}

    prices: Dict[str, float] = {}
    prices_lock = threading.Lock()

    def _fetch_one(work_item: Tuple[str, str, str]) -> None:
        cid, lid, url = work_item
        try:
            pdp_data = detail_fetcher(
                lid,
                checkin=checkin_str,
                checkout=checkout_str,
                adults=adults,
            )
            parsed = parse_pdp_response(pdp_data, lid, safe_domain_base(url))
            price = parsed.get("nightly_price")
            if isinstance(price, (int, float)) and float(price) > 0:
                with prices_lock:
                    prices[cid] = round(float(price), 2)
        except Exception as exc:
            logger.debug(
                "[pdp_price_pool] %s listing=%s fetch failed: %s",
                checkin_str, lid, str(exc)[:120],
            )

    max_concurrent = min(len(work_items), _tabs_per_browser())
    with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        list(executor.map(_fetch_one, work_items))

    logger.info(
        "[pdp_price_pool] %s: direct PDP pricing %s/%s comps priced",
        checkin_str, len(prices), len(work_items),
    )
    return prices


def _interleave_by_date_round_robin(
    per_date_items: List[List[Any]],
) -> List[Any]:
    """
    Round-robin interleave per-date work lists into a single flat list.

    Input is one list of work items per date; output cycles one item from each
    date before taking the next from any date: [d1c1, d2c1, d3c1, d1c2, d2c2, ...].
    This keeps every date's coverage balanced when downstream processing degrades
    over time (Airbnb throttling/challenges), instead of starving the later dates.
    """
    return [
        item
        for column in itertools.zip_longest(*per_date_items)
        for item in column
        if item is not None
    ]


def _prefetch_pool_pdp_prices_all_dates(
    clients: list,
    pool: Dict[str, Dict[str, Any]],
    sample_dates: List,
    adults: int,
) -> Dict[str, Dict[str, float]]:
    """
    Pre-fetch PDP prices for all (date, comp) pairs BEFORE day queries begin.

    Phase 1: Direct HTTP POST to StaysPdpSections (~300ms each, up to 50 concurrent).
    Phase 2: Browser-tab fallback for any Phase-1 failures (limited to tab count).

    Returns {date_iso: {comp_id: nightly_price}}.
    """
    if not pool or not sample_dates or not clients:
        return {}

    # Build per-date work lists, then interleave them round-robin across dates.
    # executor.map preserves submission order, so a date-grouped order ([all of
    # date1's comps, then date2's, ...]) means that when Airbnb throttling or
    # challenges accumulate mid-run, the LAST dates get starved — which shows up
    # as comp counts steadily declining toward later dates. Round-robin
    # interleaving ([d1c1, d2c1, d3c1, ..., d1c2, d2c2, ...]) spreads any
    # progressive degradation evenly so every date keeps comparable coverage.
    per_date_items: List[List[Tuple[str, str, str, str, Any]]] = []
    for date_i in sample_dates:
        checkin_str = date_i.isoformat()
        items_for_date: List[Tuple[str, str, str, str, Any]] = []
        for cid, payload in pool.items():
            url = str(payload.get("url") or "")
            lid = extract_listing_id_from_url(url)
            if lid:
                items_for_date.append((cid, str(lid), url, checkin_str, date_i))
        if items_for_date:
            per_date_items.append(items_for_date)

    work_items: List[Tuple[str, str, str, str, Any]] = _interleave_by_date_round_robin(
        per_date_items
    )

    if not work_items:
        return {}

    n_clients = len(clients)
    pre_prices: Dict[str, Dict[str, float]] = {}
    prices_lock = threading.Lock()
    fetch_start = time.time()
    logger.info(
        "[prefetch_pdp] Pre-fetching %s dates × %s comps = %s PDPs",
        len(sample_dates), len(pool), len(work_items),
    )

    def _try_store_price(pdp_data: Any, cid: str, lid: str, url: str, checkin_str: str) -> bool:
        if pdp_data is None:
            return False
        parsed = parse_pdp_response(pdp_data, lid, safe_domain_base(url))
        price = parsed.get("nightly_price")
        if isinstance(price, (int, float)) and float(price) > 0:
            with prices_lock:
                pre_prices.setdefault(checkin_str, {})[cid] = round(float(price), 2)
            return True
        return False

    # Phase 1: direct HTTP (no browser tabs, high concurrency)
    direct_fetchers = [getattr(c, "fetch_pdp_price_direct", None) for c in clients]
    has_direct = any(f is not None for f in direct_fetchers)
    failed_items: List[Tuple] = []
    direct_succeeded = 0

    if has_direct:
        direct_lock = threading.Lock()

        def _direct_fetch(i_item: Tuple[int, Tuple]) -> None:
            nonlocal direct_succeeded
            i, (cid, lid, url, checkin_str, date_i) = i_item
            fetcher = direct_fetchers[i % n_clients]
            checkout_str = (date_i + timedelta(days=1)).isoformat()
            if fetcher is None:
                failed_items.append((cid, lid, url, checkin_str, date_i))
                return
            # Small random jitter to spread requests and reduce rate-limit hits
            time.sleep(random.uniform(0.02, 0.15))
            try:
                pdp_data = fetcher(lid, checkin=checkin_str, checkout=checkout_str, adults=adults)
                if _try_store_price(pdp_data, cid, lid, url, checkin_str):
                    with direct_lock:
                        direct_succeeded += 1
                    return
            except Exception:
                pass
            failed_items.append((cid, lid, url, checkin_str, date_i))

        phase1_start = time.time()
        # Limit to 15 concurrent direct HTTP requests to avoid Airbnb rate limiting (503/429)
        direct_concurrency = min(15, len(work_items))
        logger.info("[prefetch_pdp] Phase1: direct HTTP, concurrency=%s", direct_concurrency)
        with ThreadPoolExecutor(max_workers=direct_concurrency) as executor:
            list(executor.map(_direct_fetch, enumerate(work_items)))
        phase1_ms = round((time.time() - phase1_start) * 1000)
        logger.info(
            "[prefetch_pdp] Phase1 done: %s/%s priced, %s need browser fallback, %sms",
            direct_succeeded, len(work_items), len(failed_items), phase1_ms,
        )
    else:
        failed_items = list(work_items)

    # Phase 2: browser fallback only for dates still lacking sufficient prices
    if failed_items:
        dates_needing_prices = {
            date_i.isoformat()
            for date_i in sample_dates
            if len(pre_prices.get(date_i.isoformat(), {})) < 3
        }
        needed_items = [item for item in failed_items if item[3] in dates_needing_prices]
        if not needed_items:
            logger.info(
                "[prefetch_pdp] Phase2 skipped -- all %s dates have >=3 prices from Phase1",
                len(sample_dates),
            )
        else:
            browser_succeeded = 0
            browser_lock = threading.Lock()

            def _browser_fetch(i_item: Tuple[int, Tuple]) -> None:
                nonlocal browser_succeeded
                i, (cid, lid, url, checkin_str, date_i) = i_item
                client = clients[i % n_clients]
                checkout_str = (date_i + timedelta(days=1)).isoformat()
                detail_fetcher = getattr(client, "get_listing_details", None)
                if detail_fetcher is None:
                    return
                try:
                    pdp_data = detail_fetcher(lid, checkin=checkin_str, checkout=checkout_str, adults=adults)
                    if _try_store_price(pdp_data, cid, lid, url, checkin_str):
                        with browser_lock:
                            browser_succeeded += 1
                except Exception as exc:
                    logger.debug(
                        "[prefetch_pdp] browser %s comp=%s failed: %s", checkin_str, lid, str(exc)[:80]
                    )

            phase2_start = time.time()
            browser_concurrency = n_clients * _tabs_per_browser()
            logger.info(
                "[prefetch_pdp] Phase2: browser fallback for %s/%s items (%s dates need prices), concurrency=%s",
                len(needed_items), len(failed_items), len(dates_needing_prices), browser_concurrency,
            )
            with ThreadPoolExecutor(max_workers=browser_concurrency) as executor:
                list(executor.map(_browser_fetch, enumerate(needed_items)))
            phase2_ms = round((time.time() - phase2_start) * 1000)
            logger.info(
                "[prefetch_pdp] Phase2 done: %s/%s additional prices, %sms",
                browser_succeeded, len(needed_items), phase2_ms,
            )

    total_priced = sum(len(v) for v in pre_prices.values())
    elapsed_ms = round((time.time() - fetch_start) * 1000)
    logger.info(
        "[prefetch_pdp] Done: %s/%s prices fetched across %s dates in %sms",
        total_priced, len(work_items), len(sample_dates), elapsed_ms,
    )
    return pre_prices


def _build_day_result_from_pool_pdp_prices(
    pool: Dict[str, Dict[str, Any]],
    prices: Dict[str, float],
    date_i,
    top_k: int = 20,
) -> DayResult:
    """
    Build a DayResult from known pool structural data + freshly fetched PDP prices.
    Uses pre-computed similarity scores from pool build — no re-scoring needed.
    """
    checkin_str = date_i.isoformat()
    is_weekend = date_i.weekday() >= 4  # Fri=4, Sat=5

    priced_comps: List[Tuple[str, Dict[str, Any], float, float]] = []
    for cid, price in prices.items():
        payload = pool.get(cid)
        if not payload:
            continue
        sim = float(payload.get("similarity") or 0.0)
        if sim < SIMILARITY_FLOOR:
            continue
        if not isinstance(price, (int, float)) or float(price) <= 0:
            continue
        priced_comps.append((cid, payload, float(price), sim))

    if not priced_comps:
        return DayResult(
            date=checkin_str,
            median_price=None,
            error="direct_pdp_fallback: no comps priced above similarity floor",
            selection_mode="direct_pdp_fallback",
            pricing_confidence="low",
            flags=["direct_pdp_fallback", "no_pool_prices"],
            is_weekend=is_weekend,
        )

    priced_comps.sort(key=lambda x: x[3], reverse=True)
    top_items = priced_comps[: max(1, int(top_k))]

    all_prices = [p for _, _, p, _ in top_items]
    median_price = round(statistics.median(all_prices), 2) if all_prices else None

    comp_prices: Dict[str, float] = {cid: p for cid, _, p, _ in top_items}
    top_comps: List[Dict[str, Any]] = []
    for cid, payload, price, sim in top_items:
        top_comps.append({
            "id": cid,
            "url": payload.get("url"),
            "title": payload.get("title") or "Comparable listing",
            "propertyType": payload.get("property_type") or "entire_home",
            "accommodates": payload.get("accommodates"),
            "bedrooms": payload.get("bedrooms"),
            "baths": payload.get("baths"),
            "nightlyPrice": price,
            "currency": payload.get("currency") or "USD",
            "similarity": round(sim, 3),
            "rating": payload.get("rating"),
            "reviews": payload.get("reviews"),
            "amenities": list(payload.get("amenities") or []),
            "location": payload.get("location"),
            "lat": payload.get("lat"),
            "lng": payload.get("lng"),
        })

    return DayResult(
        date=checkin_str,
        median_price=median_price,
        comps_collected=len(prices),
        comps_used=len(top_items),
        below_similarity_floor=max(0, len(priced_comps) - len(top_items)),
        is_weekend=is_weekend,
        selection_mode="direct_pdp_fallback",
        pricing_confidence="medium" if len(top_items) >= 3 else "low",
        comp_prices=comp_prices,
        top_comps=top_comps,
        flags=["direct_pdp_fallback"],
        filter_stage="pdp_pool_direct",
    )


def _build_fixed_comp_pool_by_stride(
    client,
    target: ListingSpec,
    base_origin: str,
    all_nights: List,
    adults: int,
    *,
    stride_days: int,
    max_scroll_rounds: int,
    max_cards: int,
    rate_limit_seconds: float,
    max_radius_km: Optional[float],
    pool_size: int,
) -> Tuple[Dict[str, Dict[str, Any]], List[str], int, int]:
    """Build one deduped fixed comp pool from anchor dates sampled every stride_days.

    Returns (pool, anchor_dates, collected_total, filtered_total) where
    collected_total / filtered_total are the unique comp counts seen across all
    anchors (collected_total >= filtered_total >= len(pool)). These feed the
    report's compsSummary so collected/filtered/used stay mutually consistent.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    seen_counts: Dict[str, int] = {}
    anchor_dates: List[str] = []
    collected_ids: set[str] = set()
    filtered_ids: set[str] = set()
    step = max(1, int(stride_days))
    for anchor_idx in range(0, len(all_nights), step):
        anchor_date = all_nights[anchor_idx]
        anchor_dates.append(anchor_date.isoformat())
        pool, pool_meta = _build_fixed_comp_pool(
            client,
            target,
            base_origin,
            anchor_date,
            adults,
            max_scroll_rounds=max_scroll_rounds,
            max_cards=max_cards,
            rate_limit_seconds=rate_limit_seconds,
            max_radius_km=max_radius_km,
            pool_size=pool_size,
            page_count=2,
            return_meta=True,
        )
        collected_ids |= set(pool_meta.get("collectedIds") or ())
        filtered_ids |= set(pool_meta.get("afterFilteringIds") or ())
        for cid, payload in pool.items():
            seen_counts[cid] = int(seen_counts.get(cid, 0)) + 1
            if cid not in merged:
                merged[cid] = dict(payload)
            else:
                merged[cid] = _merge_fixed_comp_entry(merged[cid], payload)

    # Keep fixed pool size bounded after dedupe by availability then similarity.
    ranked = sorted(
        merged.items(),
        key=lambda kv: (
            -int(seen_counts.get(kv[0], 0)),
            -float((kv[1] or {}).get("similarity") or 0.0),
            -int((kv[1] or {}).get("reviews") or 0),
        ),
    )
    limited = {}
    for cid, payload in ranked[: max(1, int(pool_size))]:
        item = dict(payload)
        item["seenCount"] = int(seen_counts.get(cid, 0))
        limited[cid] = item
    # filtered_ids is a superset of every selected comp, which is a superset of
    # the final (bounded) pool; collected_ids supersets filtered_ids. So the
    # invariant collected_total >= filtered_total >= len(limited) holds.
    return limited, anchor_dates, len(collected_ids), len(filtered_ids)


def _preferred_comp_id(listing_url: str) -> str:
    match = ROOM_ID_RE.search(listing_url)
    return match.group(1) if match else listing_url


def _build_url_mode_benchmark_info(
    all_day_results: List[Dict[str, Any]],
    preferred_comps: Optional[List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    if not preferred_comps:
        return None

    primary_url = str(preferred_comps[0].get("listingUrl") or "").strip()
    if not primary_url:
        return None

    sampled_days = [r for r in all_day_results if r.get("is_sampled", False)]
    total_days = len(sampled_days)
    if total_days == 0:
        return None

    from worker.core.benchmark import BENCHMARK_MARKET_WEIGHT, BENCHMARK_MAX_ADJ

    primary_id = _preferred_comp_id(primary_url)
    benchmark_prices: List[float] = []
    market_prices: List[float] = []
    market_adjustments: List[float] = []
    outlier_days = 0
    search_hits = 0
    failed = 0

    secondary_urls = [
        str(pc.get("listingUrl") or "").strip()
        for pc in preferred_comps[1:]
        if isinstance(pc, dict) and str(pc.get("listingUrl") or "").strip()
    ]
    secondary_found: Dict[str, List[float]] = {url: [] for url in secondary_urls}

    for day in sampled_days:
        comp_prices = day.get("comp_prices") or {}
        primary_price = comp_prices.get(primary_id)
        if primary_price is None:
            for tc in day.get("top_comps", []) or []:
                tc_url = str(tc.get("url") or "").strip()
                if tc_url and comp_urls_match(tc_url, primary_url):
                    price = tc.get("nightlyPrice")
                    if isinstance(price, (int, float)) and price > 0:
                        primary_price = float(price)
                        break

        if primary_price is not None:
            benchmark_prices.append(round(float(primary_price), 2))
            search_hits += 1
            market_price = day.get("median_price")
            if isinstance(market_price, (int, float)) and market_price > 0:
                market_prices.append(round(float(market_price), 2))
                adj = ((float(market_price) - float(primary_price)) / float(primary_price)) * 100
                market_adjustments.append(adj)
                if abs(adj) >= 40:
                    outlier_days += 1
        else:
            failed += 1

        for sec_url in secondary_urls:
            sec_id = _preferred_comp_id(sec_url)
            sec_price = comp_prices.get(sec_id)
            if sec_price is None:
                for tc in day.get("top_comps", []) or []:
                    tc_url = str(tc.get("url") or "").strip()
                    if tc_url and comp_urls_match(tc_url, sec_url):
                        price = tc.get("nightlyPrice")
                        if isinstance(price, (int, float)) and price > 0:
                            sec_price = float(price)
                            break
            if isinstance(sec_price, (int, float)) and sec_price > 0:
                secondary_found[sec_url].append(round(float(sec_price), 2))

    benchmark_used = len(benchmark_prices) > 0
    avg_benchmark = round(statistics.mean(benchmark_prices), 2) if benchmark_prices else None
    avg_market = round(statistics.mean(market_prices), 2) if market_prices else None
    avg_adj = round(statistics.mean(market_adjustments), 1) if market_adjustments else None

    secondary_comps: List[Dict[str, Any]] = []
    for sec_url in secondary_urls:
        prices = secondary_found.get(sec_url, [])
        secondary_comps.append({
            "url": sec_url,
            "avgPrice": round(statistics.mean(prices), 2) if prices else None,
            "daysFound": len(prices),
            "totalDays": total_days,
        })

    consensus_signal: Optional[str] = None
    found_avgs = [row["avgPrice"] for row in secondary_comps if row["avgPrice"] is not None]
    if found_avgs and avg_benchmark is not None:
        secondary_mean = statistics.mean(found_avgs)
        pct_from_benchmark = abs(secondary_mean - avg_benchmark) / avg_benchmark
        if avg_market is not None and avg_market > 0:
            pct_from_market = abs(secondary_mean - avg_market) / avg_market
            if pct_from_benchmark <= 0.20:
                consensus_signal = "strong"
            elif pct_from_market <= 0.20 and pct_from_benchmark > 0.20:
                consensus_signal = "divergent"
            else:
                consensus_signal = "mixed"
        else:
            consensus_signal = "strong" if pct_from_benchmark <= 0.20 else "mixed"

    conflict_detected = bool(
        (total_days > 0 and outlier_days / total_days > 0.30)
        or consensus_signal == "divergent"
    )

    return {
        "benchmarkUsed": benchmark_used,
        "benchmarkUrl": primary_url,
        "benchmarkFetchStatus": "search_hit" if benchmark_used else "failed",
        "benchmarkFetchMethod": "search_hit" if benchmark_used else "failed",
        "avgBenchmarkPrice": avg_benchmark,
        "avgMarketPrice": avg_market,
        "marketAdjustmentPct": avg_adj,
        "appliedMarketWeight": BENCHMARK_MARKET_WEIGHT,
        "effectiveMarketWeight": BENCHMARK_MARKET_WEIGHT,
        "maxAdjCap": BENCHMARK_MAX_ADJ,
        "outlierDays": outlier_days,
        "conflictDetected": conflict_detected,
        "fallbackReason": None if benchmark_used else "benchmark_not_found_in_url_mode",
        "fetchStats": {
            "searchHits": search_hits,
            "directFetches": 0,
            "failed": failed,
            "totalDays": total_days,
            "highConfidenceDays": search_hits,
            "mediumConfidenceDays": 0,
            "lowConfidenceDays": 0,
        },
        "secondaryComps": secondary_comps or None,
        "consensusSignal": consensus_signal,
    }


# ---------------------------------------------------------------------------
# Target spec fallback
# ---------------------------------------------------------------------------


def _is_spec_degraded(target: ListingSpec) -> bool:
    """
    Heuristic: return True when the Airbnb page appears to have returned a
    degraded / stale render that is missing most structural fields.

    Two conditions trigger degraded detection:
      - Location is empty AND at least one of (bedrooms, accommodates, baths) is None, OR
      - Three or more of the five key fields are None / empty.

    These patterns typically indicate a bot-challenge page, a redirect, or a
    cached page fragment rather than real listing content.
    """
    missing_location = not bool(target.location and target.location.strip())
    missing_count = sum([
        missing_location,
        target.bedrooms is None,
        target.accommodates is None,
        target.baths is None,
        not bool(target.property_type and target.property_type.strip()),
    ])
    if missing_location and missing_count >= 2:
        return True
    if missing_count >= 3:
        return True
    return False


def _backfill_target_spec(
    target: ListingSpec,
    attrs: Dict[str, Any],
) -> tuple:
    """
    Fill missing target spec fields from saved listing input attributes.

    Called after extract_target_spec() when key fields are absent (None / "").
    Only fills fields that are missing — never overwrites a live-extracted value.

    Returns (updated_target, debug_meta) where debug_meta is a dict with:
      "fields_filled"       — list of field names backfilled from attrs
      "fields_still_missing"— list of key fields still None/empty after backfill
      "source"              — "saved_attributes" | "none"
      "is_partial"          — True if any key field remains missing after backfill
    """
    fields_filled: list = []

    if not target.property_type:
        v = attrs.get("propertyType")
        if v and isinstance(v, str):
            target.property_type = v
            fields_filled.append("property_type")

    if target.accommodates is None:
        v = attrs.get("maxGuests")
        if isinstance(v, int) and v > 0:
            target.accommodates = v
            fields_filled.append("accommodates")

    if target.bedrooms is None:
        v = attrs.get("bedrooms")
        if isinstance(v, int) and v >= 0:
            target.bedrooms = v
            fields_filled.append("bedrooms")

    if target.baths is None:
        v = attrs.get("bathrooms")
        if isinstance(v, (int, float)) and v > 0:
            target.baths = float(v)
            fields_filled.append("baths")

    # beds is not a standard user-input field but include for completeness
    if target.beds is None:
        v = attrs.get("beds")
        if isinstance(v, int) and v > 0:
            target.beds = v
            fields_filled.append("beds")

    # Fill missing location/city/state from saved attributes when available.
    if not (target.location and target.location.strip()):
        city = str(attrs.get("city") or "").strip()
        state = str(attrs.get("state") or attrs.get("province") or "").strip()
        location_candidates = [
            attrs.get("location"),
            attrs.get("listingLocation"),
            attrs.get("address"),
            attrs.get("inputAddress"),
            f"{city}, {state}" if city and state else "",
            city or "",
        ]
        for candidate in location_candidates:
            raw = str(candidate or "").strip()
            if not raw or _looks_non_location_placeholder(raw):
                continue
            target.location = raw
            if city and not target.city:
                target.city = city
            if state and not target.state:
                target.state = state
            fields_filled.append("location")
            break

    # Fill missing target coordinates from saved attributes when available.
    if target.lat is None or target.lng is None:
        coord_pairs = [
            ("lat", "lng"),
            ("target_lat", "target_lng"),
            ("latitude", "longitude"),
            ("centerLat", "centerLng"),
        ]
        for lat_key, lng_key in coord_pairs:
            lat_raw = attrs.get(lat_key)
            lng_raw = attrs.get(lng_key)
            try:
                lat_val = float(lat_raw) if lat_raw is not None else None
                lng_val = float(lng_raw) if lng_raw is not None else None
            except Exception:
                lat_val = None
                lng_val = None
            if lat_val is None or lng_val is None:
                continue
            if target.lat is None:
                target.lat = lat_val
            if target.lng is None:
                target.lng = lng_val
            fields_filled.append("lat_lng")
            break

    fields_still_missing: list = []
    if not target.property_type:
        fields_still_missing.append("property_type")
    if target.accommodates is None:
        fields_still_missing.append("accommodates")
    if target.bedrooms is None:
        fields_still_missing.append("bedrooms")
    if target.baths is None:
        fields_still_missing.append("baths")
    if not (target.location and target.location.strip()) and _coords_search_query(target.lat, target.lng) == "":
        fields_still_missing.append("location_or_coords")

    return target, {
        "fields_filled": fields_filled,
        "fields_still_missing": fields_still_missing,
        "source": "saved_attributes" if fields_filled else "none",
        "is_partial": bool(fields_still_missing),
    }


# ---------------------------------------------------------------------------
# Main scrape pipeline — day-by-day (connects to local Chrome via CDP)
# ---------------------------------------------------------------------------


def run_scrape(
    listing_url: str,
    checkin: str,
    checkout: str,
    cdp_url: str = "http://127.0.0.1:9222",
    adults: int = 2,
    top_k: int = 20,
    max_scroll_rounds: int = DAY_SCROLL_ROUNDS,
    max_cards: int = DAY_MAX_CARDS,
    max_runtime_seconds: int = 180,
    rate_limit_seconds: float = 1.0,
    cdp_connect_timeout_ms: int = 15000,
    preferred_comps: Optional[List[Dict[str, Any]]] = None,
    excluded_room_ids: Optional[set[str]] = None,
    target_lat: Optional[float] = None,
    target_lng: Optional[float] = None,
    max_radius_km: Optional[float] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    nightly_plan: Optional[Any] = None,
    fallback_attributes: Optional[Dict[str, Any]] = None,
    fallback_address: Optional[str] = None,
    target_spec_override: Optional[ListingSpec] = None,
    query_criteria_override: Optional[Dict[str, Any]] = None,
    force_playwright_daily_search: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Full scrape pipeline using day-by-day 1-night queries.

    Returns (daily_results, transparent_result).
    daily_results is a list of dicts, one per night in [checkin, checkout).
    Each dict contains: date, median_price, comps_collected, comps_used,
    filter_stage, flags, is_sampled, is_weekend, price_distribution, error.

    fallback_attributes: saved listing input attributes (bedrooms, bathrooms,
    maxGuests, propertyType, ...) used to backfill any target spec fields that
    extract_target_spec() could not recover from the Airbnb page. Only fills
    missing fields — never overwrites live-extracted values.

    target_spec_override: when provided, this becomes the source of truth for
    criteria-mode target metadata and day-query search context. The scraped
    listing URL still acts as an anchor/exclusion reference, but its location
    and capacity no longer replace the user-entered criteria.

    query_criteria_override: when provided, this is written into the final
    transparent result instead of the shared URL-mode query_criteria payload.

    Raises ValueError if the date range exceeds MAX_NIGHTS.
    """

    listing_url = normalize_airbnb_url(listing_url)

    start_time = time.time()
    timings: Dict[str, int] = {}
    extraction_warnings: List[str] = []
    base_origin = safe_domain_base(listing_url)

    # Parse and validate dates
    d_start = dt.strptime(checkin, "%Y-%m-%d").date()
    d_end = dt.strptime(checkout, "%Y-%m-%d").date()
    total_nights = (d_end - d_start).days
    if total_nights < 1:
        return [], _empty_transparent("scrape", "Invalid date range: checkout must be after checkin")
    if total_nights > MAX_NIGHTS:
        raise ValueError(
            f"Date range of {total_nights} nights exceeds maximum of {MAX_NIGHTS}. "
            f"Please select a shorter range."
        )

    all_nights = daterange_nights(d_start, d_end)

    # Query every night in the requested range.
    if nightly_plan is not None:
        sample_indices = list(range(total_nights))
        _eff_scroll_rounds = nightly_plan.scroll_rounds
        _eff_max_cards = nightly_plan.max_cards
        _early_stop_threshold = None
        logger.info(
            f"Day-by-day pipeline (nightly): {total_nights} nights, "
            f"querying all {len(sample_indices)} nights "
            f"(scroll_rounds={_eff_scroll_rounds}, max_cards={_eff_max_cards})"
        )
    else:
        sample_indices = list(range(total_nights))
        _eff_scroll_rounds = max_scroll_rounds
        _eff_max_cards = max_cards
        _early_stop_threshold = None
        logger.info(
            f"Day-by-day pipeline: {total_nights} nights, "
            f"querying all {len(sample_indices)} days (sampling=no)"
        )

    client = AirbnbClient(
        {
            "AIRBNB_BASE_URL": base_origin,
            "CHECKIN": checkin,
            "CHECKOUT": checkout,
            "ADULTS": adults,
            "CDP_URL": cdp_url,
            "LOG_RAW_PAYLOADS": None,
        }
    )
    page = client
    try:
            preflight_start = time.time()
            listing_missing, listing_preflight = _preflight_listing_exists(client, listing_url)
            timings["listing_preflight_ms"] = round((time.time() - preflight_start) * 1000)
            if listing_preflight:
                logger.info("[run_scrape] listing preflight: %s", listing_preflight)
            if listing_missing:
                transparent = _empty_transparent("scrape", "No such listing")
                transparent["debug"]["timingsMs"] = timings
                transparent["debug"]["listingPreflight"] = listing_preflight
                return [], transparent

            # Step 1: Extract target listing spec (with one retry on degraded pages)
            logger.info(f"Extracting target: {listing_url}")
            extract_start = time.time()
            target, warnings = extract_target_spec(client, listing_url, fail_on_unusable=True)
            extraction_warnings.extend(warnings)
            timings["extract_ms"] = round((time.time() - extract_start) * 1000)

            # Degraded-page detection + one retry.
            # If the page returned suspiciously incomplete fields (e.g. bot-challenge
            # redirect or cached fragment), wait 2 s and extract once more before
            # falling back to saved attributes.  The retry is lightweight — it only
            # re-navigates to the same URL and re-parses; it does not re-open a new
            # browser context.
            _spec_retry_attempted = False
            _spec_retry_improved = False
            _spec_degraded_page_suspected = _is_spec_degraded(target)
            if _spec_degraded_page_suspected:
                logger.warning(
                    f"[run_scrape] Degraded page suspected "
                    f"(location={target.location!r}, bedrooms={target.bedrooms}, "
                    f"accommodates={target.accommodates}, baths={target.baths}, "
                    f"type={target.property_type!r}) — retrying extraction in 2s"
                )
                extraction_warnings.append("Degraded page suspected; retrying spec extraction")
                time.sleep(2)
                _spec_retry_attempted = True
                _retry_start = time.time()
                target_retry, retry_warnings = extract_target_spec(
                    client, listing_url, fail_on_unusable=True
                )
                timings["extract_retry_ms"] = round((time.time() - _retry_start) * 1000)
                extraction_warnings.extend(retry_warnings)
                if not _is_spec_degraded(target_retry):
                    target = target_retry
                    _spec_retry_improved = True
                    logger.info("[run_scrape] Retry resolved degraded spec")
                else:
                    # Prefer the retry result if it has more fields even if still degraded
                    _orig_missing = sum([
                        not target.location, target.bedrooms is None,
                        target.accommodates is None, target.baths is None,
                        not target.property_type,
                    ])
                    _retry_missing = sum([
                        not target_retry.location, target_retry.bedrooms is None,
                        target_retry.accommodates is None, target_retry.baths is None,
                        not target_retry.property_type,
                    ])
                    if _retry_missing < _orig_missing:
                        target = target_retry
                        _spec_retry_improved = True
                        logger.info(
                            f"[run_scrape] Retry partially improved spec "
                            f"(missing fields: {_orig_missing} → {_retry_missing})"
                        )
                    else:
                        logger.warning("[run_scrape] Retry did not improve degraded spec")

            # Step 1a: Backfill missing target spec fields from saved listing attributes.
            # Resolves the failure mode where Airbnb returns a degraded page that omits
            # spec fields, causing all comps to score 0.35 (below the 0.40 floor) and
            # the wrong effective_adults for the market search (None → default 2).
            _spec_backfill_meta: Optional[Dict[str, Any]] = None
            if fallback_attributes:
                target, _spec_backfill_meta = _backfill_target_spec(target, fallback_attributes)
                if _spec_backfill_meta["fields_filled"]:
                    extraction_warnings.append(
                        f"Target spec backfilled from saved attributes: "
                        f"filled={_spec_backfill_meta['fields_filled']}, "
                        f"still_missing={_spec_backfill_meta['fields_still_missing']}"
                    )
                    logger.info(
                        f"[run_scrape] Target spec after backfill: type={target.property_type!r} "
                        f"bedrooms={target.bedrooms} accommodates={target.accommodates} "
                        f"baths={target.baths} beds={target.beds} "
                        f"(filled: {_spec_backfill_meta['fields_filled']})"
                    )
                elif _spec_backfill_meta["is_partial"]:
                    logger.warning(
                        f"[run_scrape] Target spec incomplete after extraction + fallback: "
                        f"still_missing={_spec_backfill_meta['fields_still_missing']}"
                    )

            # Build spec extraction telemetry — recorded in transparent result debug section.
            _spec_still_partial = bool(
                not target.property_type or target.accommodates is None
                or target.bedrooms is None or target.baths is None
            )
            _spec_location_source = "page"
            _spec_confidence: str
            missing_after = sum([
                not bool(target.location and target.location.strip()),
                not target.property_type,
                target.accommodates is None,
                target.bedrooms is None,
                target.baths is None,
            ])
            if missing_after == 0:
                _spec_confidence = "high"
            elif missing_after <= 1:
                _spec_confidence = "medium"
            else:
                _spec_confidence = "low"
            _spec_extraction_meta: Dict[str, Any] = {
                "retryAttempted": _spec_retry_attempted,
                "retryImproved": _spec_retry_improved,
                "degradedPageSuspected": _spec_degraded_page_suspected,
                "locationSource": _spec_location_source,
                "specConfidence": _spec_confidence,
                "stillPartial": _spec_still_partial,
            }

            _target_price_confidence: Optional[str] = None
            # Phase 3B: coordinate priority — page-extracted > geocoded > none.
            # extract_target_spec() may populate target.lat/lng from JSON-LD geo.
            # Only fall back to geocoded coords when the page gave us nothing.
            if target.lat is None or target.lng is None:
                if target_lat is not None and target_lng is not None:
                    target.lat = target_lat
                    target.lng = target_lng
            else:
                logger.info(
                    f"[run_scrape] Using page-extracted target coords "
                    f"({target.lat:.5f}, {target.lng:.5f})"
                )

            if target_spec_override is not None:
                anchor_url = target.url or listing_url
                anchor_price = target.nightly_price
                target = ListingSpec(
                    url=anchor_url,
                    title=target_spec_override.title or target.title,
                    location=target_spec_override.location or target.location,
                    city=target_spec_override.city or "",
                    state=target_spec_override.state or "",
                    postal_code=target_spec_override.postal_code or "",
                    country=target_spec_override.country or "",
                    country_code=target_spec_override.country_code or "",
                    accommodates=target_spec_override.accommodates,
                    bedrooms=target_spec_override.bedrooms,
                    beds=target_spec_override.beds,
                    baths=target_spec_override.baths,
                    property_type=target_spec_override.property_type or "",
                    nightly_price=None,
                    currency=target.currency or "USD",
                    rating=None,
                    reviews=None,
                    amenities=list(target_spec_override.amenities or []),
                    scrape_nights=target.scrape_nights,
                    price_kind=target.price_kind,
                    lat=target_spec_override.lat if target_spec_override.lat is not None else target.lat,
                    lng=target_spec_override.lng if target_spec_override.lng is not None else target.lng,
                )
                extraction_warnings.append(
                    "Criteria override applied: preserved user-entered target metadata and search context; "
                    f"anchor listing kept only for exclusion/reference ({anchor_url})."
                )
                _spec_extraction_meta["criteriaOverrideApplied"] = True
                _spec_extraction_meta["anchorListingUrl"] = anchor_url
                _spec_extraction_meta["anchorListingObservedNightlyPrice"] = anchor_price
                logger.info(
                    "[run_scrape] Applied criteria override: location=%r accommodates=%r "
                    "bedrooms=%r baths=%r anchor_url=%s",
                    target.location,
                    target.accommodates,
                    target.bedrooms,
                    target.baths,
                    anchor_url,
                )

            # Map bounds disabled for now.
            _effective_radius: Optional[float] = None

            if not target.location and target.title:
                if _title_looks_suspicious(target.title):
                    logger.warning(
                        "[run_scrape] Skip title-based location fallback due to suspicious title: %r",
                        target.title,
                    )
                else:
                    loc_m = re.search(
                        r"\bin\s+([A-Z][a-zA-Z\s,]+(?:,\s*[A-Z][a-zA-Z\s]+)?)",
                        target.title,
                    )
                    if loc_m:
                        target.location = loc_m.group(1).strip().rstrip(",.")
                        _spec_location_source = "title"
                        extraction_warnings.append(
                            f"Location fallback from title: '{target.location}'"
                        )

            # Last-resort: use the saved property address when title fallback also failed.
            if not target.location and fallback_address:
                loc_from_addr, _addr_conf = _extract_search_location(fallback_address)
                if loc_from_addr:
                    target.location = loc_from_addr
                    _spec_location_source = "saved_address"
                    extraction_warnings.append(
                        f"Location fallback from saved address: '{target.location}' "
                        f"(confidence={_addr_conf})"
                    )
                    logger.warning(
                        f"[run_scrape] Location fallback from saved address: "
                        f"'{target.location}' (confidence={_addr_conf})"
                    )

            # Finalize telemetry after all location fallbacks have run.
            # specConfidence and stillPartial are recomputed here so they reflect
            # the final post-fallback state (title or saved-address recovery raises
            # confidence from "low" to "medium"/"high" when location is now resolved).
            _spec_extraction_meta["locationSource"] = _spec_location_source
            _final_missing = sum([
                not bool(target.location and target.location.strip()),
                not target.property_type,
                target.accommodates is None,
                target.bedrooms is None,
                target.baths is None,
            ])
            _spec_extraction_meta["specConfidence"] = (
                "high" if _final_missing == 0
                else "medium" if _final_missing <= 1
                else "low"
            )
            _spec_extraction_meta["stillPartial"] = bool(
                not target.property_type or target.accommodates is None
                or target.bedrooms is None or target.baths is None
            )

            if not target.location and _coords_search_query(target.lat, target.lng) == "":
                return [], {
                    "targetSpec": {
                        "title": target.title,
                        "location": "",
                        "city": target.city or "",
                        "state": target.state or "",
                        "postalCode": target.postal_code or "",
                        "country": target.country or "",
                        "countryCode": target.country_code or "",
                        "lat": target.lat,
                        "lng": target.lng,
                        "propertyType": target.property_type,
                        "accommodates": target.accommodates,
                        "bedrooms": target.bedrooms,
                        "beds": target.beds,
                        "baths": target.baths,
                        "amenities": target.amenities,
                        "rating": target.rating,
                        "reviews": target.reviews,
                    },
                    "queryCriteria": None,
                    "compsSummary": None,
                    "priceDistribution": None,
                    "recommendedPrice": None,
                    "comparableListings": None,
                    "debug": {
                        "source": "scrape",
                        "error": "Cannot determine location from listing page.",
                        "extractionWarnings": extraction_warnings,
                        "timingsMs": timings,
                        "specExtractionMeta": _spec_extraction_meta,
                    },
                }

            # Use target listing capacity for search alignment
            effective_adults = adults
            if target.accommodates and target.accommodates > 0:
                effective_adults = min(int(target.accommodates), 16)

            _location_basis = str(target.location or "").strip() or _coords_search_query(
                target.lat, target.lng
            )
            query_criteria = {
                "locationBasis": _location_basis,
                "searchAdults": effective_adults,
                "checkin": checkin,
                "checkout": checkout,
                "totalNights": total_nights,
                "sampledNights": len(sample_indices),
                "queryMode": "day_by_day",
                "propertyTypeFilter": target.property_type or None,
            }
            if query_criteria_override is not None:
                query_criteria = dict(query_criteria_override)
                query_criteria["checkin"] = checkin
                query_criteria["checkout"] = checkout
                query_criteria.setdefault("totalNights", total_nights)
                query_criteria.setdefault("sampledNights", len(sample_indices))
                query_criteria.setdefault("queryMode", "criteria_anchor_assisted")

            # Step 2: Day-by-day 1-night queries

            # Per-day mode still prices from each day's live comps, but builds one
            # reusable metadata/price pool up front so sparse days can display a
            # stable 20-comp comparable set without extra per-day PDP fetches.
            query_criteria["reportRanking"] = "per_day_top_k"
            query_criteria["perDayTopK"] = max(1, int(top_k))
            query_criteria["dailyCompCoverageTarget"] = MIN_DAILY_COMPS_PER_DAY
            query_criteria["fixedCompBaselineTarget"] = FIXED_COMP_BASELINE_SIZE
            query_criteria["compsetMethodology"] = "hybrid_fixed_dynamic"

            fixed_comp_pool: Dict[str, Dict[str, Any]] = {}
            fixed_pool_start = time.time()
            try:
                (
                    fixed_comp_pool,
                    fixed_anchor_dates,
                    fixed_collected_total,
                    fixed_filtered_total,
                ) = _build_fixed_comp_pool_by_stride(
                    client,
                    target,
                    base_origin,
                    all_nights,
                    effective_adults,
                    stride_days=FIXED_COMP_POOL_STRIDE_DAYS,
                    max_scroll_rounds=max(1, min(int(_eff_scroll_rounds), 2)),
                    max_cards=_eff_max_cards,
                    rate_limit_seconds=rate_limit_seconds,
                    max_radius_km=_effective_radius,
                    pool_size=FIXED_COMP_POOL_GLOBAL_LIMIT,
                )
                query_criteria["fixedCompPoolSize"] = len(fixed_comp_pool)
                # Unique collected/filtered totals across anchors keep the report's
                # compsSummary consistent (collected >= filtered >= used).
                query_criteria["fixedCompPoolCollectedTotal"] = max(
                    fixed_collected_total, len(fixed_comp_pool)
                )
                query_criteria["fixedCompPoolFilteredTotal"] = max(
                    fixed_filtered_total, len(fixed_comp_pool)
                )
                query_criteria["fixedCompPoolAnchorDates"] = fixed_anchor_dates
                query_criteria["fixedCompPoolStrideDays"] = FIXED_COMP_POOL_STRIDE_DAYS
                query_criteria["fixedCompPoolCoverageMode"] = "hybrid_fixed_then_dynamic"
                logger.info(
                    "[fixed_pool] built reusable pool size=%s anchors=%s target_daily_comps=%s",
                    len(fixed_comp_pool),
                    fixed_anchor_dates,
                    MIN_DAILY_COMPS_PER_DAY,
                )
            except Exception as fixed_pool_exc:
                fixed_comp_pool = {}
                extraction_warnings.append(f"Fixed comp pool build failed: {fixed_pool_exc}")
                logger.warning("[fixed_pool] build failed (non-fatal): %s", fixed_pool_exc)
            timings["fixed_comp_pool_ms"] = round((time.time() - fixed_pool_start) * 1000)

            day_loop_start = time.time()
            _day_query_workers = _bounded_workers("DAY_QUERY_MAX_WORKERS", 12)
            _day_query_pool, _day_query_locks = _build_browser_pool(
                base_config=client.config,
                requested_workers=_day_query_workers,
                total_tasks=len(sample_indices),
                pool_name="day_query",
            )
            _pool_size = len(_day_query_pool)
            # Allow more concurrent threads than browser-pool slots so all sample days
            # can run in parallel. The per-browser BoundedSemaphore(_tabs_per_browser)
            # caps concurrent tab usage per browser; browser_slot is assigned round-robin
            # across the physical pool so load is spread evenly.
            _day_query_workers = min(_day_query_workers, len(sample_indices))

            # Propagate primary client's cookies + PDP template to all pool clients.
            # Pool clients are freshly created with empty sessions; without this they
            # cannot make authenticated direct HTTP calls in Phase 1 pre-fetch.
            for _pool_client in _day_query_pool:
                try:
                    _pool_client.copy_session_state_from(client)
                except Exception:
                    pass

            # Pre-fetch comp prices for all sample dates using the full browser pool
            # BEFORE any searches start. This avoids the circular rate-limiting issue
            # where on-demand PDP fallback runs after searches have slowed down the browser.
            _pre_fetched_prices: Dict[str, Dict[str, float]] = {}
            if fixed_comp_pool:
                sample_dates_for_prefetch = [all_nights[i] for i in sample_indices]
                try:
                    _pre_fetched_prices = _prefetch_pool_pdp_prices_all_dates(
                        _day_query_pool,
                        fixed_comp_pool,
                        sample_dates_for_prefetch,
                        effective_adults,
                    )
                except Exception as _pf_exc:
                    logger.warning("[prefetch_pdp] pre-fetch failed (non-fatal): %s", _pf_exc)

            _day_query_args = [
                {"night_idx": night_idx, "browser_slot": i % _pool_size}
                for i, night_idx in enumerate(sample_indices)
            ]
            logger.info(
                f"[day_query] concurrent execution: workers={_day_query_workers}, "
                f"browsers={_pool_size}, dates={len(sample_indices)}, "
                f"rate_limit_seconds={rate_limit_seconds}"
            )

            def _day_positive_comp_ids(day_result: Optional[DayResult]) -> set[str]:
                if day_result is None:
                    return set()
                # Count comps at or above the daily similarity floor (consistent with
                # the floor used inside estimate_base_price_for_date).
                top_ids = {
                    str(comp.get("id") or build_comp_id(comp.get("url") or "") or "").strip()
                    for comp in (day_result.top_comps or [])
                    if float(comp.get("similarity") or 0.0) >= SIMILARITY_FLOOR
                }
                return {
                    str(cid)
                    for cid, price in (day_result.comp_prices or {}).items()
                    if str(cid) in top_ids
                    and isinstance(price, (int, float))
                    and float(price) > 0
                }

            def _merge_hybrid_day_results(
                fixed_result: DayResult,
                dynamic_result: DayResult,
            ) -> DayResult:
                merged_prices = dict(fixed_result.comp_prices or {})
                for cid, price in (dynamic_result.comp_prices or {}).items():
                    if (
                        cid not in merged_prices
                        and isinstance(price, (int, float))
                        and float(price) > 0
                    ):
                        merged_prices[cid] = price

                merged_top: List[Dict[str, Any]] = []
                seen_top: set[str] = set()
                for comp in list(fixed_result.top_comps or []) + list(dynamic_result.top_comps or []):
                    cid = str(comp.get("id") or build_comp_id(comp.get("url") or "") or "").strip()
                    if not cid or cid in seen_top:
                        continue
                    if float(comp.get("similarity") or 0.0) <= 0.50:
                        continue
                    seen_top.add(cid)
                    merged_top.append(comp)

                flags = list(dict.fromkeys(
                    list(fixed_result.flags or [])
                    + list(dynamic_result.flags or [])
                    + ["dynamic_day_query"]
                ))
                return DayResult(
                    date=fixed_result.date,
                    median_price=dynamic_result.median_price or fixed_result.median_price,
                    comps_collected=int(fixed_result.comps_collected or 0) + int(dynamic_result.comps_collected or 0),
                    comps_used=max(int(fixed_result.comps_used or 0), int(dynamic_result.comps_used or 0)),
                    below_similarity_floor=int(fixed_result.below_similarity_floor or 0)
                    + int(dynamic_result.below_similarity_floor or 0),
                    price_outliers_excluded=int(fixed_result.price_outliers_excluded or 0)
                    + int(dynamic_result.price_outliers_excluded or 0),
                    price_outliers_downweighted=int(fixed_result.price_outliers_downweighted or 0)
                    + int(dynamic_result.price_outliers_downweighted or 0),
                    geo_excluded=int(fixed_result.geo_excluded or 0) + int(dynamic_result.geo_excluded or 0),
                    price_band_excluded=int(fixed_result.price_band_excluded or 0)
                    + int(dynamic_result.price_band_excluded or 0),
                    filter_stage="hybrid_fixed_dynamic",
                    flags=flags,
                    is_sampled=True,
                    is_weekend=fixed_result.is_weekend,
                    price_distribution=dynamic_result.price_distribution or fixed_result.price_distribution,
                    top_comps=merged_top,
                    comp_prices=merged_prices,
                    error=dynamic_result.error if dynamic_result.error and not merged_prices else None,
                    selection_mode="hybrid_fixed_dynamic",
                    pricing_confidence=dynamic_result.pricing_confidence or fixed_result.pricing_confidence,
                )

            def _run_day_query(*, night_idx: int, browser_slot: int) -> DayResult:
                night_idx = int(night_idx)
                browser_slot = int(browser_slot)
                day_client = _day_query_pool[browser_slot]
                day_lock = _day_query_locks[browser_slot]
                date_i = all_nights[night_idx]
                date_str = date_i.isoformat()

                # Fast path: enough pre-fetched PDP prices → skip StaysSearch entirely
                if fixed_comp_pool and _pre_fetched_prices.get(date_str):
                    pdp_prices = _pre_fetched_prices[date_str]
                    if len(pdp_prices) >= 3:
                        pdp_result = _build_day_result_from_pool_pdp_prices(
                            fixed_comp_pool, pdp_prices, date_i, top_k,
                        )
                        if pdp_result.median_price is not None:
                            logger.info(
                                "[day_query] %s: fast-path skip search, %s pre-fetched prices -> median=$%s",
                                date_str, len(pdp_prices), pdp_result.median_price,
                            )
                            return pdp_result

                # Slow path: StaysSearch with retries and PDP fallback
                result: Optional[DayResult] = None
                for attempt in range(1, PER_DAY_MAX_RETRIES + 1):
                    time.sleep(rate_limit_seconds)
                    with day_lock:
                        result = estimate_base_price_for_date(
                            day_client,
                            target,
                            base_origin,
                            date_i,
                            effective_adults,
                            max_scroll_rounds=_eff_scroll_rounds,
                            max_cards=_eff_max_cards,
                            rate_limit_seconds=rate_limit_seconds,
                            top_k=top_k,
                            preferred_comps=preferred_comps,
                            excluded_room_ids=excluded_room_ids,
                            max_radius_km=_effective_radius,
                            known_comp_pool=fixed_comp_pool or None,
                            allow_relaxed_similarity=False,
                        )
                    if result.median_price is not None:
                        break
                    # Search rate-limited and we have pre-fetched prices — skip retry
                    if fixed_comp_pool and _pre_fetched_prices.get(date_str):
                        break
                    if attempt < PER_DAY_MAX_RETRIES:
                        logger.info(
                            f"[day_query] {date_str}: retry {attempt+1}/{PER_DAY_MAX_RETRIES}"
                        )
                fixed_yield = len(_day_positive_comp_ids(result))
                if fixed_comp_pool and result is not None and (result.median_price is None or fixed_yield < 3):
                    pdp_prices = _pre_fetched_prices.get(date_str)
                    source = "pre_fetched"
                    if not pdp_prices:
                        source = "on_demand"
                        pdp_prices = _fetch_pdp_prices_for_pool(
                            day_client, fixed_comp_pool, date_i, effective_adults,
                        )
                    logger.info(
                        "[hybrid_comps] %s: fixed pool yielded %s prices; using %s PDP prices for %s/%s pool comps",
                        date_str, fixed_yield, source,
                        len(pdp_prices) if pdp_prices else 0, len(fixed_comp_pool),
                    )
                    if pdp_prices:
                        pdp_result = _build_day_result_from_pool_pdp_prices(
                            fixed_comp_pool, pdp_prices, date_i, top_k,
                        )
                        pdp_yield = len(_day_positive_comp_ids(pdp_result))
                        if pdp_yield > 0:
                            result = _merge_hybrid_day_results(result, pdp_result)
                        logger.info(
                            "[hybrid_comps] %s: PDP yield=%s merged_yield=%s source=%s",
                            date_str, pdp_yield,
                            len(_day_positive_comp_ids(result)), source,
                        )
                    else:
                        logger.info(
                            "[hybrid_comps] %s: PDP returned no prices (source=%s)", date_str, source,
                        )
                if result is None:
                    return DayResult(
                        date=date_i.isoformat(),
                        median_price=None,
                        error="Day query returned no result",
                    )
                return result

            try:
                sampled_results, _runner_state = execute_day_queries_concurrently(
                    query_func=_run_day_query,
                    args_list=_day_query_args,
                    max_workers=_day_query_workers,
                    early_stop_threshold=_early_stop_threshold,
                    progress_callback=progress_callback,
                )
            finally:
                close_browser_client_pool(_day_query_pool)
                # Close any lingering browser tabs on the primary client, keeping one open.
                try:
                    client.close_extra_tabs()
                except Exception:
                    pass
            _queried_night_indices = [
                int(_day_query_args[i]["night_idx"]) for i in _runner_state.completed_indices
            ]
            _consecutive_empty_peak = _runner_state.consecutive_empty_peak
            _early_stop_triggered = _runner_state.early_stop_triggered
            if _early_stop_triggered and _early_stop_threshold is not None:
                logger.warning(
                    f"[nightly] Circuit-breaker: reached {_early_stop_threshold} consecutive "
                    f"empty results - stopping deeper crawl"
                )

            timings["day_queries_ms"] = round((time.time() - day_loop_start) * 1000)
            # Record nightly crawl metadata for debug visibility.
            if nightly_plan is not None:
                # Compute actual observed/inferred from execution tracking, not from the
                # original plan.  If early-stop fired, unqueried planned dates must not
                # be counted as observed or as the plan's original infer set.
                _actual_observed = [
                    _queried_night_indices[i]
                    for i, r in enumerate(sampled_results)
                    if r.median_price is not None
                ]
                _actual_inferred = sorted(
                    set(range(nightly_plan.total_nights)) - set(_actual_observed)
                )
                timings["nightly_crawl_debug"] = {
                    "total_nights": nightly_plan.total_nights,
                    "observed_count": len(_actual_observed),
                    "queried_count": len(_queried_night_indices),
                    "infer_count": len(_actual_inferred),
                    "early_stop_triggered": _early_stop_triggered,
                    "consecutive_empty_peak": _consecutive_empty_peak,
                    "tiers": nightly_plan.tier_debug,
                    "planned_observe_indices": nightly_plan.observe_indices,
                    "actual_queried_indices": _queried_night_indices,
                    "actual_observed_indices": _actual_observed,
                    "actual_inferred_indices": _actual_inferred,
                    "scroll_rounds": nightly_plan.scroll_rounds,
                    "max_cards": nightly_plan.max_cards,
                    "query_workers": _day_query_workers,
                }

            # Step 3: Interpolate unsampled/failed days
            interp_start = time.time()
            all_day_results_obj = interpolate_missing_days(sampled_results, all_nights)
            timings["interpolation_ms"] = round((time.time() - interp_start) * 1000)

            # Convert DayResult objects to dicts
            all_day_results: List[Dict[str, Any]] = []
            for dr in all_day_results_obj:
                all_day_results.append({
                    "date": dr.date,
                    "median_price": dr.median_price,
                    "comps_collected": dr.comps_collected,
                    "comps_used": dr.comps_used,
                    "below_similarity_floor": dr.below_similarity_floor,
                    "price_outliers_excluded": dr.price_outliers_excluded,
                    "price_outliers_downweighted": dr.price_outliers_downweighted,
                    "geo_excluded": dr.geo_excluded,
                    "price_band_excluded": dr.price_band_excluded,
                    "filter_stage": dr.filter_stage,
                    "flags": dr.flags,
                    "is_sampled": dr.is_sampled,
                    "is_weekend": dr.is_weekend,
                    "price_distribution": dr.price_distribution,
                    "top_comps": dr.top_comps,
                    "comp_prices": dr.comp_prices,
                    "error": dr.error,
                    "selection_mode": dr.selection_mode,
                    "pricing_confidence": dr.pricing_confidence,
                })

            # Capture target listing nightly price only when explicitly requested
            # or when no market day produced a valid price. PDP retries are slow
            # on unavailable dates and are not needed for comparable-market output.
            _has_valid_market_prices = any(
                isinstance(r.get("median_price"), (int, float))
                for r in all_day_results
            )
            _capture_target_price = (
                str(os.getenv("SCRAPER_CAPTURE_TARGET_PRICE", "0")).strip().lower()
                in ("1", "true", "yes", "on")
            ) or not _has_valid_market_prices
            if not _capture_target_price:
                logger.info("[run_scrape] Target price capture skipped (valid market prices present)")
            else:
                try:
                    _checkin_dt = dt.strptime(checkin, "%Y-%m-%d")
                    _checkout_1n = (_checkin_dt + timedelta(days=1)).strftime("%Y-%m-%d")
                    _checkout_2n = (_checkin_dt + timedelta(days=2)).strftime("%Y-%m-%d")

                    logger.info(
                        f"[run_scrape] Capturing target price: 1-night window "
                        f"{checkin} -> {_checkout_1n}"
                    )
                    _tp, _tp_conf = extract_nightly_price_from_listing_page(
                        page, listing_url, checkin, _checkout_1n, adults=effective_adults
                    )
                    if _tp is None and _tp_conf != "scrape_failed":
                        logger.info(
                            f"[run_scrape] 1-night target price None "
                            f"(confidence={_tp_conf}), retrying with 2-night window"
                        )
                        _tp, _tp_conf = extract_nightly_price_from_listing_page(
                            page, listing_url, checkin, _checkout_2n, adults=effective_adults
                        )

                    if _tp is not None:
                        target.nightly_price = _tp
                        _target_price_confidence = _tp_conf
                        logger.info(
                            f"[run_scrape] Target price captured: ${_tp} "
                            f"(confidence={_tp_conf})"
                        )
                    else:
                        logger.warning(
                            f"[run_scrape] Target price capture returned None after 1+2 night "
                            f"attempts (final confidence={_tp_conf})"
                        )
                        _target_price_confidence = _tp_conf
                except Exception as _tp_exc:
                    logger.warning(f"[run_scrape] Target price capture failed (non-fatal): {_tp_exc}")

            timings["total_ms"] = round((time.time() - start_time) * 1000)

            transparent = _build_daily_transparent_result(
                target=target,
                query_criteria=query_criteria,
                all_day_results=all_day_results,
                timings_ms=timings,
                source="scrape",
                extraction_warnings=extraction_warnings,
                discount_evidence=None,
                benchmark_info=_build_url_mode_benchmark_info(
                    all_day_results,
                    preferred_comps,
                ),
                target_price_confidence=_target_price_confidence,
                spec_backfill=_spec_backfill_meta,
                spec_extraction_meta=_spec_extraction_meta,
                fixed_comp_pool=fixed_comp_pool,
                excluded_room_ids=excluded_room_ids,
            )
            # Phase 3B: surface page-extracted coords so main.py can write them back to DB
            if target.lat is not None and target.lng is not None:
                _coord_source = (
                    "page" if (target.lat != target_lat or target.lng != target_lng
                               or target_lat is None)
                    else "geocoded"
                )
                transparent["pageExtractedCoords"] = {
                    "lat": target.lat,
                    "lng": target.lng,
                    "source": _coord_source,
                }
            # Keep forced Playwright daily fallback strictly on location-search flow.
            # Skip comp PDP repair passes in this mode to avoid opening unrelated
            # listing PDP tabs after the daily-search fallback.
            _repair_comparable_details = (
                str(os.getenv("SCRAPER_REPAIR_COMPARABLE_DETAILS", "0")).strip().lower()
                in ("1", "true", "yes", "on")
            )
            if (
                _repair_comparable_details
                and (not force_playwright_daily_search)
                and target_spec_override is None
            ):
                _repair_incomplete_comparable_specs(client, transparent, extraction_warnings)
                _repair_suspicious_comparable_titles(client, transparent, extraction_warnings)
            elif not _repair_comparable_details:
                logger.info("[run_scrape] Comparable PDP repair skipped for fast path")

            logger.info(
                f"Day-by-day pipeline complete: {len(sample_indices)} queries, "
                f"{sum(1 for r in all_day_results if r['median_price'] is not None)} valid prices, "
                f"{timings['total_ms']}ms total "
                f"(extract={timings.get('extract_ms', 0)}ms, "
                f"day_queries={timings.get('day_queries_ms', 0)}ms, "
                f"interpolation={timings.get('interpolation_ms', 0)}ms)"
            )

            return all_day_results, transparent
    finally:
        pass


# ---------------------------------------------------------------------------
# Benchmark-first scrape pipeline
# ---------------------------------------------------------------------------


def run_benchmark_scrape(
    benchmark_url: str,
    checkin: str,
    checkout: str,
    cdp_url: str = "http://127.0.0.1:9222",
    adults: int = 2,
    max_scroll_rounds: int = 12,
    max_cards: int = 80,
    max_runtime_seconds: int = 180,
    rate_limit_seconds: float = 1.0,
    cdp_connect_timeout_ms: int = 15000,
    target_spec_override: Optional[ListingSpec] = None,
    secondary_benchmark_urls: Optional[List[str]] = None,
    user_attributes: Optional[Dict[str, Any]] = None,
    target_lat: Optional[float] = None,
    target_lng: Optional[float] = None,
    max_radius_km: Optional[float] = None,
    excluded_room_ids: Optional[set[str]] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    nightly_plan: Optional[Any] = None,
    fallback_address: Optional[str] = None,
    target_url: Optional[str] = None,
) -> tuple:
    """
    Benchmark-first pipeline.

    Uses the pinned comp (benchmark_url) as the primary pricing anchor.
    Market comps from search are used only for a capped adjustment.

    Returns (daily_results, transparent_result).
    Fallback: if benchmark price fetch fails entirely, raises ValueError
    so the caller can fall back to the standard run_scrape pipeline.

    nightly_plan: when provided (nightly jobs only), overrides sample indices
    with the tiered nightly plan and applies reduced per-query limits and
    circuit-breaker early-stop.
    """
    from worker.core.benchmark import (
        BENCHMARK_MAX_SAMPLE_QUERIES,
        BenchmarkDayResult,
        aggregate_benchmark_transparency,
        benchmark_day_result_to_dict,
        probe_benchmark_discounts,  # 記得 import 新函式
        estimate_benchmark_price_for_date,
    )

    benchmark_url = normalize_airbnb_url(benchmark_url)
    if secondary_benchmark_urls:
        secondary_benchmark_urls = [normalize_airbnb_url(u) for u in secondary_benchmark_urls]

    start_time = time.time()
    timings: Dict[str, int] = {}
    extraction_warnings: List[str] = []
    base_origin = safe_domain_base(benchmark_url)

    d_start = dt.strptime(checkin, "%Y-%m-%d").date()
    d_end = dt.strptime(checkout, "%Y-%m-%d").date()
    total_nights = (d_end - d_start).days
    if total_nights < 1:
        return [], _empty_transparent("benchmark", "Invalid date range")
    if total_nights > MAX_NIGHTS:
        raise ValueError(
            f"Date range of {total_nights} nights exceeds maximum of {MAX_NIGHTS}."
        )

    all_nights = daterange_nights(d_start, d_end)

    # Nightly jobs use the tiered plan; benchmark interactive uses standard sampling.
    if nightly_plan is not None:
        sample_indices = nightly_plan.observe_indices
        _bm_eff_scroll_rounds = nightly_plan.scroll_rounds
        _bm_eff_max_cards = nightly_plan.max_cards
        _bm_early_stop_threshold: Optional[int] = nightly_plan.early_stop_threshold
        logger.info(
            f"Benchmark pipeline (nightly): {benchmark_url} | {total_nights} nights, "
            f"observing {len(sample_indices)} / inferring {len(nightly_plan.infer_indices)}"
        )
    else:
        if total_nights <= SAMPLE_THRESHOLD:
            sample_indices = list(range(total_nights))
        else:
            sample_indices = compute_sample_dates(total_nights, BENCHMARK_MAX_SAMPLE_QUERIES)
        from worker.core.benchmark import BENCHMARK_SCROLL_ROUNDS as _bm_eff_scroll_rounds, BENCHMARK_MAX_CARDS as _bm_eff_max_cards  # noqa: E501
        _bm_early_stop_threshold = None
        logger.info(
            f"Benchmark pipeline: {benchmark_url} | {total_nights} nights, "
            f"querying {len(sample_indices)} days"
        )

    client = AirbnbClient(
        {
            "AIRBNB_BASE_URL": base_origin,
            "CHECKIN": checkin,
            "CHECKOUT": checkout,
            "ADULTS": adults,
            "CDP_URL": cdp_url,
            "LOG_RAW_PAYLOADS": None,
        }
    )
    if True:
        # Step 0: Probe Discounts
        # 在開始逐日抓取前，先對 Benchmark 進行「定價策略探測」
        # 這會額外花費約 3-5 秒，但能大幅提升準確度
        discount_info = {}
        try:
            discount_info = probe_benchmark_discounts(client, benchmark_url, base_origin, d_start)
        except Exception as e:
            logger.warning(f"[benchmark] Discount probe failed: {e}")
        # ────────────────────────────────────────────────────────────────

        try:
            # Step 1: Extract benchmark listing spec (location, capacity, etc.)
            extract_start = time.time()
            _bm_spec_extraction_meta: Optional[Dict[str, Any]] = None
            _bm_backfill_meta: Optional[Dict[str, Any]] = None
            if target_spec_override is not None:
                target = target_spec_override
            else:
                spec_url = target_url or benchmark_url
                logger.info(f"[benchmark] Extracting spec from: {spec_url}")
                target, warnings = extract_target_spec(client, spec_url, fail_on_unusable=True)
                extraction_warnings.extend(warnings)

                # Degraded-page detection + one retry (mirrors run_scrape).
                _bm_retry_attempted = False
                _bm_retry_improved = False
                _bm_degraded_suspected = _is_spec_degraded(target)
                if _bm_degraded_suspected:
                    logger.warning(
                        f"[benchmark] Degraded page suspected — retrying spec extraction in 2s"
                    )
                    extraction_warnings.append(
                        "[benchmark] Degraded page suspected; retrying spec extraction"
                    )
                    time.sleep(2)
                    _bm_retry_attempted = True
                    target_bm_retry, bm_retry_warnings = extract_target_spec(
                        client, benchmark_url, fail_on_unusable=True
                    )
                    extraction_warnings.extend(bm_retry_warnings)
                    if not _is_spec_degraded(target_bm_retry):
                        target = target_bm_retry
                        _bm_retry_improved = True
                        logger.info("[benchmark] Retry resolved degraded spec")
                    else:
                        _bm_orig_missing = sum([
                            not target.location, target.bedrooms is None,
                            target.accommodates is None, target.baths is None,
                            not target.property_type,
                        ])
                        _bm_retry_missing = sum([
                            not target_bm_retry.location, target_bm_retry.bedrooms is None,
                            target_bm_retry.accommodates is None, target_bm_retry.baths is None,
                            not target_bm_retry.property_type,
                        ])
                        if _bm_retry_missing < _bm_orig_missing:
                            target = target_bm_retry
                            _bm_retry_improved = True
                            logger.info(
                                f"[benchmark] Retry partially improved spec "
                                f"(missing: {_bm_orig_missing} → {_bm_retry_missing})"
                            )
                        else:
                            logger.warning("[benchmark] Retry did not improve degraded spec")

                # Backfill from user_attributes when structural fields are still missing.
                _bm_backfill_meta: Optional[Dict[str, Any]] = None
                if user_attributes:
                    target, _bm_backfill_meta = _backfill_target_spec(target, user_attributes)
                    if _bm_backfill_meta["fields_filled"]:
                        extraction_warnings.append(
                            f"[benchmark] Target spec backfilled from user attributes: "
                            f"filled={_bm_backfill_meta['fields_filled']}, "
                            f"still_missing={_bm_backfill_meta['fields_still_missing']}"
                        )
                        logger.info(
                            f"[benchmark] Target spec after backfill: type={target.property_type!r} "
                            f"bedrooms={target.bedrooms} accommodates={target.accommodates} "
                            f"(filled: {_bm_backfill_meta['fields_filled']})"
                        )

                # Location fallback (mirrors run_scrape)
                _bm_location_source = "page"
                if not target.location and target.title:
                    if _title_looks_suspicious(target.title):
                        logger.warning(
                            "[benchmark] Skip title-based location fallback due to suspicious title: %r",
                            target.title,
                        )
                    else:
                        loc_m = re.search(
                            r"\bin\s+([A-Z][a-zA-Z\s,]+(?:,\s*[A-Z][a-zA-Z\s]+)?)",
                            target.title,
                        )
                        if loc_m:
                            target.location = loc_m.group(1).strip().rstrip(",.")
                            _bm_location_source = "title"
                            extraction_warnings.append(
                                f"[benchmark] Location fallback from title: '{target.location}'"
                            )

                # Last-resort: saved property address (mirrors run_scrape).
                if not target.location and fallback_address:
                    loc_from_addr, _bm_addr_conf = _extract_search_location(fallback_address)
                    if loc_from_addr:
                        target.location = loc_from_addr
                        _bm_location_source = "saved_address"
                        extraction_warnings.append(
                            f"[benchmark] Location fallback from saved address: "
                            f"'{target.location}' (confidence={_bm_addr_conf})"
                        )
                        logger.warning(
                            f"[benchmark] Location fallback from saved address: "
                            f"'{target.location}' (confidence={_bm_addr_conf})"
                        )

                # Build benchmark spec extraction telemetry after all fallbacks so
                # specConfidence / stillPartial / locationSource reflect final state.
                _bm_missing_after = sum([
                    not bool(target.location and target.location.strip()),
                    not target.property_type,
                    target.accommodates is None,
                    target.bedrooms is None,
                    target.baths is None,
                ])
                _bm_spec_extraction_meta: Dict[str, Any] = {
                    "retryAttempted": _bm_retry_attempted,
                    "retryImproved": _bm_retry_improved,
                    "degradedPageSuspected": _bm_degraded_suspected,
                    "locationSource": _bm_location_source,
                    "specConfidence": (
                        "high" if _bm_missing_after == 0
                        else "medium" if _bm_missing_after <= 1
                        else "low"
                    ),
                    "stillPartial": bool(
                        not target.property_type or target.accommodates is None
                        or target.bedrooms is None or target.baths is None
                    ),
                }

                if not target.location and _coords_search_query(target.lat, target.lng) == "":
                    return [], _empty_transparent(
                        "benchmark", "Cannot determine location from benchmark listing page."
                    )

            timings["extract_ms"] = round((time.time() - extract_start) * 1000)

            # Phase 3B: coordinate priority — page-extracted > geocoded > none.
            if target.lat is None or target.lng is None:
                if target_lat is not None and target_lng is not None:
                    target.lat = target_lat
                    target.lng = target_lng

            # Map bounds disabled for now.
            _effective_radius = None

            # ── Benchmark-to-target similarity (computed once per job) ────────
            # Compare the benchmark listing's extracted spec against the user's
            # stated property attributes.  The resulting similarity score is
            # passed to each day query so it can reduce effective_weight when
            # the benchmark is a poor structural match for the target property.
            from worker.core.benchmark import (
                _BM_SIMILARITY_HIGH_MATCH,
                _BM_SIMILARITY_STRONG_MISMATCH,
            )
            from worker.core.similarity import similarity_score as _similarity_score

            bm_target_similarity: float = 1.0   # default: no penalty
            bm_mismatch_level: str = "unknown"

            if user_attributes:
                user_spec = ListingSpec(
                    url="",
                    bedrooms=user_attributes.get("bedrooms"),
                    baths=user_attributes.get("bathrooms"),
                    accommodates=user_attributes.get("maxGuests"),
                    property_type=user_attributes.get("propertyType", ""),
                )
                bm_target_similarity = round(_similarity_score(user_spec, target), 3)
                if bm_target_similarity >= _BM_SIMILARITY_HIGH_MATCH:
                    bm_mismatch_level = "high_match"
                elif bm_target_similarity >= _BM_SIMILARITY_STRONG_MISMATCH:
                    bm_mismatch_level = "moderate_mismatch"
                else:
                    bm_mismatch_level = "strong_mismatch"

                if bm_mismatch_level != "high_match":
                    logger.warning(
                        f"[benchmark] Benchmark-to-target similarity={bm_target_similarity:.3f} "
                        f"({bm_mismatch_level}) — benchmark may be a poor anchor for this property"
                    )
                else:
                    logger.info(
                        f"[benchmark] Benchmark-to-target similarity={bm_target_similarity:.3f} "
                        f"({bm_mismatch_level})"
                    )

            effective_adults = adults
            if target.accommodates and target.accommodates > 0:
                effective_adults = min(int(target.accommodates), 16)

            _bm_location_basis = str(target.location or "").strip() or _coords_search_query(
                target.lat, target.lng
            )
            query_criteria = {
                "locationBasis": _bm_location_basis,
                "searchAdults": effective_adults,
                "checkin": checkin,
                "checkout": checkout,
                "totalNights": total_nights,
                "sampledNights": len(sample_indices),
                "queryMode": "benchmark_first",
                "benchmarkUrl": benchmark_url,
            }

            # Step 2: Benchmark day-by-day queries
            from worker.core.benchmark import BENCHMARK_TOP_K
            sampled_results: List[BenchmarkDayResult] = []
            day_loop_start = time.time()
            _bm_day_query_workers = _bounded_workers("BENCHMARK_DAY_QUERY_MAX_WORKERS", 12)
            _bm_day_query_pool, _bm_day_query_locks = _build_browser_pool(
                base_config=client.config,
                requested_workers=_bm_day_query_workers,
                total_tasks=len(sample_indices),
                pool_name="benchmark_day_query",
            )
            _bm_pool_size = len(_bm_day_query_pool)
            _bm_day_query_workers = min(_bm_day_query_workers, len(sample_indices))
            _bm_day_query_args = [
                {"night_idx": night_idx, "browser_slot": i % _bm_pool_size}
                for i, night_idx in enumerate(sample_indices)
            ]
            logger.info(
                f"[benchmark/day_query] concurrent execution: workers={_bm_day_query_workers}, "
                f"browsers={_bm_pool_size}, dates={len(sample_indices)}, "
                f"rate_limit_seconds={rate_limit_seconds}"
            )

            def _run_benchmark_day_query(*, night_idx: int, browser_slot: int) -> BenchmarkDayResult:
                night_idx = int(night_idx)
                browser_slot = int(browser_slot)
                day_client = _bm_day_query_pool[browser_slot]
                day_lock = _bm_day_query_locks[browser_slot]
                date_i = all_nights[night_idx]
                time.sleep(rate_limit_seconds)
                with day_lock:
                    return estimate_benchmark_price_for_date(
                        day_client,
                        target,
                        benchmark_url,
                        base_origin,
                        date_i,
                        effective_adults,
                        secondary_benchmark_urls=secondary_benchmark_urls or [],
                        benchmark_target_similarity=bm_target_similarity,
                        max_scroll_rounds=_bm_eff_scroll_rounds,
                        max_cards=_bm_eff_max_cards,
                        rate_limit_seconds=rate_limit_seconds,
                        top_k=BENCHMARK_TOP_K,
                        max_radius_km=_effective_radius,
                        excluded_room_ids=excluded_room_ids,
                    )

            try:
                sampled_results, _bm_runner_state = execute_day_queries_concurrently(
                    query_func=_run_benchmark_day_query,
                    args_list=_bm_day_query_args,
                    max_workers=_bm_day_query_workers,
                    early_stop_threshold=_bm_early_stop_threshold,
                    progress_callback=progress_callback,
                )
            finally:
                close_browser_client_pool(_bm_day_query_pool)
            _bm_queried_night_indices = [
                int(_bm_day_query_args[i]["night_idx"]) for i in _bm_runner_state.completed_indices
            ]
            _bm_consecutive_empty_peak = _bm_runner_state.consecutive_empty_peak
            _bm_early_stop_triggered = _bm_runner_state.early_stop_triggered
            if _bm_early_stop_triggered and _bm_early_stop_threshold is not None:
                logger.warning(
                    f"[nightly/benchmark] Circuit-breaker: reached {_bm_early_stop_threshold} consecutive "
                    f"empty results - stopping"
                )

            timings["day_queries_ms"] = round((time.time() - day_loop_start) * 1000)
            if nightly_plan is not None:
                _bm_actual_observed = [
                    _bm_queried_night_indices[i]
                    for i, r in enumerate(sampled_results)
                    if r.median_price is not None
                ]
                _bm_actual_inferred = sorted(
                    set(range(nightly_plan.total_nights)) - set(_bm_actual_observed)
                )
                timings["nightly_crawl_debug"] = {
                    "total_nights": nightly_plan.total_nights,
                    "observed_count": len(_bm_actual_observed),
                    "queried_count": len(_bm_queried_night_indices),
                    "infer_count": len(_bm_actual_inferred),
                    "early_stop_triggered": _bm_early_stop_triggered,
                    "consecutive_empty_peak": _bm_consecutive_empty_peak,
                    "tiers": nightly_plan.tier_debug,
                    "planned_observe_indices": nightly_plan.observe_indices,
                    "actual_queried_indices": _bm_queried_night_indices,
                    "actual_observed_indices": _bm_actual_observed,
                    "actual_inferred_indices": _bm_actual_inferred,
                    "scroll_rounds": nightly_plan.scroll_rounds,
                    "max_cards": nightly_plan.max_cards,
                    "query_workers": _bm_day_query_workers,
                }

            # Step 3: Interpolate
            # Reuse standard interpolation — BenchmarkDayResult.median_price is the blended price
            from worker.scraper.day_query import interpolate_missing_days

            def _to_day_result(r: BenchmarkDayResult) -> DayResult:
                return DayResult(
                    date=r.date,
                    median_price=r.median_price,
                    comps_collected=r.comps_collected,
                    comps_used=r.comps_used,
                    filter_stage=r.filter_stage,
                    flags=r.flags,
                    is_sampled=r.is_sampled,
                    is_weekend=r.is_weekend,
                    price_distribution=r.price_distribution,
                    top_comps=r.top_comps,
                    comp_prices=r.comp_prices,
                    error=r.error,
                )

            interp_start = time.time()
            all_day_objs = interpolate_missing_days(
                [_to_day_result(r) for r in sampled_results], all_nights
            )
            timings["interpolation_ms"] = round((time.time() - interp_start) * 1000)

            # Convert to dicts for the standard pipeline
            all_day_results: List[Dict[str, Any]] = []
            for dr in all_day_objs:
                all_day_results.append({
                    "date": dr.date,
                    "median_price": dr.median_price,
                    "comps_collected": dr.comps_collected,
                    "comps_used": dr.comps_used,
                    "below_similarity_floor": dr.below_similarity_floor,
                    "price_outliers_excluded": dr.price_outliers_excluded,
                    "price_outliers_downweighted": dr.price_outliers_downweighted,
                    "geo_excluded": dr.geo_excluded,
                    "price_band_excluded": dr.price_band_excluded,
                    "filter_stage": dr.filter_stage,
                    "flags": dr.flags,
                    "is_sampled": dr.is_sampled,
                    "is_weekend": dr.is_weekend,
                    "price_distribution": dr.price_distribution,
                    "top_comps": dr.top_comps,
                    "comp_prices": dr.comp_prices,
                    "error": dr.error,
                    "selection_mode": dr.selection_mode,
                    "pricing_confidence": dr.pricing_confidence,
                })

            timings["total_ms"] = round((time.time() - start_time) * 1000)

            # ── Guarantee secondary benchmarks appear in comparableListings ────
            # Secondary benchmark listings may never surface in Airbnb search
            # results (different area, unavailable, outside search radius).
            # For each secondary URL that never appeared in ANY day's top_comps,
            # inject a synthetic entry into the first sampled day using the
            # average price collected via secondary_prices across all days.
            if secondary_benchmark_urls and all_day_results:
                from worker.core.benchmark import _ROOM_ID_RE as _BM_ROOM_ID_RE
                from worker.core.similarity import comp_urls_match as _cmu

                # Collect IDs of secondary comps already seen in top_comps
                seen_sec_ids: set = set()
                for day_dict in all_day_results:
                    for tc in (day_dict.get("top_comps") or []):
                        if tc.get("isPinnedBenchmark") and tc.get("url"):
                            m = _BM_ROOM_ID_RE.search(tc["url"])
                            if m:
                                seen_sec_ids.add(m.group(1))

                for sec_url in secondary_benchmark_urls:
                    sec_m = _BM_ROOM_ID_RE.search(sec_url)
                    sec_id = sec_m.group(1) if sec_m else sec_url
                    if sec_id in seen_sec_ids:
                        continue  # already present in at least one day

                    # Collect avg price from secondary_comp_prices across all sampled days
                    sec_prices_found = [
                        r.secondary_comp_prices.get(sec_url)
                        for r in sampled_results
                        if r.secondary_comp_prices.get(sec_url) is not None
                    ]
                    avg_sec_price = (
                        round(sum(sec_prices_found) / len(sec_prices_found), 2)
                        if sec_prices_found else None
                    )

                    if avg_sec_price is None:
                        # Truly zero data — skip; we have nothing to show
                        logger.info(
                            f"[benchmark] Secondary {sec_url}: never found in search — omitting from comps"
                        )
                        continue

                    # Inject into the first non-interpolated day
                    inject_day = next(
                        (d for d in all_day_results if d.get("is_sampled")),
                        all_day_results[0],
                    )
                    inject_day.setdefault("top_comps", [])
                    inject_day.setdefault("comp_prices", {})
                    inject_day["top_comps"].append({
                        "id": sec_id,
                        "title": "Secondary benchmark listing",
                        "propertyType": target.property_type or "entire_home",
                        "accommodates": None,
                        "bedrooms": None,
                        "baths": None,
                        "nightlyPrice": avg_sec_price,
                        "currency": "USD",
                        "similarity": 0.95,
                        "rating": None,
                        "reviews": None,
                        "location": None,
                        "url": sec_url,
                        "isPinnedBenchmark": True,
                    })
                    inject_day["comp_prices"][sec_id] = avg_sec_price
                    logger.info(
                        f"[benchmark] Secondary {sec_url}: injected as synthetic comp "
                        f"(avg_price=${avg_sec_price}, n={len(sec_prices_found)} days)"
                    )

            # Aggregate benchmark transparency
            benchmark_info = aggregate_benchmark_transparency(benchmark_url, sampled_results)
            # Attach benchmark-to-target similarity (computed once above)
            if discount_info:
                benchmark_info["detectedDiscounts"] = discount_info

            if user_attributes:
                benchmark_info["benchmarkTargetSimilarity"] = bm_target_similarity
                benchmark_info["benchmarkMismatchLevel"] = bm_mismatch_level

            transparent = _build_daily_transparent_result(
                target=target,
                query_criteria=query_criteria,
                all_day_results=all_day_results,
                timings_ms=timings,
                source="benchmark",
                extraction_warnings=extraction_warnings,
                benchmark_info=benchmark_info,
                spec_backfill=_bm_backfill_meta,
                spec_extraction_meta=_bm_spec_extraction_meta,
                excluded_room_ids=excluded_room_ids,
            )
            _repair_incomplete_comparable_specs(client, transparent, extraction_warnings)
            _repair_suspicious_comparable_titles(client, transparent, extraction_warnings)

            logger.info(
                f"[benchmark] Pipeline complete: {len(sampled_results)} queries, "
                f"benchmarkUsed={benchmark_info['benchmarkUsed']}, "
                f"avg_adj={benchmark_info['marketAdjustmentPct']}%, "
                f"{timings['total_ms']}ms total"
            )

            return all_day_results, transparent
        finally:
            pass


# ---------------------------------------------------------------------------
# Address preprocessing helper
# ---------------------------------------------------------------------------


def _build_structured_search_location(
    city: Optional[str],
    state: Optional[str],
    postal_code: Optional[str],
) -> tuple:
    """
    Build a search-friendly Airbnb search string from structured fields.

    NOTE: This function is intentionally NOT called when a postalCode is
    present.  run_criteria_search() geocodes the ZIP to a canonical
    city/state first (via _geocode_postal_to_canonical), then uses that
    canonical location as the Airbnb search string.  This function is the
    fallback for the no-postal path.

    Priority:
      1. city + state  → "City, ST"   (state disambiguates city name)
      2. anything else → ""           (caller falls back to address parser)

    A city name alone is not returned here — it may be geographically
    ambiguous (e.g. "Belmont" in CA vs NC vs Long Beach).

    Returns:
        (location: str, confidence: str)
        Returns ("", "") when no unambiguous location can be built.
    """
    city = (city or "").strip() or None
    state = (state or "").strip() or None
    postal_code = (postal_code or "").strip() or None

    if city and state:
        return f"{city}, {state}", "high"
    # postal_code alone: caller should geocode it; don't return raw ZIP here
    return "", ""


def _is_us_zip(postal_code: str) -> bool:
    """Return True if postal_code looks like a US ZIP (5-digit or ZIP+4)."""
    return bool(re.match(r"^\d{5}(?:-\d{4})?$", postal_code.strip()))


def _abbrev_state_for_search(state: str) -> str:
    """
    Return the 2-letter US state code suitable for Airbnb search strings.

    Converts full US state names to their abbreviation so the Airbnb search
    URL uses the compact form (``"Belmont, CA"`` rather than
    ``"Belmont, California"``).

    * ``"California"`` → ``"CA"``
    * ``"CA"``         → ``"CA"``   (already abbreviated — unchanged)
    * ``"Queensland"`` → ``"Queensland"``  (non-US state — preserved as-is)
    * ``"台灣"``        → ``"台灣"``         (non-ASCII state — preserved as-is)
    * ``""``           → ``""``

    The geocode_result metadata is **not** touched — only the string returned
    here is used for building the Airbnb search query.
    """
    if not state:
        return state
    from worker.core.anchor_location import normalize_state
    norm = normalize_state(state)
    # normalize_state returns a 2-letter uppercase ASCII code for US states.
    # Chinese chars / non-ASCII pass .isalpha() but not .isupper(); ASCII
    # 2-letter non-US codes (e.g. "BC" for British Columbia) would pass, but
    # those are already short and acceptable.
    if len(norm) == 2 and norm.isupper() and norm.isascii():
        return norm
    return state  # non-US or unrecognised — preserve original


def _geocode_postal_to_canonical(
    postal_code: str,
    hint_city: Optional[str] = None,
    timeout: int = 3,
) -> Optional[Dict[str, Any]]:
    """
    Resolve a postal code to a canonical city / state / coords via Nominatim.

    For US ZIPs (5-digit or ZIP+4):
      - Appends ", United States" to the query so Nominatim restricts search
        to the US, preventing global misrouting (e.g. "94002" → Mexico).
      - Passes countrycodes="us" as an additional Nominatim filter.
      - Primary query:  "Belmont 94002, United States"  (with hint_city)
      - Fallback query: "94002, United States"           (bare ZIP)

    For non-US postal codes the query is sent without country restriction.

    Returns None on any failure — geocoding is always best-effort.  Callers
    must handle None and fall back gracefully.
    """
    try:
        from worker.core.geocode_details import geocode_address_details
    except ImportError:
        logger.warning("[criteria] geocode_details not available; skipping ZIP geocoding")
        return None

    us_zip = _is_us_zip(postal_code)
    country_suffix = ", United States" if us_zip else ""
    countrycodes = "us" if us_zip else None

    if hint_city:
        query = f"{hint_city} {postal_code}{country_suffix}"
    else:
        query = f"{postal_code}{country_suffix}"
    result = geocode_address_details(query, timeout=timeout, countrycodes=countrycodes)

    # If the city-hinted query fails, retry with just the ZIP (+ country context)
    if not result and hint_city:
        retry_query = f"{postal_code}{country_suffix}"
        result = geocode_address_details(retry_query, timeout=timeout, countrycodes=countrycodes)

    return result


def _extract_search_location(address: str) -> tuple:
    """
    Extract the most useful location token from a free-text address string.

    This is the FALLBACK used only when no structured location fields (city,
    state, postalCode) are available.  run_criteria_search() will geocode
    any ZIP code returned here before using it as an Airbnb search query —
    so it is safe to return a bare ZIP; callers handle the geocoding step.

    Rules (in priority order):
      1. Bare ZIP (digits only, 3–6 chars)   → return ZIP  (caller geocodes)
      2. Taiwanese address                   → extract city+district
      3. Comma-separated with trailing ZIP   → return ZIP  (caller geocodes)
      4. Comma-separated, city + state       → "City, ST"
      5. Anything else                       → as-is, medium confidence

    Returns:
        (search_location: str, confidence: str)  — "high" | "medium" | "low"
    """
    addr = address.strip()
    if not addr:
        return "", "low"

    # 1. Bare ZIP / postal code
    if re.match(r"^\d{3,6}$", addr):
        return addr, "high"

    # 2. Taiwanese address: e.g. "台北市信義區松山路123號" → "台北市信義區"
    tw_match = re.search(r"([^\s,]+?(?:市|縣)(?:[^\s,]+?(?:區|鄉|鎮|市))?)", addr)
    if tw_match:
        return tw_match.group(1), "high"

    # 3 & 4. Comma-separated address
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    if len(parts) >= 2:
        # Skip leading street component (starts with a digit)
        start = 1 if re.match(r"^\d", parts[0]) else 0
        city_parts = parts[start:]
        # Trailing bare ZIP wins — caller will geocode it
        if city_parts and re.match(r"^\d{3,6}$", city_parts[-1]):
            return city_parts[-1], "high"
        if city_parts:
            return ", ".join(city_parts[:2]), "high"

    # 5. Single token or unrecognised structure
    return addr, "medium"


# ---------------------------------------------------------------------------
# Anchor selection helper (criteria pass 1)
# ---------------------------------------------------------------------------

# Geo-filter radii used when selecting the anchor listing in criteria pass 1.
# Tighter than the default comp radius (30 km) because the anchor must be in
# the same neighbourhood as the target property.
_ANCHOR_RADIUS_TIGHT_KM: float = 20.0
_ANCHOR_RADIUS_FALLBACK_KM: float = 40.0
# City-centre proxy radius: smaller than the listing-level tight radius because
# geocoded city centres are less precise than per-listing map-pin coordinates.
# 15 km is chosen to keep nearby suburbs (Redwood City ~5 km, San Mateo ~6.5 km)
# while excluding more distant cities (San Francisco ~30 km, Sonoma ~87 km).
_ANCHOR_RADIUS_CITY_PROXY_KM: float = 15.0
# Minimum candidates that must survive the tight filter before we declare it
# "good enough" and skip the fallback.
_ANCHOR_MIN_GEO_CANDIDATES: int = 2


def _select_anchor_candidate(
    candidates: List[ListingSpec],
    user_spec: ListingSpec,
    target_lat: Optional[float],
    target_lng: Optional[float],
    *,
    target_city: Optional[str] = None,
    target_state: Optional[str] = None,
    n_listing_coords: int = 0,
    addr_confidence: str = "low",
) -> Tuple[ListingSpec, float, Dict[str, Any]]:
    """
    Choose the best anchor candidate for criteria pass 2.

    Four-phase pipeline
    -------------------

    Phase 1 — Geo pool (Paths A & B)
        Path A (``n_listing_coords > 0``): tight (20 km) then fallback (40 km)
        geo filter on page-embedded listing coordinates.

        Path B (``n_listing_coords == 0``, target has coords): geocode unique
        candidate location strings to city-centre proxies, apply 15 km filter.

        If neither produces a pool, fall through to Phase 2 as "text_bucket".

    Phase 2 — Location bucket classification
        Classify every candidate in the current pool into five buckets using
        the metro-cluster mapping:
          local_match       — same city as target
          nearby_market     — same metro cluster (approved nearby market)
          regional_mismatch — same state, different cluster
          far_mismatch      — different state
          unknown           — unparseable location (always pass-through)

        This classification is applied to ALL paths so bucket counts are always
        available for reporting, regardless of which path found the geo pool.

    Phase 3 — Controlled nearby-market expansion
        Priority 1: if ``local_match`` candidates exist → pool = local + unknown
        Priority 2: if no local but ``nearby_market`` exists → pool = nearby + unknown
                    (nearby_expansion_used = True)
        Priority 3: fallback based on ``addr_confidence``
          "high"   → fail-safe (use regional/far as last resort, flag degraded)
          "medium"/"low" → also allow regional_mismatch; far remains last resort

        For Paths A/B, this phase re-ranks the geo-filtered pool by bucket
        priority without removing candidates that are geo-confirmed nearby.
        For Path C (text-only), this phase IS the pool selection.

    Phase 4 — Structural similarity ranking
        ``filter_similar_candidates`` then ``similarity_score`` pick the best
        structural match within the location-constrained pool.

    Parameters
    ----------
    candidates       : pool of ListingSpec objects from the Airbnb search page
    user_spec        : synthetic target spec built from user criteria
    target_lat/lng   : geocoded target coordinates (may be None)
    target_city      : target city name (normalised internally)
    target_state     : target state, any form (normalised internally)
    n_listing_coords : how many candidates already have listing-level coords
    addr_confidence  : location confidence ("high" | "medium" | "low")

    Returns
    -------
    (best_match, best_score, anchor_debug)
    """
    from worker.core.anchor_location import (
        classify_candidate_location,
        geocode_candidate_cities,
        get_city_cluster,
        get_nearby_cities,
        normalize_city,
        normalize_location_text,
        normalize_state,
        parse_location_city_state,
    )

    # Pre-normalise target location once
    norm_city = normalize_city(target_city) if target_city else ""
    norm_state = normalize_state(target_state) if target_state else ""

    n_before_geo = len(candidates)
    geo_radius_used: Optional[float] = None
    geo_fallback = False
    geo_skipped = False
    n_after_geo = n_before_geo
    selection_mode = "no_geo"

    location_bucket_counts: Dict[str, int] = {
        "local_match": 0, "nearby_market": 0, "regional_mismatch": 0,
        "far_mismatch": 0, "unknown": 0,
    }
    fail_safe_triggered = False
    nearby_expansion_used = False
    allowed_nearby_cities: List[str] = []
    n_proxy_coords = 0

    pool = candidates  # default: all candidates

    # ── Phase 1A: listing-level coords ────────────────────────────────────────
    if target_lat is not None and target_lng is not None and n_listing_coords > 0:
        selection_mode = "listing_coords"

        tight_pool, tight_excluded = apply_geo_filter(
            candidates, target_lat, target_lng,
            max_radius_km=_ANCHOR_RADIUS_TIGHT_KM,
        )

        if len(tight_pool) >= _ANCHOR_MIN_GEO_CANDIDATES or tight_excluded == 0:
            pool = tight_pool
            geo_radius_used = _ANCHOR_RADIUS_TIGHT_KM
            n_after_geo = len(tight_pool)
            logger.info(
                f"[criteria/anchor] Path A: geo {_ANCHOR_RADIUS_TIGHT_KM}km: "
                f"{n_before_geo} → {n_after_geo} candidates"
            )
        else:
            geo_fallback = True
            geo_radius_used = _ANCHOR_RADIUS_FALLBACK_KM
            fallback_pool, _ = apply_geo_filter(
                candidates, target_lat, target_lng,
                max_radius_km=_ANCHOR_RADIUS_FALLBACK_KM,
            )
            logger.info(
                f"[criteria/anchor] Path A: tight radius sparse "
                f"({len(tight_pool)}); fallback {_ANCHOR_RADIUS_FALLBACK_KM}km"
                f" → {len(fallback_pool)} candidates"
            )
            if fallback_pool:
                pool = fallback_pool
                n_after_geo = len(fallback_pool)
            else:
                geo_skipped = True
                geo_radius_used = None
                selection_mode = "text_bucket"
                logger.warning(
                    "[criteria/anchor] Path A: nothing within fallback radius; "
                    "falling through to text-bucket filter"
                )

    # ── Phase 1B: city-proxy geocoding ────────────────────────────────────────
    elif target_lat is not None and target_lng is not None and n_listing_coords == 0:
        logger.info(
            "[criteria/anchor] Path B: no listing coords; "
            "attempting city-proxy geocoding"
        )
        n_proxy_coords = geocode_candidate_cities(
            candidates, max_unique_cities=10, timeout_per_city=2,
        )
        if n_proxy_coords > 0:
            proxy_pool, proxy_excluded = apply_geo_filter(
                candidates, target_lat, target_lng,
                max_radius_km=_ANCHOR_RADIUS_CITY_PROXY_KM,
            )
            if len(proxy_pool) >= _ANCHOR_MIN_GEO_CANDIDATES or proxy_excluded == 0:
                pool = proxy_pool
                geo_radius_used = _ANCHOR_RADIUS_CITY_PROXY_KM
                n_after_geo = len(proxy_pool)
                selection_mode = "city_proxy"
                logger.info(
                    f"[criteria/anchor] Path B: city-proxy {_ANCHOR_RADIUS_CITY_PROXY_KM}km: "
                    f"{n_before_geo} → {n_after_geo} candidates"
                )
            else:
                selection_mode = "text_bucket"
                logger.info(
                    f"[criteria/anchor] Path B: proxy pool sparse "
                    f"({len(proxy_pool)}); falling through to text-bucket"
                )
        else:
            selection_mode = "text_bucket"
            logger.info(
                "[criteria/anchor] Path B: city-proxy geocoding yielded no coords; "
                "falling through to text-bucket"
            )

    # ── Phase 2: Classify pool members by location bucket (all paths) ─────────
    # Always computed — gives audit trail regardless of which path was used.
    if norm_city and norm_state:
        allowed_nearby_cities = get_nearby_cities(norm_state, norm_city)

        bucketed: Dict[str, List[ListingSpec]] = {
            "local_match": [], "nearby_market": [], "regional_mismatch": [],
            "far_mismatch": [], "unknown": [],
        }
        for cand in pool:
            bkt = classify_candidate_location(
                getattr(cand, "location", "") or "", norm_city, norm_state,
            )
            bucketed[bkt].append(cand)

        for k in location_bucket_counts:
            location_bucket_counts[k] = len(bucketed[k])

        # ── Phase 3: Controlled nearby-market expansion ───────────────────────
        # For text-based paths (C / no_geo): this phase determines the pool.
        # For geo-based paths (A / B): pool is already geo-constrained; this
        # phase applies priority ordering within the geo pool.

        if selection_mode in ("text_bucket", "no_geo"):
            # Text path: staged expansion is the only location filter.
            selection_mode = "text_bucket"

            if bucketed["local_match"]:
                # Priority 1: target-city candidates found — no expansion needed
                pool = bucketed["local_match"] + bucketed["unknown"]
                nearby_expansion_used = False

            elif bucketed["nearby_market"]:
                # Priority 2: expand to approved nearby market
                pool = bucketed["nearby_market"] + bucketed["unknown"]
                nearby_expansion_used = True

            else:
                # Priority 3: no local or nearby → confidence-gated fallback
                nearby_expansion_used = True  # expansion attempted, nothing found

                if addr_confidence == "high":
                    # High confidence: refuse regional/far; fail-safe only
                    fail_safe_triggered = True
                    fallback = bucketed["regional_mismatch"] + bucketed["far_mismatch"]
                else:
                    # Medium/low: accept regional as degraded fallback
                    fallback = bucketed["regional_mismatch"] + bucketed["unknown"]
                    if not fallback:
                        fail_safe_triggered = True
                        fallback = bucketed["far_mismatch"]

                if fallback:
                    pool = fallback

            n_after_geo = len(pool)

            logger.info(
                f"[criteria/anchor] Phase 3 ({addr_confidence} conf, "
                f"expansion={'yes' if nearby_expansion_used else 'no'}): "
                f"local={location_bucket_counts['local_match']} "
                f"nearby={location_bucket_counts['nearby_market']} "
                f"regional={location_bucket_counts['regional_mismatch']} "
                f"far={location_bucket_counts['far_mismatch']} "
                f"unknown={location_bucket_counts['unknown']} "
                f"→ pool={len(pool)}"
                + (" FAIL-SAFE" if fail_safe_triggered else "")
            )

        else:
            # Geo path (A or B): pool already geo-constrained.
            # Priority-order within the pool: local > nearby > regional > far.
            # Prefer local if available; only use full pool if no local candidates.
            if bucketed["local_match"]:
                # Local candidates exist in geo pool — restrict to them
                refined = bucketed["local_match"] + bucketed["unknown"]
                pool = refined
                n_after_geo = len(pool)
                nearby_expansion_used = False
            elif bucketed["nearby_market"] or bucketed["unknown"]:
                # No local in geo pool; geo-confirmed nearby candidates compete
                nearby_expansion_used = True
                # pool stays as geo-filtered (don't narrow further — trust geo)
            else:
                # Geo pool has only regional/far — unusual given geo filter
                nearby_expansion_used = True
                # Still trust the geo filter result; just flag expansion

            logger.info(
                f"[criteria/anchor] Phase 3 geo-path "
                f"({'expansion' if nearby_expansion_used else 'local-only'}): "
                f"local={location_bucket_counts['local_match']} "
                f"nearby={location_bucket_counts['nearby_market']} "
                f"pool={len(pool)}"
            )
    # ── Phase 4: Structural similarity ranking ────────────────────────────────
    filtered, _filter_debug = filter_similar_candidates(user_spec, pool)
    if not filtered:
        logger.warning(
            f"[criteria/anchor] Similarity filter emptied pool of {len(pool)}; "
            "falling back to unfiltered pool"
        )
        filtered = pool

    scored = [(c, similarity_score(user_spec, c)) for c in filtered]
    scored.sort(key=lambda x: x[1], reverse=True)

    best_match, best_score = scored[0]
    anchor_dist = getattr(best_match, "distance_to_target_km", None)

    # Classify the selected anchor's bucket for reporting
    anchor_bucket: Optional[str] = None
    if norm_city and norm_state:
        anchor_bucket = classify_candidate_location(
            getattr(best_match, "location", "") or "", norm_city, norm_state,
        )
        # For geo paths: finalise nearby_expansion_used from the selected anchor
        if selection_mode in ("listing_coords", "city_proxy"):
            nearby_expansion_used = anchor_bucket not in (
                None, "local_match", "unknown"
            )

    # Compute location explainability fields for the selected anchor
    _anchor_raw_loc = (getattr(best_match, "location", "") or "").strip()
    _anchor_norm_loc, _anchor_norm_notes = normalize_location_text(_anchor_raw_loc)
    _anchor_city, _anchor_state_code = parse_location_city_state(
        _anchor_norm_loc if _anchor_norm_loc else _anchor_raw_loc
    )
    _anchor_cluster = (
        get_city_cluster(_anchor_state_code, _anchor_city)
        if _anchor_state_code and _anchor_city
        else None
    )

    logger.info(
        f"[criteria/anchor] Selected: {best_match.url} "
        f"score={best_score:.3f} "
        + (f"dist={anchor_dist:.1f}km " if anchor_dist is not None else "dist=unknown ")
        + f"mode={selection_mode} bucket={anchor_bucket} "
        + f"expansion={nearby_expansion_used}"
        + (f" norm={_anchor_norm_notes!r}" if _anchor_norm_notes else "")
    )

    return best_match, best_score, {
        "anchorCandidatesBeforeGeo": n_before_geo,
        "anchorCandidatesAfterGeo": n_after_geo,
        "anchorGeoRadiusKm": geo_radius_used,
        "anchorGeoFallback": geo_fallback,
        "anchorGeoSkipped": geo_skipped,
        "anchorStructuralScore": round(best_score, 3),
        "anchorDistanceKm": round(anchor_dist, 2) if anchor_dist is not None else None,
        "anchorHasTargetCoords": (target_lat is not None and target_lng is not None),
        "anchorSelectionMode": selection_mode,
        "anchorProxyCoordsAssigned": n_proxy_coords,
        "anchorLocationBuckets": location_bucket_counts,
        "anchorLocationBucket": anchor_bucket,
        "anchorFailSafeTriggered": fail_safe_triggered,
        "anchorNearbyExpansionUsed": nearby_expansion_used,
        "anchorAllowedNearbyCities": allowed_nearby_cities,
        "anchorTargetCityOnlyCount": location_bucket_counts["local_match"],
        "anchorNearbyMarketCount": location_bucket_counts["nearby_market"],
        "targetLocationConfidence": addr_confidence,
        "targetCanonicalCity": norm_city or None,
        "targetCanonicalState": norm_state or None,
        "anchorRawLocation": _anchor_raw_loc or None,
        "anchorNormalizedLocation": _anchor_norm_loc or _anchor_raw_loc or None,
        "anchorNormalizationNotes": _anchor_norm_notes or None,
        "anchorClusterId": _anchor_cluster,
    }


# ---------------------------------------------------------------------------
# Canonical target resolution helper (used by run_criteria_search)
# ---------------------------------------------------------------------------


def _resolve_canonical_target(
    candidates,                      # List[ListingSpec]
    raw_city: Optional[str],
    raw_state: Optional[str],
    addr_confidence: str,
    target_lat: Optional[float] = None,
    target_lng: Optional[float] = None,
) -> Tuple[Optional[str], Optional[str], str]:
    """
    Decide which city/state to use as the canonical target for anchor selection.

    High confidence → trust the structured address fields directly.
    Low / medium confidence → try to infer from the Airbnb first-page
    candidates (distance-first, then vote fallback).

    Returns ``(anchor_city, anchor_state, source)`` — safe to pass straight
    into ``_select_anchor_candidate`` as ``target_city`` / ``target_state``.
    ``source`` mirrors the ``targetCanonicalCitySource`` debug key.
    """
    if addr_confidence == "high":
        return raw_city, raw_state, "address"

    from worker.core.anchor_location import infer_canonical_target_from_candidates
    inf_city, inf_state, source = infer_canonical_target_from_candidates(
        candidates,
        fallback_city=raw_city,
        fallback_state=raw_state,
        target_lat=target_lat,
        target_lng=target_lng,
    )
    # Ensure we always return something, even when inference falls back
    return (inf_city or raw_city), (inf_state or raw_state), source


# ---------------------------------------------------------------------------
# Criteria-based search (Mode B)
# ---------------------------------------------------------------------------


def run_criteria_search(
    address: str,
    attributes: Dict[str, Any],
    checkin: str,
    checkout: str,
    cdp_url: str = "http://127.0.0.1:9222",
    top_k: int = 20,
    max_scroll_rounds: int = DAY_SCROLL_ROUNDS,
    max_cards: int = DAY_MAX_CARDS,
    max_runtime_seconds: int = 180,
    rate_limit_seconds: float = 1.0,
    cdp_connect_timeout_ms: int = 15000,
    preferred_comps: Optional[List[Dict[str, Any]]] = None,
    excluded_room_ids: Optional[set[str]] = None,
    target_lat: Optional[float] = None,
    target_lng: Optional[float] = None,
    max_radius_km: Optional[float] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    nightly_plan: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Criteria-based search: search Airbnb for listings matching the user's
    property criteria, find the most similar one, then use it as the anchor
    listing for a full day-by-day scrape via run_scrape().

    Two-pass pipeline:
      Pass 1 -- Search Airbnb by address, collect cards, rank by similarity
      Pass 2 -- Use best match's URL as anchor for run_scrape()

    Returns (daily_results, transparent_result).
    """

    start_time = time.time()
    timings: Dict[str, int] = {}
    base_origin = "https://www.airbnb.com"

    # ── Step 1: read structured location fields from attributes ──────────────
    _city = (attributes.get("city") or "").strip() or None
    _state = (attributes.get("state") or "").strip() or None
    _postal = (
        (attributes.get("postalCode") or attributes.get("postal_code") or "").strip() or None
    )

    # ── Step 2: resolve location to a canonical city/state for Airbnb search ─
    #
    # Strategy:
    #   A. postalCode present → geocode ZIP to canonical city/state/coords.
    #      Using raw ZIP as the Airbnb search query is unreliable — Airbnb
    #      may misroute bare ZIP codes to unrelated places (e.g. "94002" has
    #      matched San Carlos, Mexico in some sessions).  Geocoding the ZIP
    #      produces a canonical city/state that Airbnb resolves correctly.
    #   B. no postalCode, city + state → use "city, state" directly.
    #   C. no postalCode, city only   → address-string fallback (medium conf).
    #   D. nothing structured         → address-string fallback.
    #
    # In all cases where the fallback parser returns a bare ZIP, we also
    # geocode it so the Airbnb search is always city/state-based.

    geocode_result: Optional[Dict[str, Any]] = None
    city_zip_mismatch: Optional[str] = None
    geocode_query_used: Optional[str] = None  # logged in queryCriteria for debugging

    # Detect ZIP anywhere in the address string (for the all-fallback path D)
    _addr_zip_match = re.search(r"\b(\d{5})\b", address)
    _addr_zip = _addr_zip_match.group(1) if _addr_zip_match else None

    if _postal:
        # Path A: geocode the ZIP
        _postal_us = _is_us_zip(_postal)
        _postal_suffix = ", United States" if _postal_us else ""
        geocode_query_used = (
            f"{_city} {_postal}{_postal_suffix}" if _city else f"{_postal}{_postal_suffix}"
        )
        logger.info(f"[criteria] Geocoding postalCode={_postal!r} hint_city={_city!r}")
        geocode_result = _geocode_postal_to_canonical(_postal, hint_city=_city)

        if geocode_result:
            gc_city = geocode_result.get("city")
            gc_state = geocode_result.get("state")
            gc_lat = geocode_result.get("lat")
            gc_lng = geocode_result.get("lng")

            # Carry geocoded coords forward as target coords if not already set
            if target_lat is None and gc_lat is not None:
                target_lat = gc_lat
            if target_lng is None and gc_lng is not None:
                target_lng = gc_lng

            # Warn if user-supplied city disagrees with geocoded city
            if _city and gc_city and _city.lower() != gc_city.lower():
                city_zip_mismatch = (
                    f"User city {_city!r} ≠ geocoded city {gc_city!r} for ZIP {_postal!r}"
                )
                logger.warning(f"[criteria] {city_zip_mismatch}")

            # Build canonical search string from geocoded city + state.
            # Abbreviate full state names ("California" → "CA") so the Airbnb
            # search URL uses the compact form Airbnb resolves most reliably.
            if gc_city and gc_state:
                search_location = f"{gc_city}, {_abbrev_state_for_search(gc_state)}"
                addr_confidence = "high"
            elif gc_city:
                search_location = gc_city
                addr_confidence = "medium"
            else:
                # Geocode returned coords but no city — fall through to city+state
                search_location = ""
                addr_confidence = "low"
        else:
            logger.warning(
                f"[criteria] ZIP geocode failed for {_postal!r}; falling back to "
                "structured city/state or address parser"
            )
            geocode_result = None
            search_location = ""
            addr_confidence = "low"

        # Geocode failed or returned no city: try city+state, then address parser
        if not search_location:
            if _city and _state:
                search_location = f"{_city}, {_abbrev_state_for_search(_state)}"
                addr_confidence = "medium"
            elif _city:
                search_location = _city
                addr_confidence = "low"
            else:
                search_location, addr_confidence = _extract_search_location(address)

    elif _city and _state:
        # Path B: no ZIP, structured city + state
        search_location = f"{_city}, {_abbrev_state_for_search(_state)}"
        addr_confidence = "high"

    elif _city:
        # Path C: city only — low confidence, ambiguous
        search_location = _city
        addr_confidence = "low"

    else:
        # Path D: no structured fields — parse address string
        search_location, addr_confidence = _extract_search_location(address)

        # If the fallback parser returned a bare ZIP, geocode it too
        raw_is_zip = bool(re.match(r"^\d{3,6}$", search_location))
        if raw_is_zip:
            logger.info(
                f"[criteria] Address parser returned ZIP {search_location!r}; geocoding"
            )
            _raw_us = _is_us_zip(search_location)
            geocode_query_used = search_location + (", United States" if _raw_us else "")
            geocode_result = _geocode_postal_to_canonical(search_location)
            if geocode_result:
                gc_city = geocode_result.get("city")
                gc_state = geocode_result.get("state")
                if target_lat is None:
                    target_lat = geocode_result.get("lat")
                if target_lng is None:
                    target_lng = geocode_result.get("lng")
                if gc_city and gc_state:
                    search_location = f"{gc_city}, {_abbrev_state_for_search(gc_state)}"
                    addr_confidence = "high"
                elif gc_city:
                    search_location = gc_city
                    addr_confidence = "medium"
            # If geocode fails, search_location stays as the raw ZIP (best-effort)

    logger.info(
        f"[criteria] Final search_location={search_location!r} "
        f"confidence={addr_confidence} geocoded={geocode_result is not None}"
    )
    if addr_confidence == "low":
        logger.warning(
            f"[criteria] Low confidence location for address={address!r}. "
            "Results may be inaccurate."
        )

    is_zip = bool(re.match(r"^\d{3,6}$", search_location))
    search_mode = "zip" if is_zip else "city"

    # Extract preferred comps from attributes if not explicitly passed
    if preferred_comps is None:
        raw = attributes.get("preferredComps")
        preferred_comps = raw if isinstance(raw, list) else None

    # Build a synthetic target spec from user criteria
    user_city = (geocode_result.get("city") if geocode_result else None) or _city or ""
    user_state = (geocode_result.get("state") if geocode_result else None) or _state or ""
    user_country = (geocode_result.get("country") if geocode_result else None) or ""
    user_country_code = (geocode_result.get("country_code") if geocode_result else None) or ""

    user_spec = ListingSpec(
        url="",
        title=address or "User property",
        location=search_location,
        city=user_city,
        state=user_state,
        postal_code=_postal or "",
        country=user_country,
        country_code=user_country_code,
        accommodates=attributes.get("maxGuests"),
        bedrooms=attributes.get("bedrooms"),
        beds=attributes.get("bedrooms"),  # approximate beds = bedrooms
        baths=attributes.get("bathrooms"),
        property_type=attributes.get("propertyType", ""),
        lat=target_lat,
        lng=target_lng,
    )

    adults = min(attributes.get("maxGuests", 2), 16)

    total_nights = max(1, (dt.strptime(checkout, "%Y-%m-%d") - dt.strptime(checkin, "%Y-%m-%d")).days)

    query_criteria = {
        "locationBasis": search_location,
        "rawAddress": address,
        "searchMode": search_mode,
        "addressConfidence": addr_confidence,
        "structuredLocation": {
            "city": _city,
            "state": _state,
            "postalCode": _postal,
        },
        "geocodeResult": {
            "city": geocode_result.get("city") if geocode_result else None,
            "state": geocode_result.get("state") if geocode_result else None,
            "postalCode": geocode_result.get("postal_code") if geocode_result else None,
            "country": geocode_result.get("country") if geocode_result else None,
            "countryCode": geocode_result.get("country_code") if geocode_result else None,
            "lat": geocode_result.get("lat") if geocode_result else None,
            "lng": geocode_result.get("lng") if geocode_result else None,
        } if geocode_result else None,
        "geocodeQuery": geocode_query_used,
        "cityZipMismatch": city_zip_mismatch,
        "searchAdults": adults,
        "checkin": checkin,
        "checkout": checkout,
        "propertyTypeFilter": user_spec.property_type or None,
        "tolerances": {
            "accommodates": 3,
            "bedrooms": 2,
            "beds": 3,
            "baths": 1.5,
        },
    }

    client = AirbnbClient(
        {
            "AIRBNB_BASE_URL": base_origin,
            "CHECKIN": checkin,
            "CHECKOUT": checkout,
            "ADULTS": adults,
            "CDP_URL": cdp_url,
            "QUERY": search_location,
            "LOG_RAW_PAYLOADS": None,
        }
    )

    # Initialised before the playwright block so they're in scope for
    # the debug metadata section that runs after pass 2.
    coord_map: Dict[str, tuple] = {}
    n_coords_assigned = 0
    anchor_debug: Dict[str, Any] = {}

    _p1_max_cards = nightly_plan.max_cards if nightly_plan is not None else max_cards
    search_start = time.time()
    _clean_location = re.sub(r"\*+", "", str(search_location or "")).strip()
    _clean_location = re.sub(r"\s+", " ", _clean_location)
    _path_query = re.sub(r"\s*,\s*", ",", _clean_location)
    _query_location = re.sub(r"\s*,\s*", ", ", _clean_location)
    _search_path = f"/s/{quote(_path_query).replace('%2C', '--')}/homes"
    _search_params: Dict[str, Any] = {
        "date_picker_type": "calendar",
        "checkin": checkin,
        "checkout": checkout,
        "adults": adults,
        "query": _query_location,
        "search_type": "AUTOSUGGEST",
        "items_per_grid": _p1_max_cards,
    }
    if target_lat is not None:
        _search_params["center_lat"] = target_lat
    if target_lng is not None:
        _search_params["center_lng"] = target_lng
    criteria_search_url = f"{base_origin}{_search_path}?{urlencode(_search_params)}"
    query_criteria["criteriaSearchUrl"] = criteria_search_url
    logger.info("[criteria] Search URL: %s", criteria_search_url)

    _p1_overrides: Dict[str, Any] = {
        "checkin": checkin,
        "checkout": checkout,
        "adults": adults,
        "query": _query_location,
        "itemsPerGrid": _p1_max_cards,
        "searchUrl": criteria_search_url,
    }
    if target_lat is not None:
        _p1_overrides["centerLat"] = target_lat
    if target_lng is not None:
        _p1_overrides["centerLng"] = target_lng
    _, search_data = client.search_listings_with_overrides(_p1_overrides)
    timings["scroll_ms"] = round((time.time() - search_start) * 1000)
    listing_ids = parse_search_response(search_data)
    listing_context = parse_search_listing_context(search_data)
    logger.info(
        "[criteria] Pass 1 search: listing_ids=%d context_entries=%d",
        len(listing_ids), len(listing_context),
    )

    # Retry without guestFavorite if initial search is empty — dense urban/tech
    # markets (e.g. Mountain View, CA) may have very few Guest Favorites.
    if not listing_ids and client.guest_favorite_only:
        logger.info("[criteria] 0 results with guestFavorite=true; retrying without filter")
        _, search_data = client.search_listings_with_overrides({**_p1_overrides, "guestFavorite": False})
        listing_ids = parse_search_response(search_data)
        listing_context = parse_search_listing_context(search_data)
        logger.info(
            "[criteria] Retry (no guestFavorite): listing_ids=%d context_entries=%d",
            len(listing_ids), len(listing_context),
        )
    candidates: List[ListingSpec] = []
    for lid in listing_ids:
        row = listing_context.get(str(lid)) or {}
        nightly = row.get("nightly_price")
        if nightly is None:
            total = row.get("total_price")
            nights = int(row.get("price_nights") or 1)
            if isinstance(total, (int, float)) and total > 0 and nights > 0:
                normalized = normalize_raw_price(
                    total,
                    qualifier="total",
                    stay_nights=nights,
                    source="search",
                    produce_nightly_from_total=True,
                )
                nightly = normalized.nightly_price if normalized is not None else None
        candidates.append(
            ListingSpec(
                url=f"{base_origin}/rooms/{lid}",
                title=str(row.get("title") or ""),
                location=str(row.get("location") or search_location),
                nightly_price=nightly,
                accommodates=row.get("accommodates"),
                bedrooms=row.get("bedrooms"),
                beds=row.get("beds"),
                baths=row.get("baths"),
                property_type=str(row.get("property_type") or ""),
                lat=row.get("lat"),
                lng=row.get("lng"),
                rating=row.get("rating"),
                reviews=row.get("reviews"),
                amenities=list(row.get("amenities") or []),
            )
        )

    # ── Pass 1 anchor selection: drop user-blacklisted candidates ─────────
    # Otherwise an excluded comp could be picked as the criteria anchor and
    # then drag the entire pricing pipeline through it.  If the entire
    # candidate pool is excluded, we let the existing "no candidates" path
    # surface the error.
    if excluded_room_ids:
        pre = len(candidates)
        candidates = [
            c for c in candidates
            if build_comp_id(c.url or "") not in excluded_room_ids
        ]
        dropped = pre - len(candidates)
        if dropped > 0:
            logger.info(
                f"[criteria] anchor pool excluded_by_user={dropped} "
                f"(remaining {len(candidates)})"
            )

    priced_candidates = [c for c in candidates if c.url and isinstance(c.nightly_price, (int, float)) and c.nightly_price > 0]
    logger.info(
        "[criteria] candidates before price filter=%d after=%d",
        len(candidates), len(priced_candidates),
    )
    if not candidates:
        no_results_hint = (
            f"No listings found for ZIP code '{search_location}'. Try using the city name instead."
            if is_zip
            else f"No listings found in search results for '{search_location}'."
        )
        no_results_hint = f"{no_results_hint} Search URL: {criteria_search_url}"
        return [], {
            "targetSpec": None,
            "queryCriteria": query_criteria,
            "compsSummary": {"collected": 0, "afterFiltering": 0, "usedForPricing": 0, "filterStage": "empty", "topSimilarity": None, "avgSimilarity": None},
            "priceDistribution": None,
            "recommendedPrice": None,
            "comparableListings": None,
            "debug": {"source": "criteria", "error": no_results_hint, "searchMode": search_mode, "extractionWarnings": [], "timingsMs": timings},
        }
    if not priced_candidates:
        logger.info(
            "[criteria] pass-1 search returned %s listing ids but no initial price fields; "
            "continuing anchor selection using structural metadata only",
            len(candidates),
        )

    _target_raw_city = _city
    _target_raw_state = _state
    _anchor_target_city, _anchor_target_state, _target_canonical_city_source = _resolve_canonical_target(
        candidates,
        _city,
        _state,
        addr_confidence,
        target_lat=target_lat,
        target_lng=target_lng,
    )
    best_match, best_score, anchor_debug = _select_anchor_candidate(
        candidates,
        user_spec,
        target_lat,
        target_lng,
        target_city=_anchor_target_city,
        target_state=_anchor_target_state,
        n_listing_coords=n_coords_assigned,
        addr_confidence=addr_confidence,
    )
    anchor_debug["targetRawCity"] = _target_raw_city or None
    anchor_debug["targetRawState"] = _target_raw_state or None
    anchor_debug["targetCanonicalCitySource"] = _target_canonical_city_source
    # Pass 2: Use best match as anchor for full day-by-day scrape
    elapsed = time.time() - start_time
    remaining = max_runtime_seconds - elapsed

    if remaining < 30:
        # Not enough time for day-by-day scrape; return empty daily_results
        # (process_job will fail the job with a user-facing error)
        logger.info(f"[criteria] Low time ({remaining:.0f}s), returning empty daily_results")
        return [], _empty_transparent(
            "criteria_direct",
            f"Insufficient time for day-by-day queries ({remaining:.0f}s remaining)",
        )

    # Full second pass via run_scrape (day-by-day)
    logger.info(
        f"[criteria] Running day-by-day scrape on anchor: {best_match.url} "
        f"({remaining:.0f}s remaining)"
    )
    daily_results, scrape_transparent = run_scrape(
        listing_url=best_match.url,
        checkin=checkin,
        checkout=checkout,
        cdp_url=cdp_url,
        adults=adults,
        top_k=top_k,
        max_scroll_rounds=max_scroll_rounds,
        max_cards=max_cards,
        max_runtime_seconds=int(remaining),
        rate_limit_seconds=rate_limit_seconds,
        cdp_connect_timeout_ms=cdp_connect_timeout_ms,
        preferred_comps=preferred_comps,
        excluded_room_ids=excluded_room_ids,
        target_lat=target_lat,
        target_lng=target_lng,
        max_radius_km=max_radius_km,
        progress_callback=progress_callback,
        nightly_plan=nightly_plan,
        fallback_attributes=attributes,
        fallback_address=address,
        target_spec_override=user_spec,
        query_criteria_override=query_criteria,
    )

    # Merge criteria-specific info into the transparent result
    scrape_transparent["debug"]["source"] = "criteria"
    scrape_transparent["debug"]["criteria_search_ms"] = round(elapsed * 1000)
    scrape_transparent["debug"]["anchor_url"] = best_match.url
    scrape_transparent["debug"]["anchor_score"] = round(best_score, 3)
    scrape_transparent["debug"]["initial_candidates"] = len(candidates)
    scrape_transparent["debug"]["initial_priced_candidates"] = len(priced_candidates)
    # Anchor geo-selection metadata (filled by _select_anchor_candidate)
    scrape_transparent["debug"]["anchorCoordMapSize"] = len(coord_map)
    scrape_transparent["debug"]["anchorCoordsAssigned"] = n_coords_assigned
    scrape_transparent["debug"].update(anchor_debug)
    try:
        target_spec_payload = scrape_transparent.get("targetSpec") or {}
        anchor_location = (
            scrape_transparent["debug"].get("anchorNormalizedLocation")
            or scrape_transparent["debug"].get("anchorRawLocation")
            or ""
        )
        if isinstance(target_spec_payload, dict) and isinstance(anchor_location, str) and anchor_location.strip():
            target_spec_payload["location"] = anchor_location.strip()
            scrape_transparent["targetSpec"] = target_spec_payload
    except Exception:
        pass

    return daily_results, scrape_transparent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _preflight_listing_exists(client: Any, listing_url: str) -> Tuple[bool, Dict[str, Any]]:
    """
    Return (missing, debug) for a lightweight listing page preflight.

    Only explicit 404 evidence stops the scrape. Navigation failures are
    inconclusive because target extraction has its own retry/fallback handling.
    """
    room_id = extract_listing_id_from_url(str(listing_url or ""))
    debug: Dict[str, Any] = {
        "checked": False,
        "roomId": str(room_id or "") or None,
        "missing": False,
    }
    if not room_id or not hasattr(client, "browse_url_html"):
        debug["reason"] = "skipped"
        return False, debug

    probe_url = f"https://airbnb.com/rooms/{room_id}"
    debug["checked"] = True
    debug["probeUrl"] = probe_url
    try:
        result = client.browse_url_html(
            probe_url,
            label="listing_preflight",
            wait_until="domcontentloaded",
            timeout=8000,
        )
    except Exception as exc:
        debug["reason"] = "navigation_failed"
        debug["error"] = str(exc)[:200]
        return False, debug

    status = result.get("status") if isinstance(result, dict) else None
    final_url = result.get("final_url") if isinstance(result, dict) else None
    html = result.get("html") if isinstance(result, dict) else ""
    text = str(html or "").casefold()
    html_len = len(str(html or ""))
    status_is_404 = int(status) == 404 if isinstance(status, (int, float, str)) and str(status).isdigit() else False
    # Require specific title/heading patterns — Airbnb valid listing pages contain
    # "oops" and "404" in embedded JS error templates, causing false positives when
    # checking raw HTML text. A real 404 page has a <title> or <h1> with "404" or
    # "page not found" and is much smaller than a normal listing page.
    _oops_title = bool(
        re.search(r"<title[^>]*>[^<]*404[^<]*</title>", str(html or ""), re.IGNORECASE)
        or re.search(r"<h1[^>]*>[^<]*404[^<]*</h1>", str(html or ""), re.IGNORECASE)
        or "page not found" in text
        or "this listing is no longer available" in text
    )
    html_has_oops_404 = _oops_title and html_len < 150_000
    missing = bool(status_is_404 or html_has_oops_404)
    debug.update({
        "status": int(status) if isinstance(status, (int, float, str)) and str(status).isdigit() else status,
        "finalUrl": final_url,
        "statusIs404": status_is_404,
        "htmlHasOops404": html_has_oops_404,
        "htmlLen": html_len,
        "missing": missing,
    })
    return missing, debug


def _empty_transparent(source: str, error: str) -> Dict[str, Any]:
    """Return a minimal transparent_result dict for error/empty cases."""
    return {
        "targetSpec": None,
        "queryCriteria": None,
        "compsSummary": None,
        "priceDistribution": None,
        "recommendedPrice": None,
        "comparableListings": None,
        "debug": {
            "source": source,
            "error": error,
            "extractionWarnings": [],
            "timingsMs": {},
        },
    }


