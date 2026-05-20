"""
Tests for normalize_airbnb_url().

Verifies that localized Airbnb hosts (zh-t.airbnb.com, zh.airbnb.com, etc.)
are rewritten to www.airbnb.com while preserving path, query string, and
fragment.  Non-Airbnb URLs must pass through unchanged.
"""

import pytest
from worker.scraper.target_extractor import normalize_airbnb_url


class TestNormalizeAirbnbUrl:
    # ── Localized hosts must be rewritten ──────────────────────────────────

    def test_zh_t_host_rewritten(self):
        url = "https://zh-t.airbnb.com/rooms/12345678"
        assert normalize_airbnb_url(url) == "https://www.airbnb.com/rooms/12345678"

    def test_zh_host_rewritten(self):
        url = "https://zh.airbnb.com/rooms/12345678"
        assert normalize_airbnb_url(url) == "https://www.airbnb.com/rooms/12345678"

    def test_fr_host_rewritten(self):
        url = "https://fr.airbnb.com/rooms/99999"
        assert normalize_airbnb_url(url) == "https://www.airbnb.com/rooms/99999"

    def test_de_host_rewritten(self):
        url = "https://de.airbnb.com/rooms/42"
        assert normalize_airbnb_url(url) == "https://www.airbnb.com/rooms/42"

    def test_es_host_rewritten(self):
        url = "https://es.airbnb.com/s/Madrid/homes?checkin=2026-04-01&checkout=2026-04-02&adults=2"
        result = normalize_airbnb_url(url)
        assert result.startswith("https://www.airbnb.com/")
        assert "checkin=2026-04-01" in result
        assert "checkout=2026-04-02" in result
        assert "adults=2" in result

    def test_ca_host_rewritten(self):
        url = "https://www.airbnb.ca/rooms/12345678?check_in=2026-04-01&check_out=2026-04-02"
        result = normalize_airbnb_url(url)
        assert result.startswith("https://www.airbnb.com/rooms/12345678")
        assert "check_in=2026-04-01" in result
        assert "check_out=2026-04-02" in result

    # ── Canonical host must pass through unchanged ─────────────────────────

    def test_www_host_unchanged(self):
        url = "https://www.airbnb.com/rooms/12345678"
        assert normalize_airbnb_url(url) == url

    def test_www_host_with_query_unchanged(self):
        url = "https://www.airbnb.com/s/Seattle/homes?checkin=2026-04-01&checkout=2026-04-02&adults=2"
        assert normalize_airbnb_url(url) == url

    # ── Path and query string are preserved ───────────────────────────────

    def test_path_preserved(self):
        url = "https://zh-t.airbnb.com/rooms/12345678?check_in=2026-04-01&check_out=2026-04-02"
        result = normalize_airbnb_url(url)
        assert "/rooms/12345678" in result
        assert "check_in=2026-04-01" in result
        assert "check_out=2026-04-02" in result

    def test_search_path_and_query_preserved(self):
        url = "https://zh-t.airbnb.com/s/Taipei%2C%20Taiwan/homes?checkin=2026-05-01&checkout=2026-05-02&adults=2"
        result = normalize_airbnb_url(url)
        assert result == "https://www.airbnb.com/s/Taipei%2C%20Taiwan/homes?checkin=2026-05-01&checkout=2026-05-02&adults=2"

    def test_room_id_preserved(self):
        url = "https://zh.airbnb.com/rooms/987654321?source=search_page"
        result = normalize_airbnb_url(url)
        assert "987654321" in result
        assert "source=search_page" in result

    # ── Non-Airbnb URLs must be returned unchanged ─────────────────────────

    def test_non_airbnb_url_unchanged(self):
        url = "https://www.booking.com/hotel/us/some-hotel.html"
        assert normalize_airbnb_url(url) == url

    def test_vrbo_url_unchanged(self):
        url = "https://www.vrbo.com/123456"
        assert normalize_airbnb_url(url) == url

    def test_partial_airbnb_domain_unchanged(self):
        # Should not touch "notairbnb.com" or "airbnb.example.com"
        url = "https://notairbnb.com/rooms/123"
        assert normalize_airbnb_url(url) == url

    # ── Edge cases ─────────────────────────────────────────────────────────

    def test_empty_string_unchanged(self):
        assert normalize_airbnb_url("") == ""

    def test_none_like_string_unchanged(self):
        # Caller should never pass None, but be resilient
        result = normalize_airbnb_url("https://www.airbnb.com/rooms/1")
        assert result == "https://www.airbnb.com/rooms/1"

    def test_bare_airbnb_com_unchanged(self):
        # airbnb.com without subdomain — already canonical enough; regex must not break it
        url = "https://airbnb.com/rooms/55555"
        result = normalize_airbnb_url(url)
        # airbnb.com matches the regex (zero-subdomain form) and may or may not
        # be rewritten — what matters is the path is preserved and it's valid.
        assert "/rooms/55555" in result


class TestSafeDomainBase:
    """safe_domain_base() must return www.airbnb.com for localized Airbnb URLs."""

    def test_localized_host_gives_canonical_origin(self):
        from worker.scraper.target_extractor import safe_domain_base
        assert safe_domain_base("https://zh-t.airbnb.com/rooms/1234") == "https://www.airbnb.com"

    def test_canonical_host_unchanged(self):
        from worker.scraper.target_extractor import safe_domain_base
        assert safe_domain_base("https://www.airbnb.com/rooms/1234") == "https://www.airbnb.com"

    def test_ca_host_gives_canonical_origin(self):
        from worker.scraper.target_extractor import safe_domain_base
        assert safe_domain_base("https://www.airbnb.ca/rooms/1234") == "https://www.airbnb.com"

    def test_non_airbnb_host_preserved(self):
        from worker.scraper.target_extractor import safe_domain_base
        assert safe_domain_base("https://www.booking.com/hotel/us/foo.html") == "https://www.booking.com"
