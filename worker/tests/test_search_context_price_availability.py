import json
from pathlib import Path

from worker.scraper.parsers import (
    _extract_availability_context_from_search_result,
    parse_pdp_baths_property_type_fast,
    parse_pdp_response,
    parse_search_listing_context,
    _parse_price_with_prefix,
    _parse_price_string,
    _parse_dollar_amount_currency,
)


def test_parse_price_string_prefers_currency_amount_over_other_numbers():
    text = "Rated 4.91 out of 5. CA$241 for 1 night"
    assert _parse_price_string(text) == 241.0


def test_parse_price_with_prefix_returns_raw_prefix_token():
    text = "XCUR241 for 2 nights"
    value, prefix = _parse_price_with_prefix(text)
    assert value == 241.0
    assert prefix == "XCUR"


def test_availability_does_not_flip_unavailable_from_generic_word_only():
    payload = {
        "title": "Nice condo",
        "someNestedText": "Unavailable amenity: hot tub",
    }
    out = _extract_availability_context_from_search_result(payload)
    assert out["is_available"] is True
    assert out["availability_reason"] is None


def test_availability_detects_strong_unavailable_markers():
    payload = {
        "banner": "Sold out for your dates",
    }
    out = _extract_availability_context_from_search_result(payload)
    assert out["is_available"] is False
    assert out["availability_reason"] == "sold_out"


def test_parse_search_context_uses_structured_primary_price_when_available():
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "demandStayListing": {
                                    # base64("DemandStayListing:1629301846268091180")
                                    "id": "RGVtYW5kU3RheUxpc3Rpbmc6MTYyOTMwMTg0NjI2ODA5MTE4MA=="
                                },
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "accessibilityLabel": "$173 CAD total",
                                        "price": "$173 CAD",
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

    ctx = parse_search_listing_context(payload)
    row = ctx["1629301846268091180"]
    assert row["is_available"] is True
    assert row["total_price"] == 173.0
    assert row["currency"] == "CAD"


def test_parse_search_context_supports_skinny_listing_item_listing_id():
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "__typename": "SkinnyListingItem",
                                "listingId": "958866016198543537",
                                "title": "Home in Half Moon Bay",
                                "subtitle": "1 bedroom · 1 private bath",
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "accessibilityLabel": "$173 CAD total",
                                        "price": "$173 CAD",
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

    ctx = parse_search_listing_context(payload)
    row = ctx["958866016198543537"]
    assert row["total_price"] == 173.0
    assert row["property_type"] == "entire_home"
    assert row["baths"] == 1.0


def test_parse_search_context_parses_currency_prefix_variant_ca_dollar():
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "demandStayListing": {
                                    "id": "RGVtYW5kU3RheUxpc3Rpbmc6MTY1MDk1NDQwMDUyNTM0MDQ0NA=="
                                },
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "accessibilityLabel": "CA$248 CAD total",
                                        "price": "CA$248 CAD",
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

    ctx = parse_search_listing_context(payload)
    row = ctx["1650954400525340444"]
    assert row["total_price"] == 248.0
    assert row["currency"] == "CAD"


def test_availability_does_not_mark_unavailable_from_popularity_booked_text():
    payload = {"subtitle": "Booked 6 times in the last month"}
    out = _extract_availability_context_from_search_result(payload)
    assert out["is_available"] is True
    assert out["availability_reason"] is None


def test_parse_dollar_amount_currency_accepts_embedded_dollar_token():
    amount, currency = _parse_dollar_amount_currency("CA$248 CAD total")
    assert amount == 248.0
    assert currency == "CAD"


