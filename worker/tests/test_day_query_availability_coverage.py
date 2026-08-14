"""Required test 7: the report-level aggregation/coverage gate.

`estimate_base_price_for_date()` is what `worker/main.py` calls per night; its
`median_price` feeds the `valid_prices` coverage check that decides whether a
report reaches the "couldn't collect enough trustworthy nightly prices"
fallback (see `getFriendlyReportError()` in
`src/app/r/[shareId]/page.tsx` and the `_fail(...)` calls in
`worker/main.py` guarded by `if daily_results and valid_prices`).

This test exercises the real `collect_search_comps()` and the real similarity
engine (nothing about the availability contract is mocked) through a scripted
client returning the regression fixture — well-formed, structurally complete,
priced rows with no explicit `available` field and no negative signal — and
asserts the day produces a non-null `median_price`. Before the fix this
returns `None` because every priced-but-unknown-availability row is discarded
at the collector boundary before it ever reaches pricing.
"""

from __future__ import annotations

import base64
from datetime import date
from typing import Any, Dict, List

from worker.scraper import day_query
from worker.scraper.target_extractor import ListingSpec

# Shared coordinates for target and comps so the (mandatory) location
# component of similarity scores at its maximum — this test is about the
# availability contract, not geographic proximity.
_LAT, _LNG = 43.6532, -79.3832


def _gid(listing_id: str) -> str:
    return base64.b64encode(f"DemandStayListing:{listing_id}".encode("utf-8")).decode("utf-8")


def _priced_row_no_explicit_availability(listing_id: str, price: str) -> Dict[str, Any]:
    # Exactly the regression shape: a real, structurally complete, priced
    # card for the exact dates that never states `available` one way or the
    # other. Structural fields (bedroomCount/bedCount/bathroomCount/
    # roomTypeCategory/lat/lng) are populated so the row clears the real
    # similarity floor on its own merits, isolating the availability-contract
    # regression as the only thing under test.
    return {
        "demandStayListing": {"id": _gid(listing_id)},
        "title": f"Entire home {listing_id} in Toronto",
        "personCapacity": 4,
        "bedroomCount": 2,
        "bedCount": 2,
        "bathroomCount": 1.5,
        "roomTypeCategory": "Entire home",
        "lat": _LAT,
        "lng": _LNG,
        "structuredDisplayPrice": {
            "primaryLine": {"price": price, "qualifier": "night"},
        },
    }


def _wrap(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"data": {"presentation": {"staysSearch": {"results": {"searchResults": rows}}}}}


class _ScriptedClient:
    """Returns the same StaysSearch payload for every search call."""

    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.calls: List[Dict[str, Any]] = []

    def search_listings_with_overrides(self, overrides):
        self.calls.append(dict(overrides))
        return 200, self.payload


def test_day_coverage_gate_accepts_priced_rows_with_unknown_availability():
    target = ListingSpec(
        url="https://www.airbnb.com/rooms/47273102",
        title="Target",
        location="Toronto, ON",
        property_type="entire_home",
        accommodates=4,
        bedrooms=2,
        beds=2,
        baths=1.5,
        lat=_LAT,
        lng=_LNG,
    )
    payload = _wrap(
        [
            _priced_row_no_explicit_availability(str(200 + i), f"${180 + i} CAD")
            for i in range(5)
        ]
    )
    client = _ScriptedClient(payload)

    result = day_query.estimate_base_price_for_date(
        client,
        target,
        "https://www.airbnb.com",
        date(2026, 9, 6),
        adults=4,
        max_scroll_rounds=0,
        rate_limit_seconds=0.0,
    )

    assert result.median_price is not None, (
        "day coverage gate must accept priced rows with unstated availability "
        "and no negative signal; a None median_price is what triggers the "
        "'couldn't collect enough trustworthy nightly prices' report fallback"
    )
    assert result.comps_used > 0
    assert "missing_data" not in result.flags
