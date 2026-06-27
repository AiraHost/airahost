"""
Hard guarantees for every Airbnb request:
  1. Domain is always www.airbnb.com (no .ca / .com.tw / other ccTLDs).
  2. Language is always English (non-English locales are coerced).
  3. Currency is always USD (non-USD overrides are ignored).
"""

from __future__ import annotations

from worker.scraper.playwright_scraper import PlaywrightScraper
from worker.scraper.target_extractor import normalize_airbnb_url, safe_domain_base


def _scraper(**config) -> PlaywrightScraper:
    cfg = {"CDP_URL": "http://127.0.0.1:9222"}
    cfg.update(config)
    return PlaywrightScraper(cfg)


# ── 1. Domain → always .com ────────────────────────────────────────────────
def test_com_tw_listing_url_normalized_to_com():
    assert (
        normalize_airbnb_url("https://www.airbnb.com.tw/rooms/123?check_in=2026-07-01")
        == "https://www.airbnb.com/rooms/123?check_in=2026-07-01"
    )
    assert safe_domain_base("https://www.airbnb.com.tw/rooms/123") == "https://www.airbnb.com"


def test_other_cctlds_normalized_to_com():
    for host in ("www.airbnb.ca", "airbnb.co.uk", "airbnb.com.au", "zh.airbnb.com"):
        assert normalize_airbnb_url(f"https://{host}/rooms/7") == "https://www.airbnb.com/rooms/7"


def test_non_airbnb_hosts_untouched():
    for url in ("https://notairbnb.com/rooms/1", "https://www.booking.com/x"):
        assert normalize_airbnb_url(url) == url


def test_base_url_forced_to_com():
    assert _scraper(AIRBNB_BASE_URL="https://www.airbnb.com.tw").base_url == "https://www.airbnb.com"
    assert _scraper(AIRBNB_BASE_URL="https://www.airbnb.ca").base_url == "https://www.airbnb.com"


# ── 2. Language → always English ───────────────────────────────────────────
def test_non_english_locale_coerced_to_english():
    assert _scraper(LOCALE="zh-TW").locale.lower().startswith("en")
    assert _scraper(LOCALE="fr-CA").locale.lower().startswith("en")


def test_english_locale_preserved():
    assert _scraper(LOCALE="en-GB").locale == "en-GB"


# ── 3. Currency → always USD ───────────────────────────────────────────────
def test_currency_forced_usd_despite_config():
    assert _scraper(CURRENCY="CAD").currency == "USD"
    assert _scraper(CURRENCY="TWD").currency == "USD"


# ── Requests carry English + USD ───────────────────────────────────────────
def test_pdp_template_pins_usd_and_english():
    s = _scraper(USE_HARDCODED_STAYSPDP_TEMPLATE=True)
    assert s.captured_pdp_req is not None
    url = str(s.captured_pdp_req.get("url") or "")
    assert "currency=USD" in url
    assert "locale=en" in url.lower()
    assert "www.airbnb.com" in url


def test_search_nav_url_pins_english_and_usd():
    url = _scraper()._build_search_navigation_url({"query": "Seattle, WA"})
    assert "currency=USD" in url
    assert "locale=en" in url.lower()
    assert url.startswith("https://www.airbnb.com/")


def test_force_url_query_params_overwrites_and_preserves():
    out = PlaywrightScraper._force_url_query_params(
        "https://www.airbnb.com/api/v3/StaysSearch/abc?currency=CAD&locale=zh-TW&operationName=x",
        currency="USD",
        locale="en-CA",
    )
    assert "currency=USD" in out and "currency=CAD" not in out
    assert "locale=en-CA" in out and "zh-TW" not in out
    assert "operationName=x" in out