def test_parse_search_context_does_not_apply_structured_primary_price_when_unavailable():
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "demandStayListing": {
                                    # base64("DemandStayListing:1629301846268091180")
                                    "id": "RGVtYW5kU3RheUxpc3Rpbmc6MTYyOTMwMTg0NjI2ODA5MTE4MA=="
                                },
                                "available": False,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "accessibilityLabel": "$173 CAD total",
                                        "price": "$173 CAD",
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

    ctx = parse_search_listing_context(payload)
    row = ctx["1629301846268091180"]
    assert row["is_available"] is False
    assert row["total_price"] is None
    assert row["currency"] is None


def test_parse_search_context_uses_discounted_primary_price_when_available():
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "demandStayListing": {
                                    "id": "RGVtYW5kU3RheUxpc3Rpbmc6MTY1NjY0MjU2NDc5MTI1Njc0NQ=="
                                },
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "__typename": "DiscountedDisplayPriceLine",
                                        "accessibilityLabel": "$316 CAD total, originally $402 CAD",
                                        "discountedPrice": "$316 CAD",
                                        "originalPrice": "$402 CAD",
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

    ctx = parse_search_listing_context(payload)
    row = ctx["1656642564791256745"]
    assert row["is_available"] is True
    assert row["total_price"] == 316.0
    assert row["currency"] == "CAD"


def test_parse_search_context_prefers_discounted_primary_price_when_both_fields_exist():
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "demandStayListing": {
                                    "id": "RGVtYW5kU3RheUxpc3Rpbmc6MTY1NjY0MjU2NDc5MTI1Njc0NQ=="
                                },
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "__typename": "DiscountedDisplayPriceLine",
                                        "price": "$402 CAD",
                                        "discountedPrice": "$316 CAD",
                                        "accessibilityLabel": "$316 CAD total, originally $402 CAD",
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

    ctx = parse_search_listing_context(payload)
    row = ctx["1656642564791256745"]
    assert row["total_price"] == 316.0
    assert row["currency"] == "CAD"


def test_parse_search_context_prefers_discount_accessibility_label_when_discounted_price_missing():
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "demandStayListing": {
                                    "id": "RGVtYW5kU3RheUxpc3Rpbmc6MTYyOTMwMTg0NjI2ODA5MTE4MA=="
                                },
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "price": "$5,648 USD",
                                        "accessibilityLabel": "$4,659 USD for 2 nights, originally $5,648 USD",
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

    ctx = parse_search_listing_context(payload)
    row = ctx["1629301846268091180"]
    assert row["nightly_price"] is None
    assert row["total_price"] == 4659.0
    assert row["price_nights"] == 2
    assert row["currency"] == "USD"


def test_parse_search_context_marks_two_night_total_for_later_normalization():
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "demandStayListing": {
                                    "id": "RGVtYW5kU3RheUxpc3Rpbmc6MTYyOTMwMTg0NjI2ODA5MTE4MA=="
                                },
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "accessibilityLabel": "$300 CAD for 2 nights",
                                        "price": "$300 CAD",
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

    ctx = parse_search_listing_context(payload)
    row = ctx["1629301846268091180"]
    assert row["nightly_price"] is None
    assert row["total_price"] == 300.0
    assert row["price_nights"] == 2


def test_parse_search_context_ceils_direct_nightly_api_decimal_to_display_price():
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "demandStayListing": {
                                    "id": "RGVtYW5kU3RheUxpc3Rpbmc6MTYyOTMwMTg0NjI2ODA5MTE4MA=="
                                },
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "accessibilityLabel": "$40.19 USD per night",
                                        "price": "$40.19 USD",
                                        "qualifier": "night",
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    ctx = parse_search_listing_context(payload)
    row = ctx["1629301846268091180"]
    assert row["nightly_price"] == 41
    assert row["total_price"] == 41
    assert row["price_nights"] == 1


