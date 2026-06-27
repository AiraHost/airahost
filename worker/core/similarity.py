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

SIMILARITY_FLOOR: float = 0.50
"""
Minimum raw similarity score for a comp to enter pricing or display.
Applied after filter tiers, before recommend_price().
Comps must score above 50% similarity to the target (priority #2, after the
8-mile geographic cutoff and before the 20-comp cap).
"""

# ── URL matching for preferred comparable ─────────────────────────

_ROOM_ID_RE = re.compile(r"/rooms/(\d+)")
_AMENITY_TOKEN_RE = re.compile(r"[a-z0-9]+")

# ── Component weights ─────────────────────────────────────────────────────────
# CMA pricing comps. Candidates are pre-filtered to an 8-mile radius, so Location
# ranks proximity (closest = best) rather than gating out other cities.
_W_LOCATION: float = 6.0       # lat/lng Haversine, steep decay over 0-8 miles
_W_ACCOMMODATES: float = 3.5   # primary size metric, tolerance 2
_W_BEDROOMS: float = 3.0       # near-hard constraint (0 -> 1.0, 1 -> 0.5, >1 -> 0)
_W_PROPERTY_TYPE: float = 3.0  # similarity matrix, not binary
_W_AMENITIES: float = 3.0      # premium-weighted Jaccard overlap
_W_BATHS: float = 2.0          # tolerance 1.5
_W_QUALITY: float = 2.0        # combined rating + review volume (Bayesian)
_W_BEDS: float = 1.0           # minor layout tie-breaker, tolerance 3

# Partial score assigned when a feature is missing on either side. Location is
# exempt — it is mandatory and scores 0.0 when coordinates are unavailable.
_PARTIAL_MISSING: float = 0.5

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


# ── Location (Haversine) ──────────────────────────────────────────────────────

_EARTH_RADIUS_MILES: float = 3958.8
_KM_TO_MILES: float = 0.621371
_MAX_RADIUS_MILES: float = 8.0


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two lat/lng points, in miles."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2.0) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2.0) ** 2
    return 2.0 * _EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


def _location_distance_miles(target: ListingSpec, cand: ListingSpec) -> Optional[float]:
    """
    Distance from target to candidate in miles, or None when it cannot be
    determined. Prefers exact lat/lng; falls back to the precomputed
    distance_to_target_km set by the 8-mile geo filter.
    """
    if (
        target.lat is not None and target.lng is not None
        and cand.lat is not None and cand.lng is not None
    ):
        try:
            return haversine_miles(
                float(target.lat), float(target.lng), float(cand.lat), float(cand.lng)
            )
        except (TypeError, ValueError):
            pass
    d_km = getattr(cand, "distance_to_target_km", None)
    if isinstance(d_km, (int, float)):
        return float(d_km) * _KM_TO_MILES
    return None


def location_score(distance_miles: float) -> float:
    """
    Steep proximity decay over the 0-8 mile window: ~1.0 within a mile, dropping
    sharply past ~5 miles and reaching 0.0 at the 8-mile edge.
    """
    d = max(0.0, float(distance_miles))
    return max(0.0, 1.0 - (d / _MAX_RADIUS_MILES) ** 1.5)


# ── Property-type similarity matrix ───────────────────────────────────────────

# Building-type groups: in-group cross-matches (e.g. apartment<->condo) score 0.8.
_PROPERTY_TYPE_GROUPS: Dict[str, str] = {
    "apartment": "apartment_like",
    "condo": "apartment_like",
    "condominium": "apartment_like",
    "loft": "apartment_like",
    "rental_unit": "apartment_like",
    "serviced_apartment": "apartment_like",
    "aparthotel": "apartment_like",
    "house": "house_like",
    "villa": "house_like",
    "townhouse": "house_like",
    "cottage": "house_like",
    "cabin": "house_like",
    "bungalow": "house_like",
    "guesthouse": "house_like",
    "guest_suite": "house_like",
}


