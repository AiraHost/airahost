import base64
from datetime import date

import worker.scraper.comp_collection as comp_collection
from worker.scraper.comp_collection import collect_search_comps


def _gid(listing_id: str) -> str:
    raw = f"DemandStayListing:{listing_id}".encode("utf-8")
    return base64.b64encode(raw).decode("utf-8")


def _payload(listing_id: str, price_text: str, accessibility_label: str | None = None):
    primary_line = {
        "price": price_text,
        "qualifier": "total",
    }
    if accessibility_label:
        primary_line["accessibilityLabel"] = accessibility_label
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
                                    "primaryLine": primary_line,
                                },
                            }
                        ]
                    }
                }
            }
        }
    }


def _payload_many(listing_ids):
    return {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "demandStayListing": {"id": _gid(str(listing_id))},
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "price": "$200 CAD",
                                        "qualifier": "total",
                                    }
                                },
                            }
                            for listing_id in listing_ids
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


class _FakeClientSparseFirstPage:
    def __init__(self):
        self.calls = []

    def search_listings_with_overrides(self, overrides):
        self.calls.append(dict(overrides))
        offset = int(overrides.get("itemsOffset") or 0)
        if offset == 0:
            return 200, _payload_many(["100"])
        if offset == 20:
            return 200, _payload_many(str(i) for i in range(200, 219))
        return 200, {"data": {"presentation": {"staysSearch": {"results": {"searchResults": []}}}}}


class _FakeClientRepeatsSamePage:
    """Sparse market: Airbnb re-serves the identical page for every offset."""

    def __init__(self):
        self.calls = []

    def search_listings_with_overrides(self, overrides):
        self.calls.append(dict(overrides))
        return 200, _payload_many(["111", "222"])


class _FakeClientCapacity:
    def __init__(self):
        self.calls = []

    def search_listings_with_overrides(self, overrides):
        self.calls.append(dict(overrides))
        return 200, {
            "data": {
                "presentation": {
                    "staysSearch": {
                        "results": {
                            "searchResults": [
                                {
                                    "listingId": "111",
                                    "personCapacity": 4,
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {"price": "$200 CAD", "qualifier": "total"}
                                    },
                                },
                                {
                                    "listingId": "222",
                                    "personCapacity": 5,
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {"price": "$210 CAD", "qualifier": "total"}
                                    },
                                },
                                {
                                    "listingId": "333",
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {"price": "$220 CAD", "qualifier": "total"}
                                    },
                                },
                            ]
                        }
                    }
                }
            }
        }


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


class _FakeClientOneNightEmptyTwoNightDecimalTotal:
    def __init__(self):
        self.calls = []

    def search_listings_with_overrides(self, overrides):
        self.calls.append(dict(overrides))
        checkin = str(overrides.get("checkin"))
        checkout = str(overrides.get("checkout"))
        if checkin == "2026-05-06" and checkout == "2026-05-07":
            return 200, {"data": {"presentation": {"staysSearch": {"results": {"searchResults": []}}}}}
        if checkin == "2026-05-06" and checkout == "2026-05-08":
            return 200, _payload(
                "555",
                "$1110.36 USD",
                "$1110.36 USD for 2 nights",
            )
        return 200, {"data": {"presentation": {"staysSearch": {"results": {"searchResults": []}}}}}


class _FakeClientWithPdp(_FakeClient):
    def __init__(self):
        super().__init__()
        self.detail_calls = []

    def get_listing_details(self, listing_id: str, checkin=None, checkout=None, adults=None):
        self.detail_calls.append(str(listing_id))
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
                                                    "overviewItems": [
                                                        {"title": "4 guests"},
                                                        {"title": "1 bath"},
                                                    ]
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


class _FakeClientWithForbiddenPdp(_FakeClientWithPdp):
    def get_listing_details(self, listing_id: str, checkin=None, checkout=None, adults=None):
        raise AssertionError("PDP should not be fetched when structural cache has the listing")


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


def test_collect_search_comps_default_keeps_paging_until_priced_target():
    """
    A sparse first page must not make daily comp collection return one listing
    when the next offset has enough priced inventory for the 20-card compset.
    """
    client = _FakeClientSparseFirstPage()
    comps, qn = collect_search_comps(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
    )

    offsets = [call.get("itemsOffset", 0) for call in client.calls]
    assert qn == 1
    assert offsets == [0, 20]
    assert offsets.count(0) == 1
    assert len(comps) == 20


def test_collect_search_comps_min_priced_target_stops_early_when_met():
    """
    With min_priced_target set, paging stops as soon as that many priced comps
    are found, instead of crawling deeper offsets toward the full page size.
    """
    client = _FakeClientSparseFirstPage()
    comps, qn = collect_search_comps(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        min_priced_target=1,
    )

    offsets = [call.get("itemsOffset", 0) for call in client.calls]
    assert qn == 1
    assert offsets == [0]  # one priced comp on page 0 meets the target; no deeper paging
    assert len(comps) == 1