def test_parse_search_context_prefers_primary_line_price_over_accessibility_total():
    """
    primaryLine.price is the displayed per-night value and the most accurate one.
    accessibilityLabel folds small per-night fees into the figure ("$432 for 1
    night"), shifting the parsed nightly by a few dollars. The parser must use
    primaryLine.price (430), not the accessibilityLabel amount (432).
    """
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "demandStayListing": {
                                    "id": "RGVtYW5kU3RheUxpc3Rpbmc6MTYyOTMwMTg0NjI2ODA5MTE4MA=="
                                },
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "price": "$430 USD",
                                        "accessibilityLabel": "$432 USD for 1 night",
                                        "qualifier": "night",
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    ctx = parse_search_listing_context(payload)
    row = ctx["1629301846268091180"]
    assert row["nightly_price"] == 430
    assert row["price_nights"] == 1


def test_parse_search_context_ceils_decimal_two_night_total_to_display_price():
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "demandStayListing": {
                                    "id": "RGVtYW5kU3RheUxpc3Rpbmc6MTYyOTMwMTg0NjI2ODA5MTE4MA=="
                                },
                                "available": True,
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "accessibilityLabel": "$1110.36 USD for 2 nights",
                                        "price": "$1110.36 USD",
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

    ctx = parse_search_listing_context(payload)
    row = ctx["1629301846268091180"]
    assert row["nightly_price"] is None
    assert row["total_price"] == 1111
    assert row["price_nights"] == 2


def test_parse_search_context_falls_back_to_stayssearch_filter_state_and_search_input():
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "__typename": "SkinnyListingItem",
                                "listingId": "958866016198543537",
                            }
                        ],
                        "searchInput": {
                            "staysSearchInput": {
                                "guests": {
                                    "adults": {
                                        "searchParams": {
                                            "params": [
                                                {
                                                    "key": "adults",
                                                    "value": {"stringValue": "3"},
                                                }
                                            ]
                                        }
                                    }
                                }
                            }
                        },
                        "filterState": [
                            {
                                "key": "query",
                                "value": {"stringValue": "Belmont, California"},
                            },
                            {
                                "key": "room_types",
                                "value": {"stringValues": ["Entire home/apt"]},
                            },
                            {
                                "key": "min_bathrooms",
                                "value": {"integerValue": 1},
                            },
                        ],
                    }
                }
            }
        }
    }

    ctx = parse_search_listing_context(payload)
    row = ctx["958866016198543537"]
    assert row["location"] == "Belmont, California"
    assert row["accommodates"] is None
    assert row["baths"] == 1.0
    assert row["property_type"] == "entire_home"


def test_parse_search_context_uses_logging_metadata_location_and_homes_refinement_for_type():
    payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "__typename": "SkinnyListingItem",
                                "listingId": "1299373509745874199",
                            }
                        ],
                        "loggingMetadata": {
                            "remarketingLoggingData": {
                                "canonicalLocation": "Belmont, CA"
                            }
                        },
                        "filterState": [
                            {
                                "key": "refinement_paths",
                                "value": {"stringValues": ["/homes"]},
                            }
                        ],
                    }
                }
            }
        }
    }

    ctx = parse_search_listing_context(payload)
    row = ctx["1299373509745874199"]
    assert row["location"] == "Belmont, CA"
    assert row["property_type"] == "entire_home"


def test_parse_pdp_response_uses_book_it_floating_footer_structure_only():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_FLOATING_FOOTER",
                                "section": {
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "accessibilityLabel": "$173 CAD total",
                                            "price": "$173 CAD",
                                            "qualifier": "total",
                                        }
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "1629301846268091180", "https://www.airbnb.ca")
    assert out["total_price"] == 173.0
    assert out["nightly_price"] == 173.0
    assert out["currency"] == "CAD"


def test_parse_pdp_response_uses_book_it_sidebar_when_footer_absent():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "POLICIES_DEFAULT",
                                "section": {"available": True},
                            },
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "accessibilityLabel": "$480 CAD total",
                                            "price": "$480 CAD",
                                            "qualifier": "total",
                                        }
                                    },
                                },
                            },
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "1629301846268091180", "https://www.airbnb.ca")
    assert out["total_price"] == 480.0
    assert out["nightly_price"] == 480.0
    assert out["currency"] == "CAD"


