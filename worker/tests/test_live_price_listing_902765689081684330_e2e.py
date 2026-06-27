from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import pytest
from playwright.sync_api import sync_playwright

from worker.scraper.airbnb_client import AirbnbClient
from worker.scraper.parsers import parse_pdp_response
from worker.scraper.target_extractor import (
    _BOOKING_WIDGET_PRICE_JS,
    capture_target_live_price,
    select_nightly_price_from_candidates,
)


DEFAULT_LISTING_ID = "902765689081684330"
_BOOK_IT_SECTION_IDS = ("BOOK_IT_FLOATING_FOOTER", "BOOK_IT_SIDEBAR", "BOOK_IT_NAV")
_NON_USD_CURRENCY_CODES = {
    "CAD",
    "AUD",
    "NZD",
    "EUR",
    "GBP",
    "JPY",
    "INR",
    "MXN",
    "BRL",
    "CHF",
}


def _default_checkin_checkout() -> tuple[str, str]:
    start = date.today() + timedelta(days=21)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _resolve_checkin_checkout() -> tuple[str, str]:
    default_checkin, default_checkout = _default_checkin_checkout()
    checkin = str(os.getenv("AIRBNB_LIVE_CHECKIN", default_checkin)).strip()
    checkout = str(os.getenv("AIRBNB_LIVE_CHECKOUT", default_checkout)).strip()
    return checkin, checkout


def _resolve_listing_id() -> str:
    return str(os.getenv("AIRBNB_LIVE_LISTING_ID", DEFAULT_LISTING_ID)).strip()


def _resolve_adults() -> int:
    raw = str(os.getenv("AIRBNB_LIVE_ADULTS", "1")).strip()
    try:
        adults = int(raw)
    except Exception as exc:
        raise AssertionError(f"AIRBNB_LIVE_ADULTS must be an integer, got {raw!r}") from exc
    assert adults > 0, f"AIRBNB_LIVE_ADULTS must be >= 1, got {adults}"
    return adults


