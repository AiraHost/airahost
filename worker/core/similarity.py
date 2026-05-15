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


def similarity_score(target: ListingSpec, cand: ListingSpec) -> float:
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
      - address(city): 3.5  (city match = 1.0, else 0.0)
      - amenities:     1.5  (Jaccard overlap; auxiliary role)

    Property-type mismatch scores 0.0 (not 0.15) because the hard gate in
    filter_similar_candidates already blocks clear type conflicts; this
    ensures the score accurately reflects structural similarity.
    """
    score = 0.0
    weight_sum = 0.0

    def norm_city(spec: ListingSpec) -> str:
        city = (spec.city or "").strip().lower()
        if city:
            return city
        location = (spec.location or "").strip()
        if not location:
            return ""
        return location.split(",")[0].strip().lower()

    def add_num(t, c, w: float, tol: float):
        nonlocal score, weight_sum
        weight_sum += w
        if t is None or c is None:
            score += 0.35 * w
            return
        diff = abs(float(t) - float(c))
        s = max(0.0, 1.0 - diff / tol)
        score += s * w

    def add_reviews(t, c, w: float):
        """
        Review-count similarity on a log scale so 10 vs 30 is meaningful, while
        300 vs 600 is not treated as a massive mismatch.
        """
        nonlocal score, weight_sum
        weight_sum += w
        if t is None or c is None:
            score += 0.35 * w
            return
        try:
            t_log = math.log1p(max(0.0, float(t)))
            c_log = math.log1p(max(0.0, float(c)))
        except Exception:
            score += 0.35 * w
            return
        hi = max(t_log, c_log)
        lo = min(t_log, c_log)
        s = 1.0 if hi <= 0 else (lo / hi)
        score += max(0.0, min(1.0, s)) * w

    add_num(target.beds, cand.beds, w=2.5, tol=3.0)
    add_num(target.accommodates, cand.accommodates, w=2.5, tol=3.0)
    add_num(target.bedrooms, cand.bedrooms, w=2.5, tol=2.0)
    add_num(target.baths, cand.baths, w=2.0, tol=1.5)
    add_num(target.rating, cand.rating, w=2.0, tol=1.0)
    add_reviews(target.reviews, cand.reviews, w=2.0)

    # Property-type: strongest categorical signal.
    # Both known → exact match scores 1.0, mismatch scores 0.0.
    # Either unknown → partial credit (0.35) since we can't penalise what we can't read.
    weight_sum += 3.0
    if target.property_type and cand.property_type:
        score += (1.0 if target.property_type == cand.property_type else 0.0) * 3.0
    else:
        score += 0.35 * 3.0

    # Amenity overlap: auxiliary signal (weight 1.5).
    # If either side has no amenities, give partial credit rather than zero.
    weight_sum += 1.5
    t_set = set(target.amenities or [])
    c_set = set(cand.amenities or [])
    if t_set and c_set:
        overlap = len(t_set & c_set) / max(1, len(t_set | c_set))
        score += overlap * 1.5
    else:
        score += 0.35 * 1.5

    # Address/city: strict binary signal requested.
    # City match gets full credit; all non-matches (including unknown) get zero.
    weight_sum += 3.5
    t_city = norm_city(target)
    c_city = norm_city(cand)
    if t_city and c_city and t_city == c_city:
        score += 3.5

    if weight_sum <= 0:
        return 0.0
    return score / weight_sum


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