def test_parse_pdp_response_falls_back_to_any_section_with_structured_display_price():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_NAV",
                                "section": {
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": "$330 CAD",
                                            "qualifier": "total",
                                        }
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "1629301846268091180", "https://www.airbnb.ca")
    assert out["total_price"] == 330.0
    assert out["nightly_price"] == 330.0
    assert out["currency"] == "CAD"


def test_parse_pdp_response_returns_no_price_when_booking_section_unavailable():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_FLOATING_FOOTER",
                                "section": {
                                    "available": False,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": "$295 CAD",
                                            "qualifier": "total",
                                        }
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "1629301846268091180", "https://www.airbnb.ca")
    assert out["total_price"] is None
    assert out["nightly_price"] is None
    assert out["currency"] is None


def test_parse_pdp_response_blocks_ghost_price_for_unavailable_july_2_room():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {
                                    "available": False,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": "$295 USD",
                                            "accessibilityLabel": "$295 USD for 1 night",
                                            "qualifier": "total",
                                        }
                                    },
                                    "errorMessage": "Those dates are not available",
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "797454009048233847", "https://www.airbnb.com")
    assert out["total_price"] is None
    assert out["nightly_price"] is None
    assert out["currency"] is None


def test_parse_pdp_response_falls_back_to_primary_accessibility_label():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_FLOATING_FOOTER",
                                "section": {
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": None,
                                            "discountedPrice": None,
                                            "accessibilityLabel": "$330 CAD total",
                                            "qualifier": "total",
                                        }
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "1629301846268091180", "https://www.airbnb.ca")
    assert out["total_price"] == 330.0
    assert out["nightly_price"] == 330.0
    assert out["currency"] == "CAD"


def test_parse_pdp_response_prefers_discounted_price_when_both_fields_exist():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_FLOATING_FOOTER",
                                "section": {
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": "$402 CAD",
                                            "discountedPrice": "$316 CAD",
                                            "accessibilityLabel": "$316 CAD total, originally $402 CAD",
                                            "qualifier": "total",
                                        }
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "1656642564791256745", "https://www.airbnb.ca")
    assert out["total_price"] == 316.0
    assert out["nightly_price"] == 316.0
    assert out["currency"] == "CAD"


def test_parse_pdp_response_divides_two_night_total():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "accessibilityLabel": "$300 CAD for 2 nights",
                                            "price": "$300 CAD",
                                            "qualifier": "total",
                                        }
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "1629301846268091180", "https://www.airbnb.ca")
    assert out["total_price"] == 300.0
    assert out["nightly_price"] == 150.0
    assert out["price_nights"] == 2
    assert out["price_kind"] == "trip_total_from_pdp"


def test_parse_pdp_response_prefers_nightly_breakdown_over_fee_inclusive_primary_total():
    """
    User-listing prices must reflect the nightly base rate shown by Airbnb,
    not a fee-inclusive booking total from primaryLine.
    """
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": "$1,048 USD",
                                            "qualifier": "total",
                                        },
                                        "explanationData": {
                                            "priceDetails": [
                                                {
                                                    "items": [
                                                        {
                                                            "description": "2 nights x $430 USD",
                                                            "priceString": "$860 USD",
                                                        },
                                                        {
                                                            "description": "Cleaning fee",
                                                            "priceString": "$188 USD",
                                                        },
                                                    ]
                                                }
                                            ]
                                        },
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "12345", "https://www.airbnb.com")

    assert out["nightly_price"] == 430.0
    assert out["total_price"] == 860.0
    assert out["price_nights"] == 2
    assert out["price_kind"] == "nightly_from_pdp_breakdown"
    assert out["currency"] == "USD"


