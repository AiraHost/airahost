import logging
from typing import Any, Dict, Optional, Tuple

from worker.scraper.playwright_scraper import PlaywrightScraper

logger = logging.getLogger(__name__)


class AirbnbClient:
    """Routes scraping requests through Playwright (Chrome CDP)."""

    def __init__(self, config: dict):
        self.config = config
        self.base_url = PlaywrightScraper._normalize_base_url(
            self.config.get("AIRBNB_BASE_URL", "https://www.airbnb.com")
        )
        self._playwright_scraper: Optional[PlaywrightScraper] = None

    @property
    def session(self):
        return self._get_playwright_scraper().session

    def _get_playwright_scraper(self) -> PlaywrightScraper:
        if self._playwright_scraper is None:
            self._playwright_scraper = PlaywrightScraper(self.config)
        return self._playwright_scraper

    def sync_fetch_session_cookies_from_playwright(self) -> None:
        pass

    def refresh_session(self, force_capture: bool = False, bypass_cooldown: bool = False):
        return self._get_playwright_scraper().refresh_session(
            force_capture=force_capture, bypass_cooldown=bypass_cooldown
        )

    @property
    def cdp_url(self) -> str:
        import os
        raw = self.config.get("CDP_URL")
        if raw is None:
            raw = os.getenv("CDP_URL", "http://127.0.0.1:9222")
        return str(raw or "").strip() or "http://127.0.0.1:9222"

    def ensure_browser_ready(self) -> None:
        self._get_playwright_scraper().ensure_browser_ready()

    def close_browser(self) -> None:
        if self._playwright_scraper is None:
            return
        try:
            self._playwright_scraper.close_browser()
        except Exception:
            pass

    def close_extra_tabs(self) -> None:
        """Close all browser tabs except one after a scraping task completes."""
        if self._playwright_scraper is None:
            return
        try:
            self._playwright_scraper.close_extra_tabs()
        except Exception:
            pass

    def copy_session_state_from(self, source: "AirbnbClient") -> None:
        """Copy session cookies and PDP template from source client to this one."""
        src = source._playwright_scraper
        if src is None:
            return
        self._get_playwright_scraper().copy_session_from(src)

    def fork(self) -> "AirbnbClient":
        clone = AirbnbClient.__new__(AirbnbClient)
        clone.config = dict(self.config)
        clone.base_url = self.base_url
        clone._playwright_scraper = (
            self._playwright_scraper.fork() if self._playwright_scraper is not None else None
        )
        return clone

    def browse_url_html(
        self,
        url: str,
        *,
        label: str = "generic_browser_nav",
        wait_until: str = "commit",
        timeout: int = 30000,
    ) -> Dict[str, Any]:
        return self._get_playwright_scraper().browse_url_html(
            url=url, label=label, wait_until=wait_until, timeout=timeout
        )

    def search_listings(self) -> Tuple[int, Dict[str, Any]]:
        return self._get_playwright_scraper().search_listings()

    def search_listings_with_overrides(
        self, overrides: Dict[str, Any]
    ) -> Tuple[int, Dict[str, Any]]:
        return self._get_playwright_scraper().search_listings_with_overrides(overrides)

    def search_listings_direct_only(
        self, overrides: Dict[str, Any]
    ) -> Optional[Tuple[int, Dict[str, Any]]]:
        """StaysSearch via direct HTTP replay only — never the browser.

        Unlike search_listings_with_overrides, a legitimately empty result page
        is returned as-is instead of triggering a multi-second browser
        navigation. Returns None when the direct replay itself failed.
        """
        return self._get_playwright_scraper().fetch_search_direct(overrides)

    def get_listing_details(
        self,
        listing_id: str,
        checkin: Optional[str] = None,
        checkout: Optional[str] = None,
        adults: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._get_playwright_scraper().get_listing_details(
            listing_id=str(listing_id),
            checkin=checkin or self.config.get("CHECKIN", ""),
            checkout=checkout or self.config.get("CHECKOUT", ""),
            adults=int(adults if adults is not None else self.config.get("ADULTS", 1)),
        )

    def get_listing_details_fast(
        self,
        listing_id: str,
        checkin: Optional[str] = None,
        checkout: Optional[str] = None,
        adults: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Metadata-oriented PDP fetch: direct HTTP replay first, browser fallback."""
        return self._get_playwright_scraper().get_listing_details_fast(
            listing_id=str(listing_id),
            checkin=checkin or self.config.get("CHECKIN", ""),
            checkout=checkout or self.config.get("CHECKOUT", ""),
            adults=int(adults if adults is not None else self.config.get("ADULTS", 1)),
        )

    def fetch_listing_page_html(self, listing_id: str) -> Optional[str]:
        """Direct HTTP GET of the listing page's server-rendered HTML (~1s, no browser tab)."""
        return self._get_playwright_scraper()._fetch_listing_page_html_direct(str(listing_id))

    def fetch_pdp_price_direct(
        self,
        listing_id: str,
        checkin: str,
        checkout: str,
        adults: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """Direct HTTP PDP price fetch — no browser tab, ~300ms vs 5-15s browser nav."""
        scraper = self._get_playwright_scraper()
        fetcher = getattr(scraper, "fetch_pdp_price_direct", None)
        if fetcher is None:
            return None
        return fetcher(listing_id=str(listing_id), checkin=checkin, checkout=checkout, adults=adults)