def _amount_from_text(text: Optional[str]) -> Optional[float]:
    if not isinstance(text, str):
        return None
    m = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text.replace("\xa0", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None


def _extract_booking_primary_lines(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    sections = (
        (((payload or {}).get("data") or {}).get("presentation") or {})
        .get("stayProductDetailPage", {})
        .get("sections", {})
        .get("sections", [])
    )
    out: List[Dict[str, Any]] = []
    if not isinstance(sections, list):
        return out
    for entry in sections:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("sectionId") or "")
        if sid not in _BOOK_IT_SECTION_IDS:
            continue
        sec = entry.get("section") if isinstance(entry.get("section"), dict) else {}
        primary = (
            (sec.get("structuredDisplayPrice") or {}).get("primaryLine")
            if isinstance(sec.get("structuredDisplayPrice"), dict)
            else None
        )
        if isinstance(primary, dict):
            out.append({"sectionId": sid, "primaryLine": primary})
    return out


def _collect_price_texts(primary_lines: List[Dict[str, Any]]) -> List[str]:
    texts: List[str] = []
    for row in primary_lines:
        primary = row.get("primaryLine") if isinstance(row.get("primaryLine"), dict) else {}
        for key in ("price", "discountedPrice", "originalPrice", "accessibilityLabel"):
            value = primary.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
    return texts


def _listing_url(listing_id: str) -> str:
    return f"https://www.airbnb.com/rooms/{listing_id}"


def _build_listing_url_with_dates(listing_id: str, checkin: str, checkout: str, adults: int) -> str:
    return f"{_listing_url(listing_id)}?{urlencode({'check_in': checkin, 'check_out': checkout, 'adults': adults, 'guests': adults})}"


def test_live_e2e_listing_902765689081684330_usd_discount_price_is_selected():
    """
    Live E2E guard for listing 902765689081684330.

    Uses the same nightly live-price scraper path:
      1) Fetch raw PDP payload via AirbnbClient.get_listing_details (Playwright scraper backend)
      2) Extract price via capture_target_live_price
      3) Capture a screenshot + DOM candidates for manual verification artifacts

    Opt-in:
      RUN_AIRBNB_LIVE_PRICE_E2E=1
      CDP_URL=http://127.0.0.1:9222
      AIRBNB_LIVE_LISTING_ID=902765689081684330  (optional override)
      AIRBNB_LIVE_CHECKIN=YYYY-MM-DD          (optional)
      AIRBNB_LIVE_CHECKOUT=YYYY-MM-DD         (optional)
      AIRBNB_LIVE_ADULTS=N                    (optional, default 1)
      AIRBNB_LIVE_ARTIFACT_DIR=...            (optional, default worker/outputs/live_price_e2e)
      AIRBNB_LIVE_EXPECT_DISCOUNT=1|0         (optional, default 1)
    """
    if os.getenv("RUN_AIRBNB_LIVE_PRICE_E2E") != "1":
        pytest.skip("Set RUN_AIRBNB_LIVE_PRICE_E2E=1 to run live price E2E.")

    cdp_url = str(os.getenv("CDP_URL", "http://127.0.0.1:9222")).strip()
    listing_id = _resolve_listing_id()
    checkin, checkout = _resolve_checkin_checkout()
    adults = _resolve_adults()
    expect_discount = str(os.getenv("AIRBNB_LIVE_EXPECT_DISCOUNT", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    artifact_dir = Path(
        str(os.getenv("AIRBNB_LIVE_ARTIFACT_DIR", "worker/outputs/live_price_e2e")).strip()
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{listing_id}_{checkin}_{checkout}_{adults}guests"
    payload_path = artifact_dir / f"{stem}_raw_pdp.json"
    dom_path = artifact_dir / f"{stem}_dom_candidates.json"
    screenshot_path = artifact_dir / f"{stem}_page.png"
    html_path = artifact_dir / f"{stem}_page.html"
    summary_path = artifact_dir / f"{stem}_summary.json"

    client = AirbnbClient(
        {
            "CHECKIN": checkin,
            "CHECKOUT": checkout,
            "ADULTS": adults,
            "CDP_URL": cdp_url,
            "CURRENCY": "USD",
            "LOCALE": "en-US",
        }
    )

    payload = client.get_listing_details(
        listing_id,
        checkin=checkin,
        checkout=checkout,
        adults=adults,
    )
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    listing_url_with_dates = _build_listing_url_with_dates(listing_id, checkin, checkout, adults)
    dom_result: Dict[str, Any] = {}
    created_context = None
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url, timeout=20000)
        had_contexts = bool(browser.contexts)
        context = browser.contexts[0] if had_contexts else browser.new_context(
            locale="en-US",
            timezone_id="America/Los_Angeles",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        if not had_contexts:
            created_context = context
        page = context.new_page()
        try:
            page.goto(listing_url_with_dates, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)
            dom_eval = page.evaluate(_BOOKING_WIDGET_PRICE_JS)
            dom_result = dom_eval if isinstance(dom_eval, dict) else {}
            page.screenshot(path=str(screenshot_path), full_page=True)
            html_path.write_text(page.content(), encoding="utf-8")
        finally:
            try:
                page.close()
            except Exception:
                pass
            if created_context is not None:
                try:
                    created_context.close()
                except Exception:
                    pass

    dom_path.write_text(json.dumps(dom_result, ensure_ascii=False, indent=2), encoding="utf-8")

    parsed = parse_pdp_response(payload, listing_id, "https://www.airbnb.com")
    parsed_currency = str(parsed.get("currency") or "").upper()

    primary_lines = _extract_booking_primary_lines(payload)
    assert primary_lines, (
        "No BOOK_IT primary lines found. "
        f"Raw payload: {payload_path} Screenshot: {screenshot_path} DOM: {dom_path}"
    )

    price_texts = _collect_price_texts(primary_lines)
    if not price_texts:
        widget_text = str(dom_result.get("widgetText") or "").strip()
        pytest.fail(
            "No BOOK_IT price texts found in raw payload for this date window. "
            "The listing may be unavailable or constrained by min-stay for this date. "
            "Set AIRBNB_LIVE_CHECKIN/AIRBNB_LIVE_CHECKOUT to a known bookable discounted window. "
            f"Raw payload: {payload_path} Screenshot: {screenshot_path} DOM: {dom_path} "
            f"Widget text: {widget_text!r}"
        )

    assert parsed_currency == "USD", (
        f"Expected parsed currency USD, got {parsed_currency!r}. "
        f"Raw payload: {payload_path} Screenshot: {screenshot_path} DOM: {dom_path}"
    )

    found_non_usd = []
    for text in price_texts:
        for token in re.findall(r"\b[A-Z]{3}\b", text.upper()):
            if token in _NON_USD_CURRENCY_CODES:
                found_non_usd.append((token, text))
    assert not found_non_usd, (
        f"Found non-USD currency markers in BOOK_IT price text: {found_non_usd}. "
        f"Raw payload: {payload_path}"
    )

    live = capture_target_live_price(
        listing_url=_listing_url(listing_id),
        checkin=checkin,
        checkout=checkout,
        cdp_url=cdp_url,
        client=client,
        allow_retry_matrix=False,
        adults=adults,
    )
    assert live.get("livePriceStatus") == "captured", (
        f"Live capture failed: status={live.get('livePriceStatus')} "
        f"reason={live.get('livePriceStatusReason')}"
    )
    observed_price = live.get("observedListingPrice")
    assert isinstance(observed_price, (int, float)) and observed_price > 0, (
        f"Observed price is invalid: {observed_price!r}"
    )
    observed_price = float(observed_price)

    candidates = dom_result.get("candidates") if isinstance(dom_result.get("candidates"), list) else []
    assert candidates, (
        "No DOM price candidates found in booking widget. "
        f"Screenshot: {screenshot_path} DOM: {dom_path} HTML: {html_path}"
    )
    selected = select_nightly_price_from_candidates(candidates)
    assert selected is not None, (
        "Could not select nightly price from DOM candidates. "
        f"DOM: {dom_path} Screenshot: {screenshot_path}"
    )
    selected_price, selected_kind = selected

    has_strikethrough = any(bool(c.get("strikethrough")) for c in candidates if isinstance(c, dict))
    if expect_discount:
        assert has_strikethrough, (
            "Expected discount evidence (strikethrough price) but none found. "
            f"DOM: {dom_path} Screenshot: {screenshot_path}"
        )
        assert selected_kind == "nightly_discounted", (
            f"Expected selected kind nightly_discounted, got {selected_kind!r}. DOM: {dom_path}"
        )

    assert abs(observed_price - round(float(selected_price))) <= 1.0, (
        f"Observed price {observed_price} does not match DOM-selected nightly price {selected_price}. "
        f"DOM: {dom_path} Screenshot: {screenshot_path}"
    )

    discounted_amount = None
    original_amount = None
    for row in primary_lines:
        primary = row.get("primaryLine") if isinstance(row.get("primaryLine"), dict) else {}
        if discounted_amount is None:
            discounted_amount = _amount_from_text(primary.get("discountedPrice"))
        if original_amount is None:
            original_amount = _amount_from_text(primary.get("originalPrice") or primary.get("price"))
    if discounted_amount is not None and original_amount is not None and expect_discount:
        assert discounted_amount <= original_amount, (
            f"discountedPrice {discounted_amount} should be <= original/price {original_amount}. "
            f"Raw payload: {payload_path}"
        )
        assert abs(observed_price - round(discounted_amount)) <= 1.0, (
            f"Observed price {observed_price} does not match discounted price {discounted_amount}. "
            f"Raw payload: {payload_path} Screenshot: {screenshot_path}"
        )

    summary = {
        "listing_id": listing_id,
        "checkin": checkin,
        "checkout": checkout,
        "adults": adults,
        "listing_url_with_dates": listing_url_with_dates,
        "live_result": live,
        "parsed_price": {
            "nightly_price": parsed.get("nightly_price"),
            "total_price": parsed.get("total_price"),
            "currency": parsed_currency,
        },
        "has_strikethrough": has_strikethrough,
        "selected_dom_price": {"price": selected_price, "kind": selected_kind},
        "artifacts": {
            "raw_pdp": str(payload_path),
            "dom_candidates": str(dom_path),
            "screenshot": str(screenshot_path),
            "page_html": str(html_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