def test_parse_pdp_response_uses_display_primary_when_breakdown_has_cent_artifact():
    """
    Real fixture shape: primaryLine is Airbnb's displayed price, while
    explanationData has a lower-level base amount with cents. The parser should
    choose the displayed primary amount when it matches the breakdown total
    within display precision.
    """
    fixture = Path("worker/tests/fixtures/pdp_sparse_1408676238256636483.json")
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    out = parse_pdp_response(payload, "1408676238256636483", "https://www.airbnb.ca")

    assert out["nightly_price"] == 321.0
    assert out["total_price"] == 321.0
    assert out["price_nights"] == 1
    assert out["price_kind"] == "nightly_from_pdp_breakdown"
    assert out["currency"] == "CAD"


def test_parse_pdp_response_uses_display_two_night_total_when_breakdown_has_cent_artifact():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": "$1,111 USD",
                                            "accessibilityLabel": "$1,111 USD for 2 nights",
                                            "qualifier": "total",
                                        },
                                        "explanationData": {
                                            "priceDetails": [
                                                {
                                                    "items": [
                                                        {
                                                            "description": "2 nights x $555.18 USD",
                                                            "priceString": "$1,110.36 USD",
                                                        }
                                                    ]
                                                }
                                            ]
                                        },
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "12345", "https://www.airbnb.com")

    assert out["nightly_price"] == 555.5
    assert out["total_price"] == 1111.0
    assert out["price_nights"] == 2
    assert out["price_kind"] == "nightly_from_pdp_breakdown"
    assert out["currency"] == "USD"


def test_parse_pdp_response_supports_amount_before_nights_breakdown_shape():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": "$1,048 USD",
                                            "qualifier": "total",
                                        },
                                        "explanationData": {
                                            "priceDetails": [
                                                {
                                                    "items": [
                                                        {
                                                            "description": "$430 USD x 2 nights",
                                                            "priceString": "$860 USD",
                                                        }
                                                    ]
                                                }
                                            ]
                                        },
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "12345", "https://www.airbnb.com")

    assert out["nightly_price"] == 430.0
    assert out["price_nights"] == 2
    assert out["price_kind"] == "nightly_from_pdp_breakdown"


def test_parse_pdp_response_prefers_one_night_price_after_discount_over_primary_price():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": "$769 USD",
                                            "accessibilityLabel": "$684 USD for 1 night",
                                            "qualifier": "for 1 night",
                                        },
                                        "explanationData": {
                                            "priceDetails": [
                                                {
                                                    "items": [
                                                        {
                                                            "description": "Price after discount",
                                                            "priceString": "$684 USD",
                                                        }
                                                    ]
                                                }
                                            ]
                                        },
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "12345", "https://www.airbnb.com")

    assert out["nightly_price"] == 684
    assert out["total_price"] == 684
    assert out["price_nights"] == 1
    assert out["price_kind"] == "nightly_from_pdp_breakdown"
    assert out["currency"] == "USD"


def test_parse_pdp_response_prefers_two_night_discount_breakdown_over_original_primary_price():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {
                                    "available": True,
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": "$5,648 USD",
                                            "accessibilityLabel": "$4,659 USD for 2 nights, originally $5,648 USD",
                                            "qualifier": "total",
                                        },
                                        "explanationData": {
                                            "priceDetails": [
                                                {
                                                    "items": [
                                                        {
                                                            "description": "Price after discount",
                                                            "priceString": "$4,659 USD",
                                                        }
                                                    ]
                                                }
                                            ]
                                        },
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "12345", "https://www.airbnb.com")

    assert out["nightly_price"] == 2329.5
    assert out["total_price"] == 4659.0
    assert out["price_nights"] == 2
    assert out["price_kind"] == "nightly_from_pdp_breakdown"
    assert out["currency"] == "USD"


