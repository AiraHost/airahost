from __future__ import annotations

from worker.scraper.playwright_scraper import PlaywrightScraper


def test_rendered_html_unavailable_dates_marker_blocks_stale_price_fallback() -> None:
    """
    Airbnb can leave stale price-like text in the hydrated page while the booking
    widget says the requested dates are unavailable. That banner must make the
    scraper return no price instead of trusting the stale amount.
    """
    rendered_html = """
    <html>
      <body>
        <div>Those dates are not available</div>
        <span>$321 CAD</span>
      </body>
    </html>
    """

    assert PlaywrightScraper._rendered_html_dates_unavailable(rendered_html) is True


def test_rendered_html_without_unavailable_dates_marker_can_try_price_fallback() -> None:
    rendered_html = "<html><body><span>$321 CAD</span></body></html>"

    assert PlaywrightScraper._rendered_html_dates_unavailable(rendered_html) is False
