from worker.scraper.target_extractor import extract_target_spec


def _make_sample_pdp_payload():
    return {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "metadata": {
                            "sharingConfig": {
                                "location": "Belmont, CA, United States",
                                "propertyType": "Entire home",
                                "personCapacity": 10,
                            }
                        },
                        "sbuiData": {
                            "sectionConfiguration": {
                                "root": {
                                    "sections": [
                                        {
                                            "sectionId": "OVERVIEW_DEFAULT_V2",
                                            "sectionData": {
                                                "brandAccessibilityLabel": "Guest Favourite Listing.",
                                                "title": "Entire home in Belmont, CA, United States",
                                                "overviewItems": [
                                                    {"title": "10 guests"},
                                                    {"title": "5 bedrooms"},
                                                    {"title": "5 beds"},
                                                    {"title": "3 baths"},
                                                ],
                                            },
                                        }
                                    ]
                                }
                            }
                        },
                        "previewAmenitiesGroups": [
                            {
                                "title": "What this place offers",
                                "amenities": [
                                    {"title": "Air conditioning", "available": True},
                                    {"title": "Kitchen", "available": True},
                                    {"title": "Washer", "available": True},
                                    {"title": "Dryer", "available": True},
                                    {"title": "Pool", "available": True},
                                    {"title": "Hot tub", "available": True},
                                    {"title": "Free parking on premises", "available": True},
                                    {"title": "EV charger", "available": True},
                                    {"title": "Allows pets", "available": True},
                                    {"title": "Waterfront", "available": True},
                                    {"title": "Guest favorite", "available": True},
                                ],
                            }
                        ],
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": "$985 CAD",
                                            "qualifier": "total",
                                        }
                                    },
                                },
                            }
                        ],
                    }
                }
            }
        }
    }


class _FakeClientWithPdpOnly:
    def __init__(self, payload):
        self._payload = payload
        self.browser_bridge_used = False

    def get_listing_details(self, listing_id: str, checkin=None, checkout=None, adults=None):
        assert listing_id == "1305899249107196055"
        return self._payload

    def _get_playwright_scraper(self):
        self.browser_bridge_used = True
        raise AssertionError("browser bridge should not be used when PDP payload is sufficient")


def test_extract_target_spec_prefers_pdp_payload_and_keeps_amenities():
    client = _FakeClientWithPdpOnly(_make_sample_pdp_payload())
    spec, warnings = extract_target_spec(
        client,
        "https://www.airbnb.com/rooms/1305899249107196055",
    )

    assert client.browser_bridge_used is False
    assert warnings == []
    assert spec.property_type == "entire_home"
    assert spec.accommodates == 10
    assert spec.bedrooms == 5
    assert spec.beds == 5
    assert spec.baths == 3
    assert spec.location == "Belmont, CA, United States"
    assert spec.nightly_price == 985.0
    assert "Air conditioning" in spec.amenities
    assert "Kitchen" in spec.amenities
    assert "Allows pets" in spec.amenities
    assert "Waterfront" in spec.amenities
    assert "Guest favorite" in spec.amenities



def test_extract_target_spec_marks_guest_favorite_from_brand_accessibility_label():
    payload = _make_sample_pdp_payload()
    amenities = payload["data"]["presentation"]["stayProductDetailPage"]["sections"]["previewAmenitiesGroups"][0]["amenities"]
    payload["data"]["presentation"]["stayProductDetailPage"]["sections"]["previewAmenitiesGroups"][0]["amenities"] = [
        item for item in amenities if item.get("title") != "Guest favorite"
    ]

    client = _FakeClientWithPdpOnly(payload)
    spec, warnings = extract_target_spec(
        client,
        "https://www.airbnb.com/rooms/1305899249107196055",
    )

    assert warnings == []
    assert "Guest favorite" in spec.amenities