def test_parse_pdp_response_reads_overview_v2_capacity_layout_and_location():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sbuiData": {
                            "sectionConfiguration": {
                                "root": {
                                    "sections": [
                                        {
                                            "sectionId": "OVERVIEW_DEFAULT_V2",
                                            "sectionData": {
                                                "title": "Entire guest suite in Belmont, California, United States",
                                                "overviewItems": [
                                                    {"title": "3 guests"},
                                                    {"title": "1 bedroom"},
                                                    {"title": "2 beds"},
                                                    {"title": "1 bath"},
                                                ],
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "50490675", "https://www.airbnb.ca")
    assert out["location"] == "Belmont, California, United States"
    assert out["city"] == "Belmont"
    assert out["state"] == "California"
    assert out["country"] == "United States"
    assert out["property_type"] == "Entire guest suite"
    assert out["accommodates"] == 3
    assert out["bedrooms"] == 1
    assert out["beds"] == 2
    assert out["baths"] == 1.0


def test_parse_pdp_response_reads_location_from_metadata_sharing_config():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "metadata": {
                            "sharingConfig": {
                                "location": "Belmont",
                                "propertyType": "Entire guest suite",
                                "personCapacity": 3,
                            }
                        }
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "50490675", "https://www.airbnb.ca")
    assert out["location"] == "Belmont"
    assert out["city"] == "Belmont"
    assert out["property_type"] == "Entire guest suite"
    assert out["accommodates"] == 3


def test_parse_pdp_response_does_not_use_legacy_location_or_structural_fallbacks():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "LOCATION_DEFAULT",
                                "section": {"subtitle": "Old City, Old State, Old Country"},
                            }
                        ],
                        "metadata": {},
                        "sbuiData": {"sectionConfiguration": {"root": {"sections": []}}},
                    }
                }
            }
        },
        "personCapacity": 9,
        "beds": 7,
        "bathrooms": 3,
    }

    out = parse_pdp_response(payload, "999", "https://www.airbnb.ca")
    assert out["location"] is None
    assert out["accommodates"] is None
    assert out["beds"] is None
    assert out["baths"] is None


def test_parse_pdp_response_prefers_property_type_from_metadata_sharing_config():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "metadata": {
                            "sharingConfig": {
                                "location": "Belmont",
                                "propertyType": "Entire guest suite",
                                "personCapacity": 3,
                            }
                        },
                        "sbuiData": {
                            "sectionConfiguration": {
                                "root": {
                                    "sections": [
                                        {
                                            "sectionId": "OVERVIEW_DEFAULT_V2",
                                            "sectionData": {
                                                "title": "Private room in Belmont, California, United States",
                                                "overviewItems": [{"title": "1 bedroom"}],
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

    out = parse_pdp_response(payload, "50490675", "https://www.airbnb.ca")
    assert out["property_type"] == "Entire guest suite"


def test_parse_pdp_response_reads_metadata_from_sections_container_shape():
    payload = {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "__typename": "StayPDPSections",
                        "sections": [],
                        "metadata": {
                            "__typename": "StayPDPMetadata",
                            "sharingConfig": {
                                "__typename": "PdpSharingConfig",
                                "propertyType": "Entire guest suite",
                                "location": "Belmont",
                                "personCapacity": 3,
                            },
                        },
                        "sbuiData": {"sectionConfiguration": {"root": {"sections": []}}},
                    }
                }
            }
        }
    }

    out = parse_pdp_response(payload, "50490675", "https://www.airbnb.ca")
    assert out["location"] == "Belmont"
    assert out["city"] == "Belmont"
    assert out["property_type"] == "Entire guest suite"
    assert out["accommodates"] == 3


def test_parse_pdp_baths_property_type_fast_reads_only_required_fields():
    payload = {
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
                                                    {"title": "3 guests"},
                                                    {"title": "1 bedroom"},
                                                    {"title": "2 beds"},
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
    out = parse_pdp_baths_property_type_fast(payload)
    assert out["property_type"] == "Entire guest suite"
    assert out["baths"] == 1.0
