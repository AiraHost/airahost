from __future__ import annotations

import pytest

from worker.core.similarity import similarity_score
from worker.scraper.target_extractor import ListingSpec


def _spec(*, city: str = "", location: str = "Belmont, California") -> ListingSpec:
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
        amenities=["wifi", "kitchen"],
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
