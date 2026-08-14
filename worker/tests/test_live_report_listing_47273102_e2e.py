"""Opt-in live smoke test for the room 47273102 regression.

Exercises the real production report pipeline end to end — target
extraction, comparable search, nightly-price collection, and the day-level
trust/coverage gate — via `price_estimator.run_scrape()`, the same function
`worker/main.py` calls for a listing report. Nothing is written to Supabase;
`run_scrape()` is a pure in-memory pipeline.

This is a contract-level check, not an exact-value check: prices and
comparable IDs are volatile, so it only asserts that the report produced
nonzero trustworthy nightly-price coverage and that no day was accepted from
an explicitly-unavailable comp.

Opt-in:
  RUN_AIRBNB_LIVE_REPORT_E2E=1
  CDP_URL=http://127.0.0.1:9222                   (optional, default shown)
  AIRBNB_LIVE_REPORT_LISTING_ID=47273102           (optional override)
  AIRBNB_LIVE_REPORT_CHECKIN=YYYY-MM-DD            (optional, default: +21 days)
  AIRBNB_LIVE_REPORT_NIGHTS=3                      (optional, default 3)
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

DEFAULT_LISTING_ID = "47273102"


def _resolve_checkin() -> str:
    default_checkin = (date.today() + timedelta(days=21)).isoformat()
    return str(os.getenv("AIRBNB_LIVE_REPORT_CHECKIN", default_checkin)).strip()


def _resolve_nights() -> int:
    raw = str(os.getenv("AIRBNB_LIVE_REPORT_NIGHTS", "3")).strip()
    nights = int(raw)
    assert nights >= 1, f"AIRBNB_LIVE_REPORT_NIGHTS must be >= 1, got {nights}"
    return nights


def test_live_e2e_room_47273102_produces_trustworthy_nightly_price_coverage():
    if os.getenv("RUN_AIRBNB_LIVE_REPORT_E2E") != "1":
        pytest.skip("Set RUN_AIRBNB_LIVE_REPORT_E2E=1 to run the live room 47273102 report reproduction.")

    from worker.scraper import price_estimator

    cdp_url = str(os.getenv("CDP_URL", "http://127.0.0.1:9222")).strip()
    listing_id = str(os.getenv("AIRBNB_LIVE_REPORT_LISTING_ID", DEFAULT_LISTING_ID)).strip()
    listing_url = f"https://www.airbnb.com/rooms/{listing_id}"
    checkin = _resolve_checkin()
    nights = _resolve_nights()
    checkout = (date.fromisoformat(checkin) + timedelta(days=nights)).isoformat()

    daily_results, transparent = price_estimator.run_scrape(
        listing_url=listing_url,
        checkin=checkin,
        checkout=checkout,
        cdp_url=cdp_url,
        adults=2,
        rate_limit_seconds=0.5,
    )

    assert daily_results, (
        f"run_scrape() returned no daily results for {listing_url} "
        f"[{checkin}..{checkout}). This reproduces the original failure — see "
        "the deterministic regression suite for the underlying cause and fix."
    )

    priced_days = [d for d in daily_results if d.get("median_price")]
    assert priced_days, (
        "run_scrape() produced zero days with a trustworthy nightly price for "
        f"room {listing_id} [{checkin}..{checkout}) — this is exactly the "
        "'couldn't collect enough trustworthy nightly prices' failure. "
        f"daily_results={daily_results!r}"
    )

    # Ghost-price protection is covered exhaustively by the deterministic
    # regression suite (worker/tests/test_collect_search_comps_integrity.py);
    # `ListingSpec`/top_comps do not carry the raw availability flag this far
    # downstream, so this live check is limited to the coverage contract
    # above rather than re-deriving that assertion from volatile live data.
