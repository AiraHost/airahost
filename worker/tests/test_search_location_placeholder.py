"""
Regression tests for the comp-search location anchoring fix.

A listing's placeholder title (e.g. "Airbnb Listing #1596737613274892756 ·
Entire home") must never become the search `query` — comp searches must anchor on
a real location (city/state) or coordinates instead.
"""

import unittest

from worker.scraper.day_query import _derive_canonical_search_location
from worker.scraper.price_estimator import _resolve_target_search_location
from worker.scraper.target_extractor import (
    ListingSpec,
    location_looks_like_placeholder,
    map_pdp_to_listing_spec,
)

_PLACEHOLDER = "Airbnb Listing #1596737613274892756 · Entire home"


class LocationPlaceholderDetectionTest(unittest.TestCase):
    def test_placeholder_titles_detected(self):
        for value in (
            _PLACEHOLDER,
            "Airbnb Listing #123",
            "Entire home",
            "Private room",
            "Shared room",
            "",
            None,
        ):
            self.assertTrue(
                location_looks_like_placeholder(value),
                msg=f"expected placeholder for {value!r}",
            )

    def test_real_locations_not_flagged(self):
        for value in (
            "Redwood City, California",
            "Belmont, CA",
            "Taipei, Taiwan",
            "37.48556,-122.23636",
        ):
            self.assertFalse(
                location_looks_like_placeholder(value),
                msg=f"unexpected placeholder for {value!r}",
            )


class ResolverGuardTest(unittest.TestCase):
    def test_day_query_resolver_drops_placeholder(self):
        target = ListingSpec(url="https://www.airbnb.com/rooms/1", location=_PLACEHOLDER)
        self.assertEqual(_derive_canonical_search_location(target), "")

    def test_fixed_pool_resolver_drops_placeholder(self):
        target = ListingSpec(url="https://www.airbnb.com/rooms/1", location=_PLACEHOLDER)
        self.assertEqual(_resolve_target_search_location(target), "")

    def test_placeholder_city_not_used(self):
        # When city/state got contaminated by a placeholder title, resolvers must
        # not emit it as the query.
        target = ListingSpec(
            url="https://www.airbnb.com/rooms/1",
            city=_PLACEHOLDER,
            location=_PLACEHOLDER,
        )
        self.assertEqual(_derive_canonical_search_location(target), "")
        self.assertEqual(_resolve_target_search_location(target), "")

    def test_real_city_state_preferred(self):
        target = ListingSpec(
            url="https://www.airbnb.com/rooms/1",
            city="Redwood City",
            state="California",
            location=_PLACEHOLDER,
        )
        self.assertEqual(
            _derive_canonical_search_location(target), "Redwood City, California"
        )
        self.assertEqual(
            _resolve_target_search_location(target), "Redwood City, California"
        )

    def test_coords_preferred_over_everything(self):
        target = ListingSpec(
            url="https://www.airbnb.com/rooms/1",
            lat=37.48556,
            lng=-122.23636,
            location=_PLACEHOLDER,
        )
        self.assertEqual(
            _derive_canonical_search_location(target), "37.48556,-122.23636"
        )
        self.assertEqual(
            _resolve_target_search_location(target), "37.48556,-122.23636"
        )


class MapPdpSpecGuardTest(unittest.TestCase):
    def test_placeholder_location_not_propagated(self):
        spec = map_pdp_to_listing_spec(
            {"location": _PLACEHOLDER, "title": _PLACEHOLDER},
            "https://www.airbnb.com/rooms/1",
        )
        self.assertEqual(spec.city, "")
        self.assertEqual(spec.state, "")
        self.assertFalse(location_looks_like_placeholder(spec.location) and spec.location)
        # Resolver on the resulting spec must not emit the title.
        self.assertEqual(_resolve_target_search_location(spec), "")

    def test_real_location_still_parsed(self):
        spec = map_pdp_to_listing_spec(
            {"location": "Redwood City, California, United States"},
            "https://www.airbnb.com/rooms/1",
        )
        self.assertEqual(spec.city, "Redwood City")
        self.assertEqual(spec.state, "California")


if __name__ == "__main__":
    unittest.main()