def _canon_property_type(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _property_type_category(canon: str) -> str:
    """'room' for private/shared/hotel rooms, otherwise 'entire'."""
    if (
        "private_room" in canon
        or "shared_room" in canon
        or "hotel_room" in canon
        or canon == "room"
        or canon.endswith("_room")
    ):
        return "room"
    return "entire"


def _property_type_building_group(canon: str) -> Optional[str]:
    base = canon[len("entire_"):] if canon.startswith("entire_") else canon
    return _PROPERTY_TYPE_GROUPS.get(base)


def property_type_similarity(target_type: Any, cand_type: Any) -> Optional[float]:
    """
    Similarity matrix for property types (returns None when either is unknown):

      * exact match                                   -> 1.0
      * apartment/condo/loft cross-match              -> 0.8
      * house/villa/townhouse cross-match             -> 0.8
      * generic entire-place vs a specific one        -> 0.8
      * different entire building groups (apt vs house)-> 0.5
      * private vs shared room                        -> 0.5
      * any entire place vs a room                    -> 0.0
    """
    t = _canon_property_type(target_type)
    c = _canon_property_type(cand_type)
    if not t or not c:
        return None
    if t == c:
        return 1.0
    t_cat = _property_type_category(t)
    c_cat = _property_type_category(c)
    if t_cat != c_cat:
        return 0.0  # entire place vs room — not a pricing substitute
    if t_cat == "room":
        return 0.5  # private vs shared room
    t_grp = _property_type_building_group(t)
    c_grp = _property_type_building_group(c)
    if t_grp and c_grp:
        return 0.8 if t_grp == c_grp else 0.5
    # One side is the generic "entire_home" with no specific building type.
    return 0.8


# ── Trust / quality (rating + review volume) ──────────────────────────────────

_QUALITY_PRIOR_MEAN: float = 4.7   # platform-typical average rating (0-5 scale)
_QUALITY_PRIOR_COUNT: float = 5.0  # pseudo-reviews shrinking low-volume props


def quality_score(rating: Optional[float], reviews: Optional[float]) -> Optional[float]:
    """
    Bayesian-average rating in [0, 1] that blends star rating with review volume,
    so a 4.9 with 500 reviews outranks a 5.0 with 2. Returns None when the rating
    is unknown (the caller then applies the partial-missing score).
    """
    if rating is None:
        return None
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return None
    n = 0.0
    if reviews is not None:
        try:
            n = max(0.0, float(reviews))
        except (TypeError, ValueError):
            n = 0.0
    bayes = (_QUALITY_PRIOR_COUNT * _QUALITY_PRIOR_MEAN + n * r) / (_QUALITY_PRIOR_COUNT + n)
    return max(0.0, min(1.0, bayes / 5.0))


def _bedrooms_raw(target_val: Optional[float], cand_val: Optional[float]) -> float:
    """Bedrooms are a near-hard constraint: 0 diff -> 1.0, 1 -> 0.5, >1 -> 0.0."""
    if target_val is None or cand_val is None:
        return _PARTIAL_MISSING
    diff = abs(float(target_val) - float(cand_val))
    if diff == 0:
        return 1.0
    if diff <= 1:
        return 0.5
    return 0.0


def _tolerance_raw(target_val: Optional[float], cand_val: Optional[float], tol: float) -> float:
    """Linear tolerance decay: 1.0 at equal, 0.0 once the gap reaches `tol`."""
    if target_val is None or cand_val is None:
        return _PARTIAL_MISSING
    return max(0.0, 1.0 - abs(float(target_val) - float(cand_val)) / tol)


def similarity_score_with_breakdown(target: ListingSpec, cand: ListingSpec) -> Tuple[float, Dict[str, Any]]:
    score = 0.0
    weight_sum = 0.0
    breakdown: Dict[str, Any] = {}

    def add(name: str, weight: float, raw: float, extra: Optional[Dict[str, Any]] = None) -> None:
        nonlocal score, weight_sum
        weight_sum += weight
        contrib = raw * weight
        score += contrib
        entry: Dict[str, Any] = {"weight": weight, "raw_score": raw, "contribution": contrib}
        if extra:
            entry.update(extra)
        breakdown[name] = entry

    # Location (6.0) — mandatory: missing coordinates score 0.0, never the partial.
    dist_mi = _location_distance_miles(target, cand)
    if dist_mi is None:
        add("location", _W_LOCATION, 0.0, {"distance_miles": None, "mandatory_missing": True})
    else:
        add("location", _W_LOCATION, location_score(dist_mi), {"distance_miles": round(dist_mi, 3)})

    # Accommodates (3.5, tolerance 2) — primary size metric.
    add(
        "accommodates", _W_ACCOMMODATES,
        _tolerance_raw(target.accommodates, cand.accommodates, 2.0),
        {"target_val": target.accommodates, "cand_val": cand.accommodates},
    )

    # Bedrooms (3.0) — step function.
    add(
        "bedrooms", _W_BEDROOMS,
        _bedrooms_raw(target.bedrooms, cand.bedrooms),
        {"target_val": target.bedrooms, "cand_val": cand.bedrooms},
    )

    # Property type (3.0) — similarity matrix.
    pt = property_type_similarity(target.property_type, cand.property_type)
    add(
        "property_type", _W_PROPERTY_TYPE,
        _PARTIAL_MISSING if pt is None else pt,
        {"target_val": target.property_type, "cand_val": cand.property_type},
    )

    # Amenities (3.0) — premium-weighted Jaccard overlap.
    t_set = _normalize_amenity_set(list(target.amenities or []))
    c_set = _normalize_amenity_set(list(cand.amenities or []))
    am_raw = _weighted_amenity_overlap(t_set, c_set) if (t_set and c_set) else _PARTIAL_MISSING
    add(
        "amenities", _W_AMENITIES, am_raw,
        {"overlap_count": len(t_set & c_set), "union_count": len(t_set | c_set)},
    )

    # Baths (2.0, tolerance 1.5).
    add(
        "baths", _W_BATHS,
        _tolerance_raw(target.baths, cand.baths, 1.5),
        {"target_val": target.baths, "cand_val": cand.baths},
    )

    # Trust / quality (2.0) — combined rating + review volume.
    tq = quality_score(target.rating, target.reviews)
    cq = quality_score(cand.rating, cand.reviews)
    q_raw = _PARTIAL_MISSING if (tq is None or cq is None) else max(0.0, 1.0 - abs(tq - cq))
    add("quality", _W_QUALITY, q_raw, {"target_quality": tq, "cand_quality": cq})

    # Beds (1.0, tolerance 3) — minor layout tie-breaker.
    add(
        "beds", _W_BEDS,
        _tolerance_raw(target.beds, cand.beds, 3.0),
        {"target_val": target.beds, "cand_val": cand.beds},
    )

    final_score = 0.0 if weight_sum <= 0 else score / weight_sum
    breakdown["total_score"] = final_score
    breakdown["weight_sum"] = weight_sum

    return final_score, breakdown


def similarity_score(target: ListingSpec, cand: ListingSpec, debug: bool = False) -> float | Tuple[float, Dict[str, Any]]:
    """
    Compute a 0-1 similarity score between target and candidate listings.

    The score is a weighted average of per-feature sub-scores (each in [0, 1]),
    normalized by the total weight. Candidates are assumed pre-filtered to an
    8-mile radius, so Location ranks proximity rather than gating cities.

    Weights:
      - location:      6.0  (lat/lng Haversine, steep 0-8 mile decay; mandatory)
      - accommodates:  3.5  (tolerance 2)
      - bedrooms:      3.0  (0 -> 1.0, 1 -> 0.5, >1 -> 0.0)
      - property_type: 3.0  (similarity matrix; entire-vs-room -> 0.0)
      - amenities:     3.0  (premium-weighted Jaccard overlap)
      - baths:         2.0  (tolerance 1.5)
      - quality:       2.0  (combined rating + review volume, Bayesian)
      - beds:          1.0  (tolerance 3)

    Missing features contribute a partial 0.5, except Location, which is
    mandatory and contributes 0.0 when coordinates are unavailable.
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