def test_collect_search_comps_stops_deep_sweep_when_page_adds_no_new_listings():
    """
    When the market is exhausted, Airbnb re-serves the same page for every
    deeper offset. Each extra offset costs a full search request (a multi-second
    browser navigation when direct HTTP is unavailable), so crawling all
    max_scroll_rounds offsets multiplies report generation time for zero new
    comps. The sweep must stop at the first page that adds no new listings.
    """
    client = _FakeClientRepeatsSamePage()
    comps, qn = collect_search_comps(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=12,
        max_cards=20,
        rate_limit_seconds=0.0,
    )

    offsets = [call.get("itemsOffset", 0) for call in client.calls]
    assert qn == 1
    # Base page (offset 0), then exactly one deeper page (offset 20) that
    # repeats the same listings — the remaining 11 offsets must be skipped.
    assert offsets == [0, 20]
    assert len(comps) == 2


def test_collect_search_comps_two_night_keeps_paging_until_priced_target():
    client = _FakeClientSparseFirstPage()
    comps, qn = collect_search_comps(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        prefer_two_night=True,
    )

    offsets = [call.get("itemsOffset", 0) for call in client.calls]
    assert qn == 2
    assert offsets == [0, 20]
    assert offsets.count(0) == 1
    assert len(comps) == 20


def test_collect_search_comps_requires_exact_guest_capacity_before_scoring():
    client = _FakeClientCapacity()
    comps, _qn = collect_search_comps(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=4,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        page_offsets=[0],
        target_accommodates=4,
    )

    assert [c.url for c in comps] == ["https://www.airbnb.com/rooms/111"]
    assert comps[0].accommodates == 4
    assert client.calls[0]["adults"] == 4
    assert client.calls[0]["guests"] == 4


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
    assert comps[0].accommodates == 4
    assert comps[0].baths == 1.0
    assert comps[0].property_type == "entire_home"


def test_collect_search_comps_reuses_persistent_pdp_structural_cache(tmp_path, monkeypatch):
    """
    Once a listing's exact capacity has been verified from PDP, later runs
    should use the cache instead of fetching that PDP again.
    """
    monkeypatch.setenv(
        "AIRBNB_PDP_STRUCTURAL_CACHE_PATH",
        str(tmp_path / "pdp_structural_cache.json"),
    )
    comp_collection._PDP_STRUCTURAL_CACHE.clear()
    comp_collection._PDP_STRUCTURAL_CACHE_LOADED = False

    first_client = _FakeClientWithPdp()
    comps, _qn = collect_search_comps(
        client=first_client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        page_offsets=[0],
        target_accommodates=4,
        pdp_structural_enrichment=True,
    )
    assert len(comps) == 1
    assert first_client.detail_calls == ["111"]

    comp_collection._PDP_STRUCTURAL_CACHE.clear()
    comp_collection._PDP_STRUCTURAL_CACHE_LOADED = False
    second_client = _FakeClientWithForbiddenPdp()
    cached_comps, _qn = collect_search_comps(
        client=second_client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        page_offsets=[0],
        target_accommodates=4,
        pdp_structural_enrichment=True,
    )

    assert len(cached_comps) == 1
    assert cached_comps[0].accommodates == 4


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
    # Optimized PDP enrichment: search-derived values are preserved if PDP cannot provide them.
    # This avoids unnecessary re-fetches while maintaining data integrity.
    assert comps[0].baths == 1.0
    assert comps[0].property_type == "entire_home"


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


def test_collect_search_comps_span_nights_uses_n_night_window():
    """Pool discovery passes span_nights to search a single multi-night window
    (check_out = check_in + span_nights), distinct from the 1/2-night pricing."""
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
        span_nights=5,
    )
    assert qn == 5
    call = client.calls[0]
    assert call["checkin"] == "2026-05-06"
    assert call["checkout"] == "2026-05-11"  # check_in + 5 nights


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


def test_collect_search_comps_ceils_decimal_two_night_total_before_deriving_nightly():
    """
    Airbnb API totals can carry cent-level artifacts even when the card displays
    whole-dollar prices. Normalize the 2-night total first, then derive nightly.
    """
    client = _FakeClientOneNightEmptyTwoNightDecimalTotal()
    comps, qn = collect_search_comps(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.com",
        date_i=date(2026, 5, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        page_offsets=[0],
    )

    assert qn == 2
    assert len(comps) == 1
    assert comps[0].nightly_price == 555.5
    assert comps[0].query_total_price == 1111.0
    assert comps[0].scrape_nights == 2
    assert comps[0].price_kind == "trip_total_from_search"


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
