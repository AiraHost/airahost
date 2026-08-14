"""The StaysSearch result contract, applied at every acceptance boundary.

The incident produced `ids=18 context=18 priced_rows=0` because a 2xx response
with a well-shaped `searchResults` list was accepted as a successful page even
though every row held nothing but a listing ID. This suite pins the four
outcomes callers may see, and the rule that an ID-only row is not usable for
date-specific pricing.
"""

from __future__ import annotations

import base64

from worker.scraper.parsers import parse_search_listing_context
from worker.scraper.search_result_contract import (
    BLOCKED,
    DEGRADED,
    USABLE,
    VALID_EMPTY,
    classify_search_payload,
    payload_has_auth_error,
    row_is_priced,
)


def _gid(listing_id: str) -> str:
    return base64.b64encode(f"DemandStayListing:{listing_id}".encode("utf-8")).decode("utf-8")


def _wrap(search_results, **extra):
    payload = {
        "data": {"presentation": {"staysSearch": {"results": {"searchResults": search_results}}}}
    }
    payload.update(extra)
    return payload


def _priced_row(listing_id: str, price: str = "$210 USD"):
    return {
        "demandStayListing": {"id": _gid(listing_id)},
        "available": True,
        "structuredDisplayPrice": {
            "primaryLine": {"price": price, "qualifier": "night", "accessibilityLabel": price},
        },
    }


# ── Outcomes ─────────────────────────────────────────────────────────────────

def test_well_formed_priced_payload_is_usable_and_counts_priced_rows():
    payload = _wrap([_priced_row("111"), _priced_row("222")])
    ctx = parse_search_listing_context(payload)
    state = classify_search_payload(payload, 200, context=ctx)
    assert state.outcome == USABLE
    assert state.result_count == 2
    assert state.priced_count == 2
    assert state.is_authoritative is True


def test_error_free_empty_payload_is_valid_empty_not_degraded():
    state = classify_search_payload(_wrap([]), 200)
    assert state.outcome == VALID_EMPTY
    assert state.reason_code == "empty_result_set"
    assert state.is_authoritative is True
    assert state.is_blocked is False


def test_graphql_auth_error_payload_is_blocked_even_with_results():
    payload = _wrap(
        [_priced_row("111")],
        errors=[{"message": "Client is not authorized", "extensions": {"code": "UNAUTHORIZED"}}],
    )
    state = classify_search_payload(payload, 200)
    assert state.outcome == BLOCKED
    assert state.reason_code == "graphql_auth_error"
    assert state.is_authoritative is False


def test_empty_payload_with_auth_error_at_deep_offset_is_not_valid_empty():
    # Required behavior 7: a challenge at a deep offset must never be read as
    # "result set exhausted", which is what stops deeper paging.
    payload = _wrap([], errors=[{"message": "captcha required"}])
    state = classify_search_payload(payload, 200)
    assert state.outcome == BLOCKED
    assert state.is_authoritative is False


def test_empty_payload_with_non_auth_graphql_errors_is_degraded_not_valid_empty():
    payload = _wrap([], errors=[{"message": "internal server hiccup"}])
    state = classify_search_payload(payload, 200)
    assert state.outcome == DEGRADED
    assert state.is_authoritative is False


def test_missing_search_results_node_is_degraded():
    assert classify_search_payload({"data": {}}, 200).outcome == DEGRADED
    assert classify_search_payload(None, 200).outcome == DEGRADED
    assert classify_search_payload("not a payload", 200).outcome == DEGRADED


def test_http_401_403_are_blocked_and_other_non_2xx_are_degraded():
    assert classify_search_payload(_wrap([]), 403).outcome == BLOCKED
    assert classify_search_payload(_wrap([]), 401).outcome == BLOCKED
    assert classify_search_payload(_wrap([]), 500).outcome == DEGRADED


def test_payload_has_auth_error_ignores_ordinary_error_text():
    assert payload_has_auth_error({"errors": [{"message": "timeout talking to service"}]}) is False
    assert payload_has_auth_error({"errors": [{"message": "CSRF token invalid"}]}) is True


# ── Row-level rule: ID-only rows are never priced comps ──────────────────────

def test_id_only_payload_yields_no_priced_rows():
    # Exactly the synthetic payload the removed DOM fallback used to build.
    payload = _wrap([{"listingId": str(1000 + i)} for i in range(18)])
    ctx = parse_search_listing_context(payload)
    state = classify_search_payload(payload, 200, context=ctx)

    assert state.outcome == USABLE  # structurally well-formed...
    assert state.result_count == 18
    assert state.priced_count == 0  # ...but nothing in it can be priced.
    assert all(row.get("is_available") is None for row in ctx.values())


def test_row_is_priced_requires_explicit_availability_and_a_positive_price():
    assert row_is_priced({"is_available": True, "nightly_price": 210.0}) is True
    assert row_is_priced({"is_available": True, "total_price": 420.0}) is True
    # Unknown availability is not availability.
    assert row_is_priced({"is_available": None, "nightly_price": 210.0}) is False
    # Explicitly unavailable rows keep their ghost-price protection.
    assert row_is_priced({"is_available": False, "nightly_price": 210.0}) is False
    assert row_is_priced({"is_available": True, "nightly_price": 0}) is False
    assert row_is_priced({"is_available": True}) is False


def test_empty_amenities_never_affect_usability_or_pricing():
    # Required behavior: [] amenities is a valid state, not a degradation signal.
    payload = _wrap([_priced_row("111")])
    ctx = parse_search_listing_context(payload)
    assert ctx["111"]["amenities"] == []
    state = classify_search_payload(payload, 200, context=ctx)
    assert state.outcome == USABLE
    assert state.priced_count == 1
    assert row_is_priced(ctx["111"]) is True
