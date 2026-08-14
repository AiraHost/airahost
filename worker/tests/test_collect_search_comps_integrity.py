"""collect_search_comps() must not let a bad page contaminate the comp pool.

The collector is the last line of defense: even if an upstream caller regresses
and hands it a synthesized or blocked payload, nothing unusable may reach the
candidate pool, and a blocked session must not masquerade as empty inventory or
as an exhausted result set.
"""

from __future__ import annotations

import base64
from datetime import date
from typing import Any, Dict, List

import pytest

from worker.scraper.comp_collection import collect_search_comps
from worker.scraper.scraper_errors import AirbnbSearchBlocked


def _gid(listing_id: str) -> str:
    return base64.b64encode(f"DemandStayListing:{listing_id}".encode("utf-8")).decode("utf-8")


def _full_row(listing_id: str, price: str = "$200 CAD", *, available=True, amenities=None):
    row: Dict[str, Any] = {
        "demandStayListing": {"id": _gid(listing_id)},
        "available": available,
        "title": f"Home {listing_id}",
        "personCapacity": 2,
        "structuredDisplayPrice": {"primaryLine": {"price": price, "qualifier": "total"}},
    }
    if amenities is not None:
        row["amenities"] = amenities
    return row


def _wrap(rows, **extra):
    payload = {"data": {"presentation": {"staysSearch": {"results": {"searchResults": rows}}}}}
    payload.update(extra)
    return payload


_EMPTY = _wrap([])


def _collect(client, **kwargs):
    params = dict(
        client=client,
        search_location="Toronto, ON",
        base_origin="https://www.airbnb.ca",
        date_i=date(2026, 9, 6),
        adults=2,
        max_scroll_rounds=1,
        max_cards=20,
        rate_limit_seconds=0.0,
        target_accommodates=2,
    )
    params.update(kwargs)
    return collect_search_comps(**params)


class _ScriptedClient:
    """Returns one scripted (status, payload) per call, then repeats the last."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []

    def search_listings_with_overrides(self, overrides):
        self.calls.append(dict(overrides))
        if len(self.script) > 1:
            return self.script.pop(0)
        return self.script[0]


# ── Required test 9: ID-only payloads yield nothing, priced or unpriced ──────

def test_id_only_payload_produces_no_comps_and_no_unpriced_fallback():
    # Exactly the payload the removed rendered-DOM fallback used to synthesize:
    # 18 rows, each carrying only a listing ID.
    id_only = _wrap([{"listingId": str(9000 + i)} for i in range(18)])
    client = _ScriptedClient([(200, id_only)])

    comps, _ = _collect(client)

    assert comps == [], "ID-only rows must not enter the pool, priced or unpriced"


def test_id_only_rows_are_rejected_for_unknown_availability_not_silently_kept(caplog):
    id_only = _wrap([{"listingId": str(9000 + i)} for i in range(3)])
    client = _ScriptedClient([(200, id_only)])

    with caplog.at_level("INFO", logger="worker.scraper.comp_collection"):
        comps, _ = _collect(client, target_accommodates=None)

    assert comps == []
    funnel = " ".join(r.getMessage() for r in caplog.records)
    assert "unknown_availability=3" in funnel


# ── Required test 8: unknown availability is not availability ───────────────

def test_priced_row_with_unknown_availability_is_rejected():
    # A card that shows a price but never claims availability for these dates
    # cannot back a date-specific comp.
    row = {
        "demandStayListing": {"id": _gid("111")},
        "title": "Home 111",
        "personCapacity": 2,
        "structuredDisplayPrice": {"primaryLine": {"price": "$200 CAD", "qualifier": "total"}},
    }
    client = _ScriptedClient([(200, _wrap([row]))])

    comps, _ = _collect(client)
    assert comps == []


def test_explicitly_available_priced_row_is_accepted():
    client = _ScriptedClient([(200, _wrap([_full_row("111")]))])
    comps, qn = _collect(client)
    assert [c.url for c in comps] == ["https://www.airbnb.com/rooms/111"]
    assert qn == 1


# ── Required test 11: unavailable / minimum-stay protections survive ────────

def test_explicitly_unavailable_row_keeps_its_ghost_price_protection():
    client = _ScriptedClient([(200, _wrap([_full_row("111", available=False)]))])
    comps, _ = _collect(client)
    assert comps == []


def test_minimum_stay_row_is_still_rejected_for_a_one_night_query():
    row = _full_row("111")
    row["minNightsText"] = "Minimum stay of 2 nights"
    client = _ScriptedClient([(200, _wrap([row]))])
    comps, _ = _collect(client, prefer_one_night=True)
    assert comps == []


# ── Required test 21: empty amenities remain valid ──────────────────────────

def test_empty_amenities_do_not_block_acceptance():
    client = _ScriptedClient([(200, _wrap([_full_row("111", amenities=[])]))])
    comps, _ = _collect(client)
    assert len(comps) == 1
    assert comps[0].amenities == []


# ── Required test 13: blocked pages are counted, never "exhausted" ──────────

def test_blocked_page_propagates_and_is_not_read_as_empty_inventory():
    blocked = _wrap([], errors=[{"message": "captcha challenge required"}])
    client = _ScriptedClient([(200, blocked)])

    with pytest.raises(AirbnbSearchBlocked) as ctx:
        _collect(client)
    assert ctx.value.reason_code == "graphql_auth_error"


def test_blocked_page_logs_blocked_pages_and_no_valid_empty(caplog):
    blocked = _wrap([], errors=[{"message": "captcha challenge required"}])
    client = _ScriptedClient([(200, blocked)])

    with caplog.at_level("WARNING", logger="worker.scraper.comp_collection"):
        with pytest.raises(AirbnbSearchBlocked):
            _collect(client)

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "search page blocked" in messages
    assert "result set exhausted" not in messages


def test_malformed_page_is_skipped_without_becoming_a_valid_page(caplog):
    malformed = {"data": {"presentation": {}}}
    client = _ScriptedClient([(200, malformed)])

    with caplog.at_level("WARNING", logger="worker.scraper.comp_collection"):
        comps, _ = _collect(client)

    assert comps == []
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "search page degraded" in messages
    assert "result set exhausted" not in messages


# ── Required test 12: valid earlier pages survive a later empty page ────────

def test_valid_first_page_plus_authoritative_empty_page_returns_candidates_and_stops():
    first_page = _wrap([_full_row(str(100 + i)) for i in range(3)])
    client = _ScriptedClient([(200, first_page), (200, _EMPTY)])

    comps, _ = _collect(client, min_priced_target=10)

    urls = sorted(c.url for c in comps)
    assert urls == [f"https://www.airbnb.com/rooms/{100 + i}" for i in range(3)]
    # Paging stopped at the first page that added nothing new, rather than
    # sweeping every remaining offset.
    offsets = [call.get("itemsOffset", 0) for call in client.calls]
    assert offsets == [0, 20]


def test_authoritative_empty_first_page_yields_no_comps_and_no_exception():
    client = _ScriptedClient([(200, _EMPTY)])
    comps, qn = _collect(client)
    assert comps == []
    assert qn == 1
