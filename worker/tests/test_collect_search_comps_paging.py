import base64
from datetime import date

from worker.scraper.comp_collection import collect_search_comps


def _gid(listing_id: str) -> str:
    raw = f"DemandStayListing:{listing_id}".encode("utf-8")
    return base64.b64encode(raw).decode("utf-8")


def _payload(listing_id: str, price_text: str):
    return {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "demandStayListing": {"id": _gid(listing_id)},
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "price": price_text,
                                        "qualifier": "total",
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        }
    }


class _FakeClient:
    def __init__(self):
        self.calls = []

    def search_listings_with_overrides(self, overrides):
        self.calls.append(dict(overrides))
        offset = int(overrides.get("itemsOffset") or 0)
        if offset == 0:
            return 200, _payload("111", "$200 CAD")
        if offset == 20:
            return 200, _payload("222", "$300 CAD")
        return 200, {"data": {"presentation": {"staysSearch": {"results": {"searchResults": []}}}}}


class _FakeClientOneNightEmptyTwoNightHasPrice:
    def __init__(self):
        self.calls = []

    def search_listings_with_overrides(self, overrides):
        self.calls.append(dict(overrides))
        checkin = str(overrides.get("checkin"))
        checkout = str(overrides.get("checkout"))
        if checkin == "2026-05-06" and checkout == "2026-05-07":
            return 200, {"data": {"presentation": {"staysSearch": {"results": {"searchResults": []}}}}}
        if checkin == "2026-05-06" and checkout == "2026-05-08":
            return 200, _payload("444", "$400 CAD")
        return 200, {"data": {"presentation": {"staysSearch": {"results": {"searchResults": []}}}}}


class _FakeClientWithPdp(_FakeClient):
    def get_listing_details(self, listing_id: str, checkin=None, checkout=None, adults=None):
        return {
            "data": {
                "presentation": {
                    "stayProductDetailPage": {
                        "sections": {
                            "metadata": {
                                "sharingConfig": {
                                    "propertyType": "Entire guest suite",
                                }
                            },
                            "sbuiData": {
                                "sectionConfiguration": {
                                    "root": {
                                        "sections": [
                                            {
                                                "sectionId": "OVERVIEW_DEFAULT_V2",
                                                "sectionData": {
                                                    "overviewItems": [{"title": "1 bath"}]
                                                },
                                            }
                                        ]
                                    }
                                }
                            },
                        }
                    }
                }
            }
        }


class _FakeClientWithEmptyPdp(_FakeClient):
    def get_listing_details(self, listing_id: str, checkin=None, checkout=None, adults=None):
        # No metadata.sharingConfig.propertyType and no OVERVIEW_DEFAULT_V2 bath item.
        return {"data": {"presentation": {"stayProductDetailPage": {"sections": {}}}}}


def _payload_with_search_structurals(listing_id: str, price_text: str):
    return {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "__typename": "SkinnyListingItem",
                                "listingId": listing_id,
                                "title": "Home in Testville",
                                "subtitle": "1 bedroom · 2 beds · 1 bath",
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "price": price_text,
                                        "qualifier": "total",
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        }
    }


class _FakeClientSearchHasBathTypePdpMissing(_FakeClientWithEmptyPdp):
    def search_listings_with_overrides(self, overrides):
        self.calls.append(dict(overrides))
        return 200, _payload_with_search_structurals("333", "$250 CAD")


def test_collect_search_comps_merges_offsets():
    client = _FakeClient()
    comps, qn = collect_search_comps(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        page_offsets=[0, 20],
    )

    urls = sorted([c.url for c in comps])
    assert qn == 1
    assert "https://www.airbnb.com/rooms/111" in urls
    assert "https://www.airbnb.com/rooms/222" in urls


def test_collect_search_comps_can_enrich_baths_and_property_type_from_pdp():
    client = _FakeClientWithPdp()
    comps, _qn = collect_search_comps(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        page_offsets=[0],
        pdp_structural_enrichment=True,
    )

    assert len(comps) == 1
    assert comps[0].baths == 1.0
    assert comps[0].property_type == "entire_home"


def test_collect_search_comps_strict_pdp_only_clears_search_baths_and_property_type_when_missing():
    client = _FakeClientSearchHasBathTypePdpMissing()
    comps, _qn = collect_search_comps(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        page_offsets=[0],
        pdp_structural_enrichment=True,
    )

    assert len(comps) == 1
    # Strict PDP-only mode: search-derived values are discarded.
    assert comps[0].baths is None
    assert comps[0].property_type == ""


def test_collect_search_comps_prefer_two_night_uses_two_night_window():
    client = _FakeClient()
    _comps, qn = collect_search_comps(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        page_offsets=[0],
        prefer_two_night=True,
    )
    assert qn == 2


def test_collect_search_comps_default_retries_two_night_when_one_night_empty():
    client = _FakeClientOneNightEmptyTwoNightHasPrice()
    comps, qn = collect_search_comps(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        page_offsets=[0],
    )
    assert qn == 2
    assert len(comps) == 1
    assert comps[0].url == "https://www.airbnb.com/rooms/444"


def test_collect_search_comps_enables_map_search_with_bounds_when_center_and_radius_provided():
    client = _FakeClient()
    _comps, _qn = collect_search_comps(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        page_offsets=[0],
        center_lat=43.6532,
        center_lng=-79.3832,
        map_radius_km=5.0,
    )

    assert client.calls, "expected at least one search call"
    first = client.calls[0]
    assert first.get("searchByMap") is True
    for k in ("neLat", "neLng", "swLat", "swLng"):
        assert k in first
