"""
Similarity scoring and filtering for comparable listings.

Compares candidate listings against a target listing using weighted
feature matching (property_type, bedrooms, amenities). Supports multi-tier
filtering (strict/medium/relaxed).

Extracted from price_estimator.py for modularity.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from worker.scraper.target_extractor import ListingSpec

# ── Similarity floor ──────────────────────────────────────────────────────────

SIMILARITY_FLOOR: float = 0.40
"""
Minimum raw similarity score for a comp to enter pricing or display.
Applied after filter tiers, before recommend_price().
"""

# ── URL matching for preferred comparable ─────────────────────────

_ROOM_ID_RE = re.compile(r"/rooms/(\d+)")
_AMENITY_TOKEN_RE = re.compile(r"[a-z0-9]+")
_AMENITY_SIMILARITY_WEIGHT: float = 3.0
_LOCATION_SIMILARITY_WEIGHT: float = 5.0

# Every amenity gets at least this baseline weight. We then override specific
# amenities with larger weights where market pricing impact is typically higher.
# Baseline is intentionally tiny so non-priority amenities barely move score.
_AMENITY_BASE_WEIGHT: float = 0.05
_AMENITY_ALIASES: Dict[str, str] = {
    "wi_fi": "wifi",
    "wireless_internet": "wifi",
    "internet": "wifi",
    "air_conditioning": "ac",
    "central_air_conditioning": "ac",
    "portable_air_conditioning": "ac",
    "a_c": "ac",
    "laundry": "washer",
    "washing_machine": "washer",
    "jacuzzi": "hot_tub",
    "jacuzzi_tub": "hot_tub",
    "free_parking_on_premises": "free_parking",
    "parking_on_premises": "free_parking",
    "allows_pets": "pets_allowed",
    "pet_friendly": "pets_allowed",
    "beach_view": "beach_access",
    "lakefront": "lake_access",
    "water_front": "waterfront",
    "barbecue": "bbq",
    "grill": "bbq",
    "fitness": "gym",
    "ski_in_out": "ski_in_ski_out",
}
_AMENITY_WEIGHT_OVERRIDES: Dict[str, float] = {
    # Premium location/value drivers: these should dominate amenity matching.
    "beach_access": 12.0,
    "beachfront": 12.0,
    "waterfront": 10.0,
    "lake_access": 8.0,
    "ski_in_ski_out": 8.0,
    # High-value leisure amenities.
    "private_pool": 7.0,
    "infinity_pool": 6.0,
    "heated_pool": 5.0,
    "pool": 4.0,
    "private_hot_tub": 6.0,
    "hot_tub": 3.0,
    # Keep common utilities relatively low-impact.
    "guest_favorite": 2.0,
    "ev_charger": 0.5,
    "kitchen": 0.25,
    "ac": 0.25,
    "washer": 0.20,
    "dryer": 0.20,
    "free_parking": 0.15,
    "pets_allowed": 0.15,
}


def _normalize_amenity_key(value: str) -> str:
    tokens = _AMENITY_TOKEN_RE.findall(str(value or "").casefold())
    if not tokens:
        return ""
    normalized = "_".join(tokens)
    return _AMENITY_ALIASES.get(normalized, normalized)


def _normalize_amenity_set(values: List[str]) -> set[str]:
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        key = _normalize_amenity_key(value)
        if key:
            normalized.add(key)
    return normalized


def _amenity_weight(amenity_key: str) -> float:
    return _AMENITY_WEIGHT_OVERRIDES.get(amenity_key, _AMENITY_BASE_WEIGHT)


def _weighted_amenity_overlap(target_set: set[str], cand_set: set[str]) -> float:
    union = target_set | cand_set
    if not union:
        return 0.0

    overlap = target_set & cand_set
    overlap_weight = sum(_amenity_weight(name) for name in overlap)
    union_weight = sum(_amenity_weight(name) for name in union)
    if union_weight <= 0:
        return 0.0

    return max(0.0, min(1.0, overlap_weight / union_weight))


def extract_airbnb_room_id(url: str) -> Optional[str]:
    """Extract the numeric room ID from an Airbnb listing URL."""
    m = _ROOM_ID_RE.search(url or "")
    return m.group(1) if m else None


def comp_urls_match(url_a: str, url_b: str) -> bool:
    """
    Return True if two URLs refer to the same Airbnb listing.

    Matching strategy (in priority order):
    1. Airbnb room ID extraction — most reliable
    2. Normalized URL comparison (strip query params / trailing slash)
    """
    if not url_a or not url_b:
        return False
    id_a = extract_airbnb_room_id(url_a)
    id_b = extract_airbnb_room_id(url_b)
    if id_a and id_b:
        return id_a == id_b
    # Fallback: normalise and compare
    def _norm(u: str) -> str:
        return u.strip().rstrip("/").split("?")[0].lower()
    return _norm(url_a) == _norm(url_b)


def similarity_score_with_breakdown(target: ListingSpec, cand: ListingSpec) -> Tuple[float, Dict[str, Any]]:
    score = 0.0
    weight_sum = 0.0
    breakdown: Dict[str, Any] = {}

    def norm_city(spec: ListingSpec) -> str:
        city = (spec.city or "").strip().lower()
        if city:
            return city
        location = (spec.location or "").strip()
        if not location:
            return ""
        return location.split(",")[0].strip().lower()

    def add_num(name: str, t, c, w: float, tol: float):
        nonlocal score, weight_sum
        weight_sum += w
        if t is None or c is None:
            contrib = 0.35 * w
            score += contrib
            breakdown[name] = {"weight": w, "raw_score": 0.35, "contribution": contrib, "target_val": t, "cand_val": c}
            return
        diff = abs(float(t) - float(c))
        s = max(0.0, 1.0 - diff / tol)
        contrib = s * w
        score += contrib
        breakdown[name] = {"weight": w, "raw_score": s, "contribution": contrib, "target_val": t, "cand_val": c}

    def add_reviews(name: str, t, c, w: float):
        nonlocal score, weight_sum
        weight_sum += w
        if t is None or c is None:
            contrib = 0.35 * w
            score += contrib
            breakdown[name] = {"weight": w, "raw_score": 0.35, "contribution": contrib, "target_val": t, "cand_val": c}
            return
        try:
            t_log = math.log1p(max(0.0, float(t)))
            c_log = math.log1p(max(0.0, float(c)))
        except Exception:
            contrib = 0.35 * w
            score += contrib
            breakdown[name] = {"weight": w, "raw_score": 0.35, "contribution": contrib, "target_val": t, "cand_val": c}
            return
        hi = max(t_log, c_log)
        lo = min(t_log, c_log)
        s = 1.0 if hi <= 0 else (lo / hi)
        s = max(0.0, min(1.0, s))
        contrib = s * w
        score += contrib
        breakdown[name] = {"weight": w, "raw_score": s, "contribution": contrib, "target_val": t, "cand_val": c}

    add_num("beds", target.beds, cand.beds, w=2.5, tol=3.0)
    add_num("accommodates", target.accommodates, cand.accommodates, w=2.5, tol=3.0)
    add_num("bedrooms", target.bedrooms, cand.bedrooms, w=2.5, tol=2.0)
    add_num("baths", target.baths, cand.baths, w=2.0, tol=1.5)
    add_num("rating", target.rating, cand.rating, w=2.0, tol=1.0)
    add_reviews("reviews", target.reviews, cand.reviews, w=2.0)

    # Property-type
    weight_sum += 3.0
    pt_s = 0.35
    if target.property_type and cand.property_type:
        pt_s = 1.0 if target.property_type == cand.property_type else 0.0
    contrib = pt_s * 3.0
    score += contrib
    breakdown["property_type"] = {"weight": 3.0, "raw_score": pt_s, "contribution": contrib, "target_val": target.property_type, "cand_val": cand.property_type}

    # Amenity overlap
    weight_sum += _AMENITY_SIMILARITY_WEIGHT
    t_set = _normalize_amenity_set(list(target.amenities or []))
    c_set = _normalize_amenity_set(list(cand.amenities or []))
    am_s = 0.35
    if t_set and c_set:
        am_s = _weighted_amenity_overlap(t_set, c_set)
    contrib = am_s * _AMENITY_SIMILARITY_WEIGHT
    score += contrib
    breakdown["amenities"] = {"weight": _AMENITY_SIMILARITY_WEIGHT, "raw_score": am_s, "contribution": contrib, "overlap_count": len(t_set & c_set) if t_set and c_set else 0, "union_count": len(t_set | c_set) if t_set and c_set else 0}

    # Address/city
    weight_sum += _LOCATION_SIMILARITY_WEIGHT
    t_city = norm_city(target)
    c_city = norm_city(cand)
    loc_s = 0.0
    if t_city and c_city and t_city == c_city:
        loc_s = 1.0
    contrib = loc_s * _LOCATION_SIMILARITY_WEIGHT
    score += contrib
    breakdown["location"] = {"weight": _LOCATION_SIMILARITY_WEIGHT, "raw_score": loc_s, "contribution": contrib, "target_city": t_city, "cand_city": c_city, "match": loc_s == 1.0}

    final_score = 0.0 if weight_sum <= 0 else score / weight_sum
    breakdown["total_score"] = final_score
    breakdown["weight_sum"] = weight_sum

    return final_score, breakdown


def similarity_score(target: ListingSpec, cand: ListingSpec, debug: bool = False) -> float | Tuple[float, Dict[str, Any]]:
    """
    Compute a 0-1 similarity score between target and candidate listings.

    Weights (priority order requested):
      - property_type: 3.0  (categorical; mismatch → 0.0, unknown → partial)
      - beds:          2.5  (tolerance 3)
      - accommodates:  2.5  (tolerance 3)
      - bedrooms:      2.5  (tolerance 2)
      - baths:         2.0  (tolerance 1.5)
      - rating:        2.0  (tolerance 1.0)
      - reviews:       2.0  (log-scaled count similarity)
      - address(city): 5.0  (city match = 1.0, else 0.0)
      - amenities:     3.0  (weighted Jaccard overlap; premium-heavy)

    Property-type mismatch scores 0.0 (not 0.15) because the hard gate in
    filter_similar_candidates already blocks clear type conflicts; this
    ensures the score accurately reflects structural similarity.
    """
    final_score, breakdown = similarity_score_with_breakdown(target, cand)
    if debug:
        return final_score, breakdown
    return final_score


def _within_tolerance(
    target_val: Optional[float],
    cand_val: Optional[float],
    tol: float,
) -> bool:
    if target_val is None or cand_val is None:
        return True
    return abs(float(target_val) - float(cand_val)) <= tol


def _passes_property_type_gate(target: ListingSpec, cand: ListingSpec) -> bool:
    """
    Hard gate: reject comps whose type is mutually exclusive with the target.

    Applied to every filter tier — even relaxed — so type mismatches never
    contaminate pricing regardless of how few comps are found.

    Rules:
      - entire_home target  → rejects private_room / shared_room comps
      - private_room target → rejects entire_home comps
      - Unknown comp type   → allowed (can't reject what we can't read)
      - Unknown target type → no gate applied
    """
    if not target.property_type or not cand.property_type:
        return True
    if target.property_type == "entire_home" and cand.property_type in ("private_room", "shared_room"):
        return False
    if target.property_type == "private_room" and cand.property_type == "entire_home":
        return False
    return True


def filter_similar_candidates(
    target: ListingSpec,
    candidates: List[ListingSpec],
) -> Tuple[List[ListingSpec], Dict[str, Any]]:
    """
    Keep candidates structurally similar to the target listing.

    Tiers (V1):
      1) Strict:  property_type gate + tight tolerances
                  requires bedrooms + accommodates non-null; needs >= 6 comps
      2) Medium:  property_type gate + relaxed tolerances
                  requires bedrooms + accommodates non-null; needs >= 4 comps
      3) Relaxed: property_type gate + broad tolerances (replaces fallback_all)
                  allows missing bedrooms/accommodates; no minimum count
                  returns stage="insufficient_data" when 0 comps survive

    Returns (filtered_list, filter_metadata).
    """
    total = len(candidates)
    if total == 0:
        return [], {"stage": "insufficient_data", "total_candidates": 0, "filtered_candidates": 0}

    # Property-type hard gate applies to all tiers.
    type_gated = [c for c in candidates if _passes_property_type_gate(target, c)]

    # ── Tier 1: Strict ───────────────────────────────────────────────────────
    strict = [
        c for c in type_gated
        if c.bedrooms is not None
        and c.accommodates is not None
        and _within_tolerance(target.accommodates, c.accommodates, 2)
        and _within_tolerance(target.bedrooms, c.bedrooms, 1)
        and _within_tolerance(target.beds, c.beds, 2)
        and _within_tolerance(target.baths, c.baths, 1)
    ]
    if len(strict) >= 6:
        return strict, {
            "stage": "strict",
            "total_candidates": total,
            "filtered_candidates": len(strict),
        }

    # ── Tier 2: Medium ───────────────────────────────────────────────────────
    medium = [
        c for c in type_gated
        if c.bedrooms is not None
        and c.accommodates is not None
        and _within_tolerance(target.accommodates, c.accommodates, 3)
        and _within_tolerance(target.bedrooms, c.bedrooms, 2)
        and _within_tolerance(target.baths, c.baths, 1.5)
    ]
    if len(medium) >= 4:
        return medium, {
            "stage": "medium",
            "total_candidates": total,
            "filtered_candidates": len(medium),
        }

    # ── Tier 3: Relaxed (replaces fallback_all) ──────────────────────────────
    # Still property-type gated. Allows missing bedrooms/accommodates
    # (_within_tolerance returns True when either value is None).
    relaxed = [
        c for c in type_gated
        if _within_tolerance(target.accommodates, c.accommodates, 5)
        and _within_tolerance(target.bedrooms, c.bedrooms, 3)
        and _within_tolerance(target.baths, c.baths, 2)
    ]
    if len(relaxed) == 0:
        return [], {
            "stage": "insufficient_data",
            "total_candidates": total,
            "filtered_candidates": 0,
        }
    return relaxed, {
        "stage": "relaxed",
        "total_candidates": total,
        "filtered_candidates": len(relaxed),
    }
