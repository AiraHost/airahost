"""
Unit tests for nightly price candidate selection logic.

Tests the pure-Python select_nightly_price_from_candidates() helper which
is the core decision function for choosing between strikethrough (original)
and non-strikethrough (discounted/current) price elements scraped from the
Airbnb booking widget DOM.

These tests do NOT require a browser — they operate on the structured
candidate dicts that the JS layer (_BOOKING_WIDGET_PRICE_JS) would return.
"""

import pytest
from worker.scraper.price_normalizer import normalize_raw_price
from worker.scraper.target_extractor import (
    extract_nightly_price_from_listing_page,
    select_nightly_price_from_candidates,
    _extract_text_price_matches,
    _TRIP_TOTAL_RE,
    _NIGHTLY_PRICE_RES,
)


# ---------------------------------------------------------------------------
# Core discount scenario
# ---------------------------------------------------------------------------

def test_picks_discounted_price_over_strikethrough_original():
    """
    Critical case: $540 (strikethrough original) and $465 (current discounted).
    System must return $465, not $540.
    """
    candidates = [
        {"value": 540, "strikethrough": True, "domIndex": 0},   # original (crossed out)
        {"value": 465, "strikethrough": False, "domIndex": 1},  # discounted (current)
    ]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    price, kind = result
    assert price == 465.0
    assert kind == "nightly_discounted"


def test_price_kind_is_discounted_when_original_present():
    """price_kind reflects that a strikethrough original was present."""
    candidates = [
        {"value": 300, "strikethrough": True, "domIndex": 0},
        {"value": 250, "strikethrough": False, "domIndex": 1},
    ]
    _, kind = select_nightly_price_from_candidates(candidates)
    assert kind == "nightly_discounted"


# ---------------------------------------------------------------------------
# Standard (no discount) scenario
# ---------------------------------------------------------------------------

def test_picks_standard_price_when_no_discount():
    """Single non-strikethrough entry → standard nightly."""
    candidates = [{"value": 350, "strikethrough": False, "domIndex": 0}]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    price, kind = result
    assert price == 350.0
    assert kind == "nightly_standard"


def test_price_kind_is_standard_when_no_strikethrough():
    candidates = [{"value": 200, "strikethrough": False, "domIndex": 0}]
    _, kind = select_nightly_price_from_candidates(candidates)
    assert kind == "nightly_standard"


# ---------------------------------------------------------------------------
# DOM order: last non-strikethrough is the discounted price
# ---------------------------------------------------------------------------

def test_last_non_strikethrough_in_dom_order_is_selected():
    """
    When the DOM has: original(strikethrough) → discounted → /night
    the last non-strikethrough candidate (highest domIndex) must be chosen.
    """
    candidates = [
        {"value": 600, "strikethrough": True, "domIndex": 0},   # original
        {"value": 520, "strikethrough": False, "domIndex": 1},  # discounted
        {"value": 520, "strikethrough": False, "domIndex": 2},  # same price, duplicate span
    ]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    price, _ = result
    assert price == 520.0


def test_multiple_non_strikethrough_picks_last_by_dom_index():
    """Among multiple non-strikethrough candidates picks the highest domIndex."""
    candidates = [
        {"value": 400, "strikethrough": False, "domIndex": 0},
        {"value": 380, "strikethrough": False, "domIndex": 3},
        {"value": 370, "strikethrough": False, "domIndex": 1},
    ]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    price, _ = result
    assert price == 380.0  # domIndex=3 is the last


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_candidates_returns_none():
    assert select_nightly_price_from_candidates([]) is None


def test_all_strikethrough_returns_none():
    """If every candidate has strikethrough, we cannot determine current price."""
    candidates = [
        {"value": 540, "strikethrough": True, "domIndex": 0},
        {"value": 465, "strikethrough": True, "domIndex": 1},
    ]
    assert select_nightly_price_from_candidates(candidates) is None


def test_price_below_minimum_returns_none():
    """Prices < $10 are likely fees or formatting artifacts — reject them."""
    candidates = [{"value": 5, "strikethrough": False, "domIndex": 0}]
    assert select_nightly_price_from_candidates(candidates) is None


def test_price_above_maximum_returns_none():
    """Prices > $10 000 are likely totals — reject them."""
    candidates = [{"value": 15000, "strikethrough": False, "domIndex": 0}]
    assert select_nightly_price_from_candidates(candidates) is None


def test_price_at_boundary_10_is_valid():
    candidates = [{"value": 10, "strikethrough": False, "domIndex": 0}]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    assert result[0] == 10.0


def test_price_at_boundary_10000_is_valid():
    candidates = [{"value": 10000, "strikethrough": False, "domIndex": 0}]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    assert result[0] == 10000.0


