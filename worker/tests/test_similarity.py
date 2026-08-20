from __future__ import annotations

from typing import List, Optional

import pytest

from worker.core.similarity import (
    haversine_miles,
    location_score,
    property_type_similarity,
    quality_score,
    similarity_score,
)
from worker.scraper.target_extractor import ListingSpec

# Total of all component weights — used to express expected scores as the
# fraction of weight retained after one feature is degraded.
_TOTAL_WEIGHT = 20.5


def _spec(
    *,
    lat: Optional[float] = 37.52,
    lng: Optional[float] = -122.27,
    accommodates: Optional[int] = 4,
    bedrooms: Optional[int] = 2,
    beds: Optional[int] = 2,
    baths: Optional[float] = 1.0,
    rating: Optional[float] = 4.9,
    reviews: Optional[int] = 120,
    property_type: str = "entire_home",
    amenities: Optional[List[str]] = None,
) -> ListingSpec:
    return ListingSpec(
        url="https://www.airbnb.com/rooms/1",
        location="Belmont, California",
        lat=lat,
        lng=lng,
        beds=beds,
        accommodates=accommodates,
        bedrooms=bedrooms,
        baths=baths,
        rating=rating,
        reviews=reviews,
        property_type=property_type,
        amenities=list(amenities or ["wifi", "kitchen"]),
    )


def test_identical_listings_score_one():
    assert similarity_score(_spec(), _spec()) == pytest.approx(1.0)


# ── Location (lat/lng Haversine, weight 6.0) ──────────────────────────────────
def test_location_decay_curve():
    assert location_score(0.0) == pytest.approx(1.0)
    assert location_score(1.0) > 0.94          # within a mile ≈ 1.0
    assert 0.45 < location_score(5.0) < 0.55   # mid-range drops off
    assert location_score(8.0) == pytest.approx(0.0)
    assert location_score(20.0) == 0.0         # clamped, never negative


def test_haversine_miles_is_reasonable():
    # ~0.44 mi between two points 0.005° apart near 37.5N.
    d = haversine_miles(37.52, -122.27, 37.525, -122.275)
    assert 0.3 < d < 0.6


def test_closer_candidate_scores_higher_than_far_one():
    target = _spec(lat=37.52, lng=-122.27)
    near = _spec(lat=37.525, lng=-122.275)  # ~0.4 mi
    far = _spec(lat=37.45, lng=-122.20)     # ~6 mi (still inside 8-mi pre-filter)
    assert similarity_score(target, near) > similarity_score(target, far)


def test_location_is_mandatory_missing_coords_not_partial():
    # Missing coordinates score 0.0 for Location (NOT the 0.5 partial), so an
    # otherwise-identical comp forfeits the full 6.0 location weight.
    score = similarity_score(_spec(), _spec(lat=None, lng=None))
    assert score == pytest.approx((_TOTAL_WEIGHT - 6.0) / _TOTAL_WEIGHT)


def test_location_falls_back_to_precomputed_distance_km():
    target = _spec()
    cand = _spec(lat=None, lng=None)
    cand.distance_to_target_km = 0.0  # set by the 8-mile geo filter
    # distance 0 → location 1.0 → perfect score again.
    assert similarity_score(target, cand) == pytest.approx(1.0)


# ── Bedrooms (weight 3.0, step 0 → 1.0, 1 → 0.5, >1 → 0.0) ─────────────────────
def test_bedrooms_step_logic():
    target = _spec(bedrooms=2)
    assert similarity_score(target, _spec(bedrooms=2)) == pytest.approx(1.0)
    assert similarity_score(target, _spec(bedrooms=3)) == pytest.approx(
        (_TOTAL_WEIGHT - 1.5) / _TOTAL_WEIGHT  # diff 1 → 0.5 → loses 1.5 of 3.0
    )
    assert similarity_score(target, _spec(bedrooms=5)) == pytest.approx(
        (_TOTAL_WEIGHT - 3.0) / _TOTAL_WEIGHT  # diff >1 → 0.0 → loses all 3.0
    )


# ── Property-type similarity matrix (weight 3.0) ──────────────────────────────
def test_property_type_matrix():
    assert property_type_similarity("entire_home", "entire_home") == 1.0
    assert property_type_similarity("apartment", "condo") == pytest.approx(0.8)
    assert property_type_similarity("house", "villa") == pytest.approx(0.8)
    assert property_type_similarity("apartment", "house") == pytest.approx(0.5)
    assert property_type_similarity("private_room", "shared_room") == pytest.approx(0.5)
    assert property_type_similarity("entire_home", "private_room") == 0.0
    assert property_type_similarity("entire_home", "") is None  # unknown


# ── Trust / quality (weight 2.0, rating + review volume) ──────────────────────
def test_quality_blends_rating_and_review_volume():
    assert quality_score(4.9, 500) > quality_score(5.0, 2)  # volume matters
    assert quality_score(None, 100) is None                 # rating unknown


def test_two_strong_properties_match_on_quality():
    score = similarity_score(_spec(rating=4.95, reviews=300), _spec(rating=4.9, reviews=250))
    assert score > 0.99


# ── Missing data → 0.5 partial (everything except Location) ───────────────────
def test_missing_non_location_feature_gets_half_partial():
    # baths missing on the candidate → 0.5 raw → contributes 1.0 of its 2.0 weight.
    score = similarity_score(_spec(baths=2.0), _spec(baths=None))
    assert score == pytest.approx((_TOTAL_WEIGHT - 1.0) / _TOTAL_WEIGHT)


