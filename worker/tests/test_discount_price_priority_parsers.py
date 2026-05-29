from worker.scraper.parsers import parse_pdp_response, parse_search_listing_context
from worker.scraper.parsers_deepbnb import (
    parse_deepbnb_pdp_to_stayspdp_payload,
    parse_deepbnb_search_to_stayssearch_payload,
)


def test_deepbnb_search_conversion_prefers_discounted_primary_price():
    payload = {
        "data": {
            "dora": {
                "exploreV3": {
                    "sections": [
                        {
                            "sectionComponentType": "listings_ListingsGrid_Explore",
                            "items": [
                                {
                                    "listing": {"id": 12345, "name": "Discount Listing"},
                                    "pricingQuote": {
                                        "structuredStayDisplayPrice": {
                                            "primaryLine": {
                                                "price": "$402 USD",
                                                "discountedPrice": "$316 USD",
                                                "qualifier": "total",
                                            }
                                        }
                                    },
                                }
                            ],
                        }
                    ]
                }
            }
        }
    }

    converted = parse_deepbnb_search_to_stayssearch_payload(
        payload,
        checkin="2026-06-01",
        checkout="2026-06-03",
        currency="USD",
    )
    ctx = parse_search_listing_context(converted)
    row = ctx["12345"]
    assert row["total_price"] == 316.0
    assert row["currency"] == "USD"


def test_deepbnb_pdp_conversion_prefers_discounted_primary_price():
    payload = {
        "data": {
            "merlin": {
                "pdpSections": {
                    "sections": [
                        {
                            "sectionId": "BOOK_IT_FLOATING_FOOTER",
                            "section": {
                                "structuredDisplayPrice": {
                                    "primaryLine": {
                                        "price": "$402 USD",
                                        "discountedPrice": "$316 USD",
                                        "accessibilityLabel": "$316 USD total, originally $402 USD",
                                        "qualifier": "total",
                                    }
                                }
                            },
                        }
                    ],
                    "metadata": {"sharingConfig": {"location": "Austin", "propertyType": "Entire home"}},
                }
            }
        }
    }

    converted = parse_deepbnb_pdp_to_stayspdp_payload(
        payload,
        listing_id="12345",
        checkin="2026-06-01",
        checkout="2026-06-03",
        currency="USD",
    )
    out = parse_pdp_response(converted, "12345", "https://www.airbnb.com")
    assert out["total_price"] == 316.0
    assert out["nightly_price"] == 316.0
    assert out["currency"] == "USD"