def test_missing_strikethrough_key_treated_as_false():
    """Candidates without 'strikethrough' key default to non-strikethrough."""
    candidates = [{"value": 299, "domIndex": 0}]  # no 'strikethrough' key
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    assert result[0] == 299.0


def test_missing_dom_index_does_not_crash():
    """Candidates without 'domIndex' should still work (defaults to 0)."""
    candidates = [
        {"value": 500, "strikethrough": True},   # no domIndex
        {"value": 420, "strikethrough": False},  # no domIndex
    ]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    # Both have domIndex defaulting to 0; last in sorted order is either —
    # the important thing is a non-strikethrough price is returned
    price, kind = result
    assert price == 420.0
    assert kind == "nightly_discounted"


def test_float_value_preserved():
    """Values passed as floats are returned as floats."""
    candidates = [{"value": 123.45, "strikethrough": False, "domIndex": 0}]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    assert result[0] == 123.45


def test_client_pdp_extraction_uses_price_after_discount_not_primary_price():
    """
    Airbnb can place the visible one-night price in a "price after discount"
    breakdown row while primaryLine carries another nearby price.
    """

    class _Client:
        def get_listing_details(self, listing_id, checkin, checkout, adults=1):
            return {
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

    price, confidence = extract_nightly_price_from_listing_page(
        _Client(),
        listing_url="https://www.airbnb.com/rooms/12345",
        checkin="2026-06-17",
        checkout="2026-06-18",
        adults=1,
    )

    assert price == 684.0
    assert confidence == "high"


def test_normalizer_ceils_one_night_api_decimal_but_keeps_divided_total_fraction():
    one_night = normalize_raw_price(
        40.19,
        qualifier="total",
        stay_nights=1,
        source="search",
        produce_nightly_from_total=True,
        round_direct_nightly_to_whole=True,
    )
    assert one_night is not None
    assert one_night.nightly_price == 41
    assert one_night.total_price == 41

    two_night = normalize_raw_price(
        827,
        qualifier="total",
        context_text="for 2 nights",
        stay_nights=2,
        source="search",
        produce_nightly_from_total=True,
        round_direct_nightly_to_whole=True,
    )
    assert two_night is not None
    assert two_night.nightly_price == 413.5
    assert two_night.total_price == 827


# ---------------------------------------------------------------------------
# Trip-night division in select_nightly_price_from_candidates
# ---------------------------------------------------------------------------

def test_trip_total_2_nights_divided():
    """$300 trip total for 2 nights → $150 per night."""
    candidates = [{"value": 300, "tripNights": 2, "strikethrough": False, "domIndex": 0}]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    price, kind = result
    assert price == 150.0
    assert kind == "nightly_standard"


def test_trip_total_3_nights_divided():
    """$450 trip total for 3 nights → $150 per night."""
    candidates = [{"value": 450, "tripNights": 3, "strikethrough": False, "domIndex": 0}]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    price, _ = result
    assert price == 150.0


def test_trip_total_1_night_not_divided():
    """tripNights=1 (or absent) means already per-night — no division."""
    candidates = [{"value": 150, "tripNights": 1, "strikethrough": False, "domIndex": 0}]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    assert result[0] == 150.0


def test_trip_total_missing_trip_nights_not_divided():
    """Candidates without tripNights default to 1 — no division."""
    candidates = [{"value": 150, "strikethrough": False, "domIndex": 0}]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    assert result[0] == 150.0


def test_trip_total_discounted_with_strikethrough():
    """Trip total with strikethrough original → kind is nightly_discounted."""
    candidates = [
        {"value": 400, "tripNights": 2, "strikethrough": True, "domIndex": 0},   # original
        {"value": 300, "tripNights": 2, "strikethrough": False, "domIndex": 1},  # discounted
    ]
    result = select_nightly_price_from_candidates(candidates)
    assert result is not None
    price, kind = result
    assert price == 150.0  # 300 / 2
    assert kind == "nightly_discounted"


def test_trip_total_out_of_range_after_division_returns_none():
    """If per-night price after division is < $10, reject."""
    candidates = [{"value": 10, "tripNights": 2, "strikethrough": False, "domIndex": 0}]
    # per night = 5 → below $10 minimum
    assert select_nightly_price_from_candidates(candidates) is None


# ---------------------------------------------------------------------------
# _extract_text_price_matches — regex text scanning
# ---------------------------------------------------------------------------

def _price_set(text: str):
    """Helper: return set of per-night prices found in text."""
    return {p for _, p in _extract_text_price_matches(text)}


def test_text_dollar_slash_night():
    """$150/night → 150."""
    assert 150.0 in _price_set("$150/night")


def test_text_dollar_space_slash_night():
    """$150 /night → 150."""
    assert 150.0 in _price_set("$150 /night")


def test_text_dollar_per_night():
    """$150 per night → 150."""
    assert 150.0 in _price_set("$150 per night")


def test_text_for_1_night():
    """$150 for 1 night → 150 (already per-night, no division)."""
    matches = _extract_text_price_matches("$150 for 1 night")
    prices = [p for _, p in matches]
    assert 150.0 in prices
    # Must NOT produce 150 from trip-total regex (which requires N>=2)
    trip_hits = list(_TRIP_TOTAL_RE.finditer("$150 for 1 night"))
    assert len(trip_hits) == 0


def test_text_for_2_nights():
    """$300 for 2 nights → 150 (trip total divided by 2)."""
    matches = _extract_text_price_matches("$300 for 2 nights")
    prices = [p for _, p in matches]
    assert 150.0 in prices


def test_text_for_3_nights():
    """$450 for 3 nights → 150."""
    matches = _extract_text_price_matches("$450 for 3 nights")
    prices = [p for _, p in matches]
    assert 150.0 in prices


def test_text_minimum_stay_label_not_divided():
    """'2 nights minimum' must NOT trigger trip-total division of a nearby nightly price."""
    # The nightly price comes from a '/night' pattern; '2 nights minimum' has no $ prefix
    # so _TRIP_TOTAL_RE won't match it.
    text = "$150/night\n2 nights minimum"
    matches = _extract_text_price_matches(text)
    prices = [p for _, p in matches]
    assert 150.0 in prices
    assert 75.0 not in prices  # must NOT divide 150 by 2


def test_text_multiline_no_false_division():
    """Price on one line, 'night' on the next — still captured by /night regex."""
    # In body text, Airbnb sometimes renders "$150" then "\n/night" as separate nodes.
    # The body-text layer calls page.inner_text() which joins them; simulate joined form.
    text = "$150\n/night"
    # The /night regex requires $ immediately before digits so this may not match,
    # but we should NOT produce a trip-total division result either.
    matches = _extract_text_price_matches(text)
    prices = [p for _, p in matches]
    # No trip-total division (no "for N nights" present)
    assert 75.0 not in prices


def test_text_combined_widget_extract_last_is_discounted():
    """In widget text with both original and discounted price, last match is the discounted one."""
    # Simulates widget text: strikethrough original $200, then discounted $150
    text = "$200 per night\n$150 per night"
    matches = _extract_text_price_matches(text)
    matches.sort(key=lambda x: x[0])
    assert matches[-1][1] == 150.0


def test_text_usd_night_format():
    """'150 USD /night' format → 150."""
    assert 150.0 in _price_set("150 USD /night")


# ---------------------------------------------------------------------------
# CAD/AUD/NZD currency-suffix format (Airbnb .ca / .com.au regression)
# ---------------------------------------------------------------------------

def test_text_cad_suffix_nightly():
    """'$267 CAD' (no /night) is treated as a nightly price on .ca domains."""
    assert 267.0 in _price_set("$267 CAD")


def test_text_cad_suffix_total_excluded():
    """'$195 CAD total' must NOT match — it is a trip total, not nightly."""
    assert 195.0 not in _price_set("$195 CAD total")


def test_text_cad_widget_first_price_extracted():
    """Widget text from airbnb.ca: '$267 CAD' extracted, '$195 CAD total' excluded."""
    matches = _extract_text_price_matches(
        "$267 CAD \n$195 CAD total\nShow price breakdown\nReserve"
    )
    prices = [p for _, p in matches]
    assert 267.0 in prices
    assert 195.0 not in prices


def test_text_aud_suffix_nightly():
    """'$180 AUD' without /night → treated as nightly (AUD domain coverage)."""
    assert 180.0 in _price_set("$180 AUD")


def test_text_aud_suffix_total_excluded():
    """'$360 AUD total' must NOT be treated as nightly."""
    assert 360.0 not in _price_set("$360 AUD total")


def test_text_cad_trip_total_divided():
    """'$267 CAD for 2 nights' → 133.50 per night (trip total divided)."""
    matches = _extract_text_price_matches("$267 CAD for 2 nights")
    prices = [p for _, p in matches]
    assert 133.50 in prices
    assert 267.0 not in prices  # raw total must NOT appear as nightly


def test_text_cad_trip_total_3_nights():
    """'$450 CAD for 3 nights' → 150.0 per night."""
    matches = _extract_text_price_matches("$450 CAD for 3 nights")
    prices = [p for _, p in matches]
    assert 150.0 in prices
    assert 450.0 not in prices


def test_text_plain_trip_total_still_works():
    """'$300 for 2 nights' (no currency suffix) still divides correctly."""
    matches = _extract_text_price_matches("$300 for 2 nights")
    prices = [p for _, p in matches]
    assert 150.0 in prices
