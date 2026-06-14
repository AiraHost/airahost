import logging
import os
import time
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

import requests

from worker.scraper.deepbnb_scraper import DeepBnbScraper
from worker.scraper.playwright_scraper import PlaywrightScraper
from worker.scraper.scraper_errors import ScraperForbiddenError

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class AirbnbClient:
    """Controller that routes scraping requests across isolated scraper strategies."""

    def __init__(self, config: dict):
        self.config = config
        self.base_url = PlaywrightScraper._normalize_base_url(
            self.config.get("AIRBNB_BASE_URL", "https://www.airbnb.com")
        )
        self.guest_favorite_only = bool(
            str(
                self.config.get(
                    "GUEST_FAVORITE_ONLY",
                    os.getenv("AIRBNB_GUEST_FAVORITE_ONLY", "1"),
                )
            ).strip().lower()
            in ("1", "true", "yes", "on")
        )
        self._playwright_scraper: Optional[PlaywrightScraper] = None
        self._deepbnb_session = requests.Session()
        self._deepbnb_disabled_for_task = False

        use_deepbnb_cfg = self.config.get("USE_DEEPBNB_BACKEND", None)
        if use_deepbnb_cfg is None:
            self.use_deepbnb_backend = bool(
                str(os.getenv("AIRBNB_USE_DEEPBNB_BACKEND", "1")).strip().lower() in ("1", "true", "yes", "on")
            )
        else:
            self.use_deepbnb_backend = bool(use_deepbnb_cfg)

        self.deepbnb_scraper: Optional[DeepBnbScraper] = (
            DeepBnbScraper(config=self.config, base_url=self.base_url, session=self._deepbnb_session)
            if self.use_deepbnb_backend
            else None
        )

    @property
    def session(self):
        return self._get_playwright_scraper().session

    def _get_playwright_scraper(self) -> PlaywrightScraper:
        if self._playwright_scraper is None:
            cfg = dict(self.config)
            cfg.setdefault("USE_HARDCODED_STAYSPDP_TEMPLATE", False)
            self._playwright_scraper = PlaywrightScraper(cfg)
        return self._playwright_scraper

    def sync_fetch_session_cookies_from_playwright(self) -> None:
        """Disabled: do not replicate cookies from Playwright into Deepbnb session."""
        logger.info("Skipping sync_fetch_session_cookies_from_playwright (disabled).")

    def refresh_session(self, force_capture: bool = False, bypass_cooldown: bool = False):
        return self._get_playwright_scraper().refresh_session(force_capture=force_capture, bypass_cooldown=bypass_cooldown)

    @property
    def cdp_url(self) -> str:
        raw = self.config.get("CDP_URL")
        if raw is None:
            raw = os.getenv("CDP_URL", "http://127.0.0.1:9222")
        value = str(raw or "").strip()
        return value or "http://127.0.0.1:9222"

    def ensure_browser_ready(self) -> None:
        self._get_playwright_scraper().ensure_browser_ready()

    def close_browser(self) -> None:
        if self._playwright_scraper is None:
            return
        try:
            self._playwright_scraper.close_browser()
        except Exception:
            pass

    def fork(self) -> "AirbnbClient":
        clone = AirbnbClient.__new__(AirbnbClient)
        clone.config = dict(self.config)
        clone.base_url = self.base_url
        clone.guest_favorite_only = self.guest_favorite_only
        clone._playwright_scraper = self._playwright_scraper.fork() if self._playwright_scraper is not None else None
        clone._deepbnb_session = requests.Session()
        clone._deepbnb_disabled_for_task = self._deepbnb_disabled_for_task
        if clone._playwright_scraper is not None:
            for cookie in clone._playwright_scraper.session.cookies:
                clone._deepbnb_session.cookies.set(
                    cookie.name,
                    cookie.value,
                    domain=cookie.domain,
                    path=cookie.path,
                    secure=cookie.secure,
                    expires=cookie.expires,
                )
        clone.use_deepbnb_backend = self.use_deepbnb_backend
        clone.deepbnb_scraper = (
            DeepBnbScraper(config=clone.config, base_url=clone.base_url, session=clone._deepbnb_session)
            if clone.use_deepbnb_backend
            else None
        )
        return clone

    def _search_via_playwright(self, overrides: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        scraper = self._get_playwright_scraper()
        if overrides is None:
            return scraper.search_listings()
        return scraper.search_listings_with_overrides(overrides)

    def browse_url_html(
        self,
        url: str,
        *,
        label: str = "generic_browser_nav",
        wait_until: str = "commit",
        timeout: int = 30000,
    ) -> Dict[str, Any]:
        return self._get_playwright_scraper().browse_url_html(
            url=url,
            label=label,
            wait_until=wait_until,
            timeout=timeout,
        )

    @staticmethod
    def _looks_challenge_exception(exc: Exception) -> bool:
        text = str(exc or "").strip().lower()
        if not text:
            return False
        markers = (
            "challenge",
            "captcha",
            "checkpoint",
            "forbidden",
            "blocked",
            "security",
            "unauth",
            "403",
            "login required",
            "verify",
            "persistedquerynotfound",
            "persisted query not found",
            "not found",
        )
        return any(marker in text for marker in markers)

    def _run_deepbnb_with_fallback(
        self,
        op_name: str,
        deepbnb_call: Callable[[], _T],
        fallback_call: Callable[[], _T],
    ) -> _T:
        if self.deepbnb_scraper is None:
            return fallback_call()
        if self._deepbnb_disabled_for_task:
            logger.info(
                "DeepBnbScraper disabled for current task after challenge; using Playwright for %s",
                op_name,
            )
            return fallback_call()

        last_exc: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                return deepbnb_call()
            except ScraperForbiddenError as exc:
                self._deepbnb_disabled_for_task = True
                logger.error(
                    "DeepBnbScraper blocked/challenge for %s; immediate fallback to Playwright: %s",
                    op_name,
                    exc,
                )
                return fallback_call()
            except Exception as exc:
                last_exc = exc
                if self._looks_challenge_exception(exc):
                    self._deepbnb_disabled_for_task = True
                    logger.error(
                        "DeepBnbScraper challenge-like error for %s; immediate fallback to Playwright: %s",
                        op_name,
                        exc,
                    )
                    return fallback_call()
                logger.warning(
                    "DeepBnbScraper %s attempt %s/2 failed; retrying/fallbacking: %s",
                    op_name,
                    attempt,
                    exc,
                )
                if attempt < 2:
                    time.sleep(0.8)
        logger.warning("DeepBnbScraper %s failed after 2 attempts; falling back to Playwright: %s", op_name, last_exc)
        return fallback_call()

    def search_listings(self) -> Tuple[int, Dict[str, Any]]:
        # Default non-daily path: browser Playwright search.
        return self._search_via_playwright()

    def search_listings_with_overrides(
        self,
        overrides: Dict[str, Any],
    ) -> Tuple[int, Dict[str, Any]]:
        # Deepbnb is allowed only for explicit daily-search calls.
        is_daily_search = bool((overrides or {}).get("dailySearch"))
        if not is_daily_search:
            return self._search_via_playwright(overrides)
        return self._run_deepbnb_with_fallback(
            op_name="search_listings_with_overrides",
            deepbnb_call=lambda: self.deepbnb_scraper.search_listings_with_overrides(overrides),  # type: ignore[union-attr]
            fallback_call=lambda: self._search_via_playwright(overrides),
        )

    def get_listing_details(
        self,
        listing_id: str,
        checkin: Optional[str] = None,
        checkout: Optional[str] = None,
        adults: Optional[int] = None,
    ) -> Dict[str, Any]:
        effective_checkin = checkin or self.config.get("CHECKIN", "")
        effective_checkout = checkout or self.config.get("CHECKOUT", "")
        effective_adults = int(adults if adults is not None else self.config.get("ADULTS", 1))

        # Self-listing / PDP details should always use browser-based Playwright.
        # Deepbnb is restricted to daily search endpoints only.
        return self._get_playwright_scraper().get_listing_details(
            listing_id=str(listing_id),
            checkin=effective_checkin,
            checkout=effective_checkout,
            adults=effective_adults,
        )
