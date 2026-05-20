from __future__ import annotations

import json
from pathlib import Path

from worker.scraper.parsers import parse_pdp_response
from worker.scraper.playwright_scraper import PlaywrightScraper


_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_SPARSE_PDP_FIXTURE = _FIXTURES_DIR / "pdp_sparse_1408676238256636483.json"
_RENDERED_HTML_FIXTURE = _FIXTURES_DIR / "pdp_rendered_amenities_1408676238256636483.html"


def test_pdp_amenities_html_fallback_enriches_sparse_payload_e2e() -> None:
    """
    End-to-end regression test for listing 1408676238256636483.

    The captured StaysPdpSections payload includes booking/policy blocks but
    misses AMENITIES_* sections (only 4 amenities parse). The rendered HTML
    still carries full amenities in data.node.pdpPresentation.amenities.
    """
    payload = json.loads(_SPARSE_PDP_FIXTURE.read_text(encoding="utf-8"))
    rendered_html = _RENDERED_HTML_FIXTURE.read_text(encoding="utf-8")

    assert PlaywrightScraper._pdp_payload_has_amenity_groups(payload) is False

    html_amenities = PlaywrightScraper._extract_pdp_amenities_from_rendered_html(rendered_html)
    assert isinstance(html_amenities, dict)

    PlaywrightScraper._inject_pdp_presentation_amenities(payload, html_amenities)
    assert PlaywrightScraper._pdp_payload_has_amenity_groups(payload) is True

    parsed = parse_pdp_response(payload, "1408676238256636483", "https://www.airbnb.com")
    amenities = list(parsed.get("amenities") or [])

    # Prior to fallback this payload parsed to exactly 4 amenities.
    assert len(amenities) > 20
    assert "Guest favorite" in amenities
    assert "Self check-in" in amenities
    assert "Smoke alarm" in amenities
    assert "Carbon monoxide alarm" in amenities
    assert "Wi-Fi" in amenities
