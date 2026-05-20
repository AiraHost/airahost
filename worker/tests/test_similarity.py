from __future__ import annotations

from typing import List, Optional

import pytest

from worker.core.similarity import similarity_score
from worker.scraper.target_extractor import ListingSpec


def _spec(
    *,
    city: str = "",
    location: str = "Belmont, California",
    amenities: Optional[List[str]] = None,
) -> ListingSpec:
    return ListingSpec(
        url="https://www.airbnb.com/rooms/1",
        city=city,
        location=location,
        beds=2,
        accommodates=4,
        bedrooms=2,
        baths=1.0,
        rating=4.9,
        reviews=120,
        property_type="entire_home",
        amenities=list(amenities or ["wifi", "kitchen"]),
    )


def test_similarity_city_match_gets_full_address_weight_from_location_fallback():
    target = _spec(city="", location="Belmont, California")
    cand = _spec(city="", location="belmont, CA")
    assert similarity_score(target, cand) == pytest.approx(1.0)


def test_similarity_city_mismatch_gets_zero_address_weight():
    target = _spec(city="", location="Belmont, California")
    cand = _spec(city="", location="San Mateo, CA")

    # All non-city features match perfectly.
    # Address(city) contributes 0.0 on mismatch with weight 3.5.
    expected = 18.0 / 21.5
    assert similarity_score(target, cand) == pytest.approx(expected)


def test_similarity_amenities_normalize_aliases_for_matching():
    target = _spec(
        amenities=[
            "Wi-Fi",
            "Air conditioning",
            "Free parking on premises",
            "Allows pets",
        ]
    )
    cand = _spec(
        amenities=[
            "wifi",
            "ac",
            "free_parking",
            "pets_allowed",
        ]
    )
    assert similarity_score(target, cand) == pytest.approx(1.0)


def test_similarity_amenity_weights_penalize_missing_beach_access_more_than_wifi():
    target = _spec(amenities=["wifi", "kitchen", "beach_access"])
    missing_beach_access = _spec(amenities=["wifi", "kitchen"])
    missing_wifi = _spec(amenities=["kitchen", "beach_access"])

    score_missing_beach = similarity_score(target, missing_beach_access)
    score_missing_wifi = similarity_score(target, missing_wifi)

    assert score_missing_beach < score_missing_wifi
    assert (score_missing_wifi - score_missing_beach) > 0.03
