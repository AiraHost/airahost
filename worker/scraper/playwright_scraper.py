import base64
import asyncio
import copy
import json
import logging
import os
import random
import re
import string
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlparse, urlunparse

import requests
from playwright.async_api import async_playwright
from worker.core.concurrent_runner import MAX_SCRAPER_WORKERS
from worker.core.rate_limiter import get_airbnb_rate_limiter
from worker.scraper.airbnb_pdp_api.airbnb_crawler import (
    AirbnbPdpClient,
    AntiBotError as PdpApiAntiBotError,
    StaleHashError as PdpApiStaleHashError,
)
from worker.scraper.stayspdp_template import HARDCODED_STAYS_PDP_TEMPLATE
from worker.scraper.stayssearch_template import HARDCODED_STAYS_SEARCH_TEMPLATE

# .../api/v3/StaysPdpSections/<64-hex-hash>?... — used to opportunistically
# refresh the airbnb_pdp_api persisted-query hash (in-memory only) whenever a
# real browser PDP navigation happens to run anyway, so the standalone client
# self-heals without ever spawning its own browser.
_PDP_API_HASH_IN_URL_RE = re.compile(r"/api/v3/StaysPdpSections/([0-9a-f]{64})")

logger = logging.getLogger(__name__)

# Airbnb's public web API key — the same value the browser sends on every
# GraphQL call. Mirrors the stl-scraper technique (github.com/JoeBashe/stl-scraper):
# authenticate StaysSearch over plain HTTP with this public api-key header
# instead of relying on a captured browser tab / session headers.
PUBLIC_AIRBNB_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"


class AirbnbRateLimited(RuntimeError):
    """Raised when Airbnb responds with a rate-limit status (503/429)."""


def _is_rate_limited_status(status: Any) -> bool:
    try:
        return int(status) in (429, 503)
    except (TypeError, ValueError):
        return False


class PlaywrightScraper:
    """Playwright capture/replay strategy using Chrome CDP."""
    _refresh_lock = threading.Lock()
    # Canonical = any subdomain of airbnb.com (NOT .ca/.com.tw/etc.), so a
    # redirect that lands on a non-.com host is detected as non-canonical and
    # retried back onto .com.
    _CANONICAL_AIRBNB_HOST_RE = re.compile(
        r"^(?:[a-z0-9-]+\.)*airbnb\.com$",
        re.IGNORECASE,
    )

    def __init__(self, config: dict):
        self.config = config
        self.base_url = self._normalize_base_url(self.config.get("AIRBNB_BASE_URL", "https://www.airbnb.com"))
        # Hard requirements: Airbnb content must always be English and prices
        # always USD, regardless of config/env. Any non-English locale is coerced
        # to English; currency is pinned to USD outright.
        _configured_locale = str(
            self.config.get("LOCALE", os.getenv("AIRBNB_LOCALE", "en-CA")) or "en-CA"
        ).strip()
        self.locale = _configured_locale if _configured_locale.lower().startswith("en") else "en-CA"
        self.currency = "USD"
        self.session = requests.Session()
        # Direct-HTTP replays must carry the logged-in CDP browser's cookies —
        # Airbnb returns different prices to logged-out sessions. Warmed lazily
        # on first direct fetch (see _ensure_session_cookies_from_browser).
        self._session_cookies_warmed = False
        self.captured_search_req = None
        self.captured_pdp_req = None
        # Standalone airbnb_pdp_api client (public API key, no CDP browser
        # required) tried before the captured-template replay / browser
        # fallback below. Lazily constructed; hash override is refreshed
        # in-memory whenever a real browser PDP navigation reveals a newer
        # persisted-query hash (see _get_listing_details_via_browser).
        self._pdp_api_client: Optional[AirbnbPdpClient] = None
        self._pdp_api_hash_override: Optional[str] = None
        self.hardcoded_search_req: Optional[Dict[str, Any]] = None
        disable_map_cfg = self.config.get("DISABLE_MAP_SEARCH", None)
        if disable_map_cfg is None:
            self.disable_map_search = bool(
                str(os.getenv("AIRBNB_DISABLE_MAP_SEARCH", "0")).strip().lower() in ("1", "true", "yes", "on")
            )
        else:
            self.disable_map_search = bool(disable_map_cfg)
        enable_ai_cfg = self.config.get("ENABLE_AI_SEARCH", None)
        if enable_ai_cfg is None:
            self.enable_ai_search = bool(
                str(os.getenv("AIRBNB_ENABLE_AI_SEARCH", "0")).strip().lower() in ("1", "true", "yes", "on")
            )
        else:
            self.enable_ai_search = bool(enable_ai_cfg)
        self.cache_path = self.config.get("SESSION_CACHE_PATH", ".airbnb_session_cache.json")
        self.session_max_age_seconds = int(self.config.get("SESSION_MAX_AGE_SECONDS", 6 * 60 * 60))
        self.refresh_cooldown_seconds = int(self.config.get("SESSION_REFRESH_COOLDOWN_SECONDS", 45))
        self._last_refresh_started_at = 0.0
        refresh_each_cfg = self.config.get("REFRESH_SESSION_BEFORE_EACH_SEARCH", None)
        if refresh_each_cfg is None:
            self.refresh_before_each_search = bool(
                str(os.getenv("AIRBNB_REFRESH_SESSION_BEFORE_EACH_SEARCH", "0")).strip().lower() in ("1", "true", "yes", "on")
            )
        else:
            self.refresh_before_each_search = bool(refresh_each_cfg)
        hardcoded_pdp_cfg = self.config.get("USE_HARDCODED_STAYSPDP_TEMPLATE", None)
        if hardcoded_pdp_cfg is None:
            self.use_hardcoded_stayspdp_template = bool(
                str(os.getenv("AIRBNB_USE_HARDCODED_STAYSPDP_TEMPLATE", "1")).strip().lower()
                not in ("0", "false", "no", "off")
            )
        else:
            self.use_hardcoded_stayspdp_template = bool(hardcoded_pdp_cfg)
        hardcoded_search_cfg = self.config.get("USE_HARDCODED_STAYSSEARCH_TEMPLATE", None)
        if hardcoded_search_cfg is None:
            self.use_hardcoded_stayssearch_template = bool(
                str(os.getenv("AIRBNB_USE_HARDCODED_STAYSSEARCH_TEMPLATE", "1")).strip().lower()
                not in ("0", "false", "no", "off")
            )
        else:
            self.use_hardcoded_stayssearch_template = bool(hardcoded_search_cfg)
        # Cache unresolved PDP booking windows to avoid repeated expensive
        # template recaptures when Airbnb consistently returns NOT_COMPLETE.
        self._pdp_unresolved_windows: Dict[str, float] = {}
        self._browser_init_lock = threading.Lock()
        self._session_cookie_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._runtime_owner_tid: Optional[int] = None
        self._cdp_url = str(
            self.config.get("CDP_URL")
            or os.getenv("CDP_URL", "http://127.0.0.1:9222")
        ).strip()
        self._uses_external_browser = True
        self._pw = None
        self._browser = None
        self._context = None
        self._context_owned = False
        self._tab_gate_lock = threading.Lock()
        self._tab_gate = None
        self._tab_limit = 8
        self._open_tab_count = 0
        self._ensure_tab_gate()
        if self.use_hardcoded_stayspdp_template:
            self._load_hardcoded_stayspdp_template()
        if self.use_hardcoded_stayssearch_template:
            self._load_hardcoded_stayssearch_template()

    @staticmethod
    def _normalize_base_url(raw_base: Any) -> str:
        raw = str(raw_base or "").strip()
        if not raw:
            return "https://www.airbnb.com"
        try:
            parsed = urlparse(raw)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                host = str(parsed.hostname or "").strip().lower()
                # Any Airbnb host (any ccTLD / localized subdomain) collapses to
                # the canonical .com so every search/PDP request stays on .com.
                if re.fullmatch(r"(?:[a-z0-9-]+\.)*airbnb\.[a-z]{2,3}(?:\.[a-z]{2,3})?", host):
                    return "https://www.airbnb.com"
                return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        except Exception:
            pass
        return "https://www.airbnb.com"

    @classmethod
    def _is_canonical_airbnb_url(cls, raw_url: str) -> bool:
        try:
            host = str(urlparse(str(raw_url or "")).hostname or "").strip().lower()
            return bool(host and cls._CANONICAL_AIRBNB_HOST_RE.fullmatch(host))
        except Exception:
            return False

    def _build_pdp_listing_url(self, listing_id: str, checkin: str, checkout: str, adults: int) -> str:
        params = {
            "check_in": checkin,
            "check_out": checkout,
            "guests": adults,
            "adults": adults,
            "locale": self.locale,
            "currency": self.currency,
        }
        return f"{self.base_url}/rooms/{listing_id}?{urlencode(params)}"

    def _ensure_tab_gate(self) -> None:
        with self._tab_gate_lock:
            if self._tab_gate is not None:
                return
            # Default tabs-per-browser to the configured worker cap so tab
            # concurrency matches DAY_QUERY/MAX_SCRAPER_WORKERS instead of
            # bottlenecking below it; AIRBNB_PLAYWRIGHT_MAX_TABS still overrides.
            raw_limit = os.getenv("AIRBNB_PLAYWRIGHT_MAX_TABS", str(MAX_SCRAPER_WORKERS))
            try:
                parsed_limit = int(str(raw_limit).strip())
            except Exception:
                parsed_limit = MAX_SCRAPER_WORKERS
            # Hard safety ceiling: never allow more than 8 concurrent tabs per
            # browser, to limit Airbnb anti-bot challenges under heavy load.
            self._tab_limit = max(1, min(parsed_limit, 8))
            self._tab_gate = threading.BoundedSemaphore(self._tab_limit)

    async def _acquire_tab_slot(self, timeout_seconds: float = 120.0) -> None:
        self._ensure_tab_gate()
        assert self._tab_gate is not None
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self._tab_gate.acquire(blocking=False):
                with self._tab_gate_lock:
                    self._open_tab_count += 1
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Timed out waiting for Playwright tab slot (limit={self._tab_limit})"
                )
            await asyncio.sleep(0.05)

    def _release_tab_slot(self) -> None:
        with self._tab_gate_lock:
            self._open_tab_count = max(0, self._open_tab_count - 1)
        if self._tab_gate is not None:
            try:
                self._tab_gate.release()
            except Exception:
                pass

    def _ensure_async_loop(self) -> asyncio.AbstractEventLoop:
        with self._browser_init_lock:
            if self._loop is not None and self._loop_thread is not None and self._loop_thread.is_alive():
                return self._loop
            self._loop_ready.clear()
            self._loop_thread = threading.Thread(
                target=self._run_loop_forever,
                name="playwright-async-runtime",
                daemon=True,
            )
            self._loop_thread.start()
            if not self._loop_ready.wait(timeout=10.0):
                raise RuntimeError("Timed out starting Playwright async runtime")
            if self._loop is None:
                raise RuntimeError("Playwright async runtime failed to initialize")
            return self._loop

    def _run_loop_forever(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._runtime_owner_tid = threading.get_ident()
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

    def _run_async(self, coro, *, op_name: str, timeout_seconds: Optional[float] = None):
        loop = self._ensure_async_loop()
        if threading.get_ident() == self._runtime_owner_tid:
            raise RuntimeError(f"Playwright {op_name} called from runtime loop thread")
        if callable(coro):
            coro = coro()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        if isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0:
            return future.result(timeout=float(timeout_seconds))
        return future.result()

    @staticmethod
    def _is_recoverable_browser_failure(exc: BaseException) -> bool:
        messages: List[str] = []
        seen: set[int] = set()
        current: Optional[BaseException] = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            messages.append(str(current or "").lower())
            next_exc = current.__cause__ or current.__context__
            current = next_exc if isinstance(next_exc, BaseException) else None
        error_text = " | ".join(messages)
        markers = (
            "target page, context or browser has been closed",
            "target closed",
            "browser has been closed",
            "browser closed",
            "browser disconnected",
            "connection closed while reading from the driver",
            "failed to open a new tab",
            "page crashed",
            "target crashed",
        )
        return any(marker in error_text for marker in markers)

    def _reset_browser_connection_after_failure(self, exc: BaseException, *, op_name: str) -> bool:
        if not self._is_recoverable_browser_failure(exc):
            return False
        logger.warning(
            "Playwright browser became unavailable during %s; resetting CDP session before one retry: %s",
            op_name,
            exc,
        )
        try:
            self._run_async(
                self._close_browser_async(),
                op_name=f"{op_name}_browser_reset",
                timeout_seconds=20,
            )
        except Exception as reset_exc:
            logger.warning("Playwright CDP session reset failed during %s: %s", op_name, reset_exc)
            self._context = None
            self._context_owned = False
            self._browser = None
            self._pw = None
        return True

    async def _open_capped_page(self, context):
        await self._acquire_tab_slot()
        try:
            page = await context.new_page()
            try:
                await page.bring_to_front()
                logger.info(
                    "Playwright tab brought to front [thread=%s]",
                    threading.get_ident(),
                )
            except Exception as exc:
                logger.warning(
                    "Playwright failed to bring tab to front [thread=%s]: %s",
                    threading.get_ident(),
                    exc,
                )
            try:
                logger.info(
                    "Playwright new_page opened [thread=%s] initial_url=%s open_tabs=%s/%s",
                    threading.get_ident(),
                    str(getattr(page, "url", "") or ""),
                    self._open_tab_count,
                    self._tab_limit,
                )
            except Exception:
                pass
            return page
        except Exception:
            self._release_tab_slot()
            raise

    async def _close_capped_page(self, page) -> None:
        try:
            await page.close()
        except Exception:
            pass
        finally:
            self._release_tab_slot()

    @staticmethod
    async def _human_action_pause(page, min_ms: int = 250, max_ms: int = 1200) -> None:
        try:
            await page.wait_for_timeout(int(random.uniform(min_ms, max_ms)))
        except Exception:
            pass

    @staticmethod
    async def _goto_with_logging(page, url: str, *, wait_until: str, timeout: int, label: str):
        await PlaywrightScraper._human_action_pause(page)
        logger.info(
            "Playwright goto[%s] request_url=%s wait_until=%s thread=%s",
            label,
            url,
            wait_until,
            threading.get_ident(),
        )
        try:
            response = await page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as exc:
            try:
                current_url = str(page.url or "")
            except Exception:
                current_url = ""
            logger.warning(
                "Playwright goto[%s] failed thread=%s current_url=%s err=%s",
                label,
                threading.get_ident(),
                current_url,
                exc,
            )
            raise
        try:
            final_url = str(page.url or "")
        except Exception:
            final_url = ""
        status = None
        try:
            status = response.status if response is not None else None
        except Exception:
            status = None
        logger.info("Playwright goto[%s] final_url=%s status=%s", label, final_url, status)
        return response

    async def _navigate_and_capture_html(
        self,
        page,
        *,
        url: str,
        label: str,
        wait_until: str,
        timeout: int,
    ) -> Dict[str, Any]:
        response = await self._goto_with_logging(
            page,
            url,
            wait_until=wait_until,
            timeout=timeout,
            label=label,
        )
        final_url = str(getattr(page, "url", "") or "")
        status: Optional[int] = None
        try:
            status = response.status if response is not None else None
        except Exception:
            status = None
        html = ""
        try:
            html = await page.content() or ""
        except Exception:
            html = ""
        return {
            "requested_url": url,
            "final_url": final_url,
            "status": status,
            "html": html,
        }

    def browse_url_html(
        self,
        url: str,
        *,
        label: str = "generic_browser_nav",
        wait_until: str = "commit",
        timeout: int = 30000,
    ) -> Dict[str, Any]:
        """
        General browser-navigation entrypoint for any caller:
        opens a capped page, navigates, and returns HTML + navigation metadata.
        """
        async def _run():
            context = await self._get_thread_context()
            self._sync_session_cookies_into_context(context)
            page = await self._open_capped_page(context)
            try:
                result = await self._navigate_and_capture_html(
                    page,
                    url=url,
                    label=label,
                    wait_until=wait_until,
                    timeout=timeout,
                )
                await self._sync_context_cookies_into_session(context)
                self._save_cached_state()
                return result
            finally:
                await self._close_capped_page(page)
        try:
            return self._run_async(_run(), op_name="browse_url_html")
        except Exception as exc:
            if not self._reset_browser_connection_after_failure(exc, op_name="browse_url_html"):
                raise
            return self._run_async(_run(), op_name="browse_url_html_retry")

    def _load_hardcoded_stayspdp_template(self) -> bool:
        if not isinstance(HARDCODED_STAYS_PDP_TEMPLATE, dict):
            return False
        required = ("url", "headers", "post_data")
        if not all(key in HARDCODED_STAYS_PDP_TEMPLATE for key in required):
            return False
        template = copy.deepcopy(HARDCODED_STAYS_PDP_TEMPLATE)
        safe_base = self._normalize_base_url(self.base_url)
        currency = str(self.currency or "USD").upper()
        locale = str(self.locale or "en-CA")

        def _force_param(query: str, name: str, value: str) -> str:
            q = str(query or "")
            if not q:
                return f"{name}={value}"
            if re.search(rf"(^|&){re.escape(name)}=", q, flags=re.I):
                return re.sub(
                    rf"(^|&){re.escape(name)}=[^&]*",
                    lambda m: f"{m.group(1)}{name}={value}",
                    q,
                    count=1,
                    flags=re.I,
                )
            return f"{q}&{name}={value}"

        def _normalize_query(query: str) -> str:
            # Pin both currency=USD and an English locale on the replayed request
            # so direct HTTP PDP fetches return English content priced in USD.
            return _force_param(_force_param(query, "currency", currency), "locale", locale)

        try:
            raw_url = str(template.get("url") or "").strip()
            if raw_url:
                parsed = urlparse(raw_url)
                normalized_query = _normalize_query(parsed.query)
                query = f"?{normalized_query}" if normalized_query else ""
                template["url"] = f"{safe_base}{parsed.path}{query}"
            headers = template.get("headers")
            if isinstance(headers, dict):
                raw_referer = str(headers.get("referer") or "").strip()
                if raw_referer:
                    parsed_ref = urlparse(raw_referer)
                    normalized_query_ref = _normalize_query(parsed_ref.query)
                    query_ref = f"?{normalized_query_ref}" if normalized_query_ref else ""
                    headers["referer"] = f"{safe_base}{parsed_ref.path}{query_ref}"
        except Exception:
            pass
        self.captured_pdp_req = template
        logger.info("Loaded hardcoded StaysPdpSections template.")
        return True

    def _load_hardcoded_stayssearch_template(self) -> bool:
        if not isinstance(HARDCODED_STAYS_SEARCH_TEMPLATE, dict):
            return False
        required = ("url", "headers", "post_data")
        if not all(key in HARDCODED_STAYS_SEARCH_TEMPLATE for key in required):
            return False
        template = copy.deepcopy(HARDCODED_STAYS_SEARCH_TEMPLATE)
        safe_base = self._normalize_base_url(self.base_url)
        currency = str(self.currency or "USD").upper()
        locale = str(self.locale or "en-CA")

        def _force_param(query: str, name: str, value: str) -> str:
            q = str(query or "")
            if not q:
                return f"{name}={value}"
            if re.search(rf"(^|&){re.escape(name)}=", q, flags=re.I):
                return re.sub(
                    rf"(^|&){re.escape(name)}=[^&]*",
                    lambda m: f"{m.group(1)}{name}={value}",
                    q,
                    count=1,
                    flags=re.I,
                )
            return f"{q}&{name}={value}"

        def _normalize_query(query: str) -> str:
            return _force_param(_force_param(query, "currency", currency), "locale", locale)

        try:
            raw_url = str(template.get("url") or "").strip()
            if raw_url:
                parsed = urlparse(raw_url)
                normalized_query = _normalize_query(parsed.query)
                query = f"?{normalized_query}" if normalized_query else ""
                template["url"] = f"{safe_base}{parsed.path}{query}"
            headers = template.get("headers")
            if isinstance(headers, dict):
                raw_referer = str(headers.get("referer") or "").strip()
                if raw_referer:
                    parsed_ref = urlparse(raw_referer)
                    query_ref = f"?{parsed_ref.query}" if parsed_ref.query else ""
                    headers["referer"] = f"{safe_base}{parsed_ref.path}{query_ref}"
        except Exception:
            pass
        self.hardcoded_search_req = template
        logger.info("Loaded hardcoded StaysSearch template.")
        return True

    def _cookies_to_records(self):
        records = []
        for c in self.session.cookies:
            records.append(
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path,
                    "expires": c.expires,
                    "secure": c.secure,
                }
            )
        return records

    def _restore_cookies(self, cookies):
        self.session.cookies.clear()
        for c in cookies:
            if not isinstance(c, dict):
                continue
            self.session.cookies.set(
                c.get("name", ""),
                c.get("value", ""),
                domain=c.get("domain"),
                path=c.get("path", "/"),
                secure=bool(c.get("secure", False)),
                expires=c.get("expires"),
            )

    def _is_cache_valid(self, saved_at: float, cookies) -> bool:
        if not saved_at or (time.time() - saved_at) > self.session_max_age_seconds:
            return False

        now = time.time()
        has_any_cookie = False
        for c in cookies or []:
            if not isinstance(c, dict):
                continue
            has_any_cookie = True
            exp = c.get("expires")
            if exp is None:
                # Session cookie; consider valid while cache max age is valid.
                return True
            try:
                if float(exp) > now:
                    return True
            except (TypeError, ValueError):
                continue
        return has_any_cookie and False

    def _save_cached_state(self):
        payload = {
            "saved_at": time.time(),
            "cookies": self._cookies_to_records(),
            "captured_search_req": self.captured_search_req,
        }
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception:
            pass

    def fork(self) -> "PlaywrightScraper":
        """
        Create an in-memory clone of the client for concurrent read-only replay.

        The clone reuses captured templates/cookies but has its own requests.Session,
        so concurrent setup tasks (e.g., fixed-pool anchor searches) don't contend
        on a single session object.
        """
        clone = PlaywrightScraper.__new__(PlaywrightScraper)
        clone.config = copy.deepcopy(self.config)
        clone.base_url = self.base_url
        clone.locale = self.locale
        clone.currency = self.currency
        clone.session = requests.Session()
        clone._session_cookies_warmed = self._session_cookies_warmed
        # Not shared across clones: requests.Session isn't safe for concurrent
        # use, and each fork gets its own lazily-built client. The learned hash
        # override *is* copied so forks don't all pay to rediscover it.
        clone._pdp_api_client = None
        clone._pdp_api_hash_override = self._pdp_api_hash_override
        clone.captured_search_req = copy.deepcopy(self.captured_search_req)
        clone.captured_pdp_req = copy.deepcopy(self.captured_pdp_req)
        clone.hardcoded_search_req = copy.deepcopy(self.hardcoded_search_req)
        clone.disable_map_search = self.disable_map_search
        clone.enable_ai_search = self.enable_ai_search
        clone.use_hardcoded_stayspdp_template = self.use_hardcoded_stayspdp_template
        clone.use_hardcoded_stayssearch_template = self.use_hardcoded_stayssearch_template
        clone.cache_path = self.cache_path
        clone.session_max_age_seconds = self.session_max_age_seconds
        clone.refresh_cooldown_seconds = self.refresh_cooldown_seconds
        clone._last_refresh_started_at = self._last_refresh_started_at
        clone.refresh_before_each_search = self.refresh_before_each_search
        clone._pdp_unresolved_windows = copy.deepcopy(self._pdp_unresolved_windows)
        clone._browser_init_lock = threading.Lock()
        clone._session_cookie_lock = threading.Lock()
        clone._loop = None
        clone._loop_thread = None
        clone._loop_ready = threading.Event()
        clone._runtime_owner_tid = None
        clone._cdp_url = self._cdp_url
        clone._uses_external_browser = self._uses_external_browser
        clone._pw = None
        clone._browser = None
        clone._context = None
        clone._context_owned = False
        clone._tab_gate_lock = threading.Lock()
        clone._tab_gate = None
        clone._tab_limit = 8
        clone._open_tab_count = 0
        clone._ensure_tab_gate()
        for c in self.session.cookies:
            clone.session.cookies.set(
                c.name,
                c.value,
                domain=c.domain,
                path=c.path,
                secure=c.secure,
                expires=c.expires,
            )
        return clone

    def _load_cached_state(self) -> bool:
        if not os.path.exists(self.cache_path):
            return False
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return False

        saved_at = payload.get("saved_at")
        cookies = payload.get("cookies", [])
        if not self._is_cache_valid(saved_at, cookies):
            return False

        self._restore_cookies(cookies)
        self.captured_search_req = payload.get("captured_search_req")
        # PDP template is intentionally not restored from cache.
        # It is captured fresh on each listing details fetch.
        self.captured_pdp_req = None
        if self.use_hardcoded_stayspdp_template:
            self._load_hardcoded_stayspdp_template()

        # Search template is required for this run mode.
        if not self.captured_search_req:
            return False
        return True

    @staticmethod
    def _normalize_query_text(value: Any) -> str:
        return re.sub(r"\s*,\s*", ", ", str(value or "").strip())

    @staticmethod
    def _response_looks_auth_or_challenge_error(status_code: int, response_data: Dict[str, Any]) -> bool:
        if status_code in (401, 403):
            return True
        errs = response_data.get("errors")
        if not isinstance(errs, list):
            return False
        for err in errs:
            if not isinstance(err, dict):
                continue
            txt = " ".join(
                str(x or "")
                for x in (
                    err.get("message"),
                    err.get("errorType"),
                    err.get("code"),
                    (err.get("extensions") or {}).get("code"),
                    (err.get("extensions") or {}).get("errorType"),
                )
            ).lower()
            if any(k in txt for k in ("unauth", "forbidden", "csrf", "captcha", "challenge", "login", "security")):
                return True
        return False

    @staticmethod
    def _page_looks_challenged(content_html: str, page_url: str) -> bool:
        txt = f"{page_url}\n{content_html}".lower()
        markers = (
            "captcha",
            "verify you are human",
            "are you a human",
            "security check",
            "unusual traffic",
            "/challenge",
            "/checkpoint",
            "/login",
        )
        return any(m in txt for m in markers)

    def refresh_session(self, force_capture: bool = False, bypass_cooldown: bool = False):
        """
        Browser-only mode: do not capture/replay API templates or headless cookies.
        """
        with self._refresh_lock:
            self._last_refresh_started_at = time.time()
            self.captured_search_req = None
            self.captured_pdp_req = None
            logger.info("Playwright refresh_session is a no-op in browser-only mode.")

    def _capture_pdp_template_for_listing(
        self,
        listing_id: str,
        checkin: Optional[str] = None,
        checkout: Optional[str] = None,
        adults: Optional[int] = None,
        force_refresh: bool = True,
    ):
        """Deprecated in browser-only mode (no API template capture/replay)."""
        logger.info(
            "Skipping _capture_pdp_template_for_listing in browser-only mode for listing_id=%s",
            listing_id,
        )
        self.captured_pdp_req = None

    def _refresh_before_search_if_enabled(self) -> None:
        if not self.refresh_before_each_search:
            return
        logger.info("REFRESH_SESSION_BEFORE_EACH_SEARCH enabled; refreshing session/templates before StaysSearch replay.")
        # Force a fresh capture so request tokens/cookies are rotated each search call.
        self.refresh_session(force_capture=True, bypass_cooldown=True)

    @staticmethod
    def _extract_pdp_sections(response_data: Dict[str, Any]) -> list[dict]:
        if not isinstance(response_data, dict):
            return []
        for path in (
            ("data", "presentation", "stayProductDetailPage", "sections", "sections"),
            ("data", "presentation", "stayproductdetailpage", "sections", "sections"),
        ):
            cur: Any = response_data
            ok = True
            for key in path:
                if not isinstance(cur, dict) or key not in cur:
                    ok = False
                    break
                cur = cur[key]
            if ok and isinstance(cur, list):
                return [x for x in cur if isinstance(x, dict)]
        return []

    @staticmethod
    def _extract_pdp_amenities_from_rendered_html(rendered_html: str) -> Optional[Dict[str, Any]]:
        """
        Parse rendered PDP HTML and return data.node.pdpPresentation.amenities when present.

        This is a fallback for cases where the captured StaysPdpSections response only
        includes booking/policies blocks and omits AMENITIES_* sections.
        """
        if not isinstance(rendered_html, str) or not rendered_html.strip():
            return None

        script_blobs = re.findall(
            r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
            rendered_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not script_blobs:
            return None

        for blob in script_blobs:
            text = str(blob or "").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except Exception:
                continue

            stack: List[Any] = [parsed]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    amenities: Optional[Dict[str, Any]] = None
                    data_node = cur.get("data")
                    if isinstance(data_node, dict):
                        node = data_node.get("node")
                        if isinstance(node, dict):
                            pdp_presentation = node.get("pdpPresentation")
                            if isinstance(pdp_presentation, dict):
                                raw_amenities = pdp_presentation.get("amenities")
                                if isinstance(raw_amenities, dict):
                                    amenities = raw_amenities
                    if isinstance(amenities, dict):
                        preview = amenities.get("previewAmenitiesGroups")
                        see_all = amenities.get("seeAllAmenitiesGroups")
                        if isinstance(preview, list) or isinstance(see_all, list):
                            return amenities
                    for value in cur.values():
                        if isinstance(value, (dict, list)):
                            stack.append(value)
                elif isinstance(cur, list):
                    for value in cur:
                        if isinstance(value, (dict, list)):
                            stack.append(value)

        return None

    @staticmethod
    def _pdp_payload_has_amenity_groups(response_data: Dict[str, Any]) -> bool:
        if not isinstance(response_data, dict):
            return False

        def _has_groups(node: Any) -> bool:
            if isinstance(node, dict):
                for key in ("previewAmenitiesGroups", "seeAllAmenitiesGroups", "amenityGroups"):
                    value = node.get(key)
                    if isinstance(value, list) and len(value) > 0:
                        return True
                for value in node.values():
                    if isinstance(value, (dict, list)) and _has_groups(value):
                        return True
            elif isinstance(node, list):
                for value in node:
                    if isinstance(value, (dict, list)) and _has_groups(value):
                        return True
            return False

        return _has_groups(response_data)

    @staticmethod
    def _inject_pdp_presentation_amenities(
        response_data: Dict[str, Any],
        amenities_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(response_data, dict) or not isinstance(amenities_payload, dict):
            return response_data

        data_node = response_data.setdefault("data", {})
        if not isinstance(data_node, dict):
            return response_data
        node = data_node.setdefault("node", {})
        if not isinstance(node, dict):
            return response_data
        pdp_presentation = node.setdefault("pdpPresentation", {})
        if not isinstance(pdp_presentation, dict):
            return response_data

        pdp_presentation["amenities"] = amenities_payload
        return response_data

    @classmethod
    def _pdp_booking_has_price(cls, response_data: Dict[str, Any]) -> bool:
        sections = cls._extract_pdp_sections(response_data)
        for entry in sections:
            sid = entry.get("sectionId")
            if sid not in ("BOOK_IT_FLOATING_FOOTER", "BOOK_IT_SIDEBAR", "BOOK_IT_NAV"):
                continue
            sec = entry.get("section")
            if not isinstance(sec, dict):
                continue
            primary = ((sec.get("structuredDisplayPrice") or {}).get("primaryLine") or {})
            if not isinstance(primary, dict):
                continue
            for key in ("price", "discountedPrice", "accessibilityLabel"):
                value = primary.get(key)
                if isinstance(value, str) and value.strip():
                    return True
        return False

    @classmethod
    def _pdp_booking_unresolved(cls, response_data: Dict[str, Any]) -> bool:
        sections = cls._extract_pdp_sections(response_data)
        saw_booking = False
        saw_not_complete = False
        for entry in sections:
            sid = entry.get("sectionId")
            if sid not in ("BOOK_IT_FLOATING_FOOTER", "BOOK_IT_SIDEBAR", "BOOK_IT_NAV"):
                continue
            saw_booking = True
            status = str(entry.get("sectionContentStatus") or "").upper()
            if "NOT_COMPLETE" in status:
                saw_not_complete = True
        return saw_booking and saw_not_complete and not cls._pdp_booking_has_price(response_data)

    @staticmethod
    def _pdp_dates_unavailable(response_data: Dict[str, Any]) -> bool:
        try:
            payload_text = json.dumps(response_data, ensure_ascii=False).lower()
        except Exception:
            payload_text = str(response_data or "").lower()
        markers = (
            "those dates are not available",
            "dates are not available",
            "date_not_available",
            "not available for these dates",
        )
        return any(m in payload_text for m in markers)

    @staticmethod
    def _rendered_html_dates_unavailable(rendered_html: str) -> bool:
        if not isinstance(rendered_html, str) or not rendered_html:
            return False
        return "those dates are not available" in rendered_html.lower()

    @staticmethod
    def _extract_dom_price_text(raw_text: str) -> Optional[str]:
        if not isinstance(raw_text, str):
            return None
        text = raw_text.replace("\xa0", " ").strip()
        if not text:
            return None
        # Require currency marker to avoid false positives like "4 guests".
        currency_first = re.search(
            r"(?:[A-Z]{1,3}\$|\$|\u20AC|\u00A3|\u00A5|\u20B9|\u20A9|\u20AA|\u20AB|\u20BD|\u20B4|\u20B1|\u0E3F|\u20A6|\u20BA)\s*\d[\d,]*(?:\.\d{1,2})?(?:\s*[A-Z]{3})?",
            text,
        )
        if currency_first:
            return currency_first.group(0).strip() or None
        currency_last = re.search(
            r"\d[\d,]*(?:\.\d{1,2})?\s*[A-Z]{3}",
            text,
        )
        if currency_last:
            return currency_last.group(0).strip() or None
        return None

    @staticmethod
    async def _read_dom_price_text(page, timeout_ms: int = 5000) -> Optional[str]:
        js = """
() => {
  const clean = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
  const parsePrice = (text) => {
    const t = clean(text);
    if (!t) return null;
    const m1 = t.match(/(?:[A-Z]{1,3}\\$|\\$|€|£|¥|₹|₩|₪|₫|₽|₴|₱|฿|₦|₺)\\s*\\d[\\d,]*(?:\\.\\d{1,2})?(?:\\s*[A-Z]{3})?/);
    if (m1) return m1[0].trim();
    const m2 = t.match(/\\d[\\d,]*(?:\\.\\d{1,2})?\\s*[A-Z]{3}/);
    return m2 ? m2[0].trim() : null;
  };
  const getNumeric = (priceText) => {
    const m = (priceText || '').match(/\\d[\\d,]*(?:\\.\\d{1,2})?/);
    return m ? Number(m[0].replace(/,/g, '')) : null;
  };
  const isStrikethrough = (el) => {
    let node = el;
    for (let i = 0; i < 4 && node; i++) {
      const cs = getComputedStyle(node);
      const td = (cs.textDecorationLine || cs.textDecoration || '').toLowerCase();
      if (td.includes('line-through')) return true;
      node = node.parentElement;
    }
    return false;
  };
  const inBookIt = (el) =>
    !!el.closest(
      '[data-testid="book-it-default"], [data-testid="book-it-sidebar"], [data-testid="price-block"], [data-testid="book-it-price-breakdown"], [data-section-id*="BOOK_IT"], [data-plugin-in-point-id*="BOOK_IT"]'
    );
  const hasNightContext = (el) => {
    const box = el.closest(
      '[data-testid="book-it-default"], [data-testid="book-it-sidebar"], [data-testid="price-block"], [data-testid="book-it-price-breakdown"], [data-section-id*="BOOK_IT"], [data-plugin-in-point-id*="BOOK_IT"]'
    ) || el.parentElement;
    const txt = clean(box ? (box.innerText || box.textContent || '') : '');
    return /\\/\\s*night|per\\s+night|night/i.test(txt);
  };

  const nodes = Array.from(document.querySelectorAll('span, div, b, strong'));
  const candidates = [];
  let idx = 0;
  for (const el of nodes) {
    if (!inBookIt(el)) continue;
    const text = clean(el.textContent || '');
    if (!text) continue;
    const priceText = parsePrice(text);
    if (!priceText) continue;
    const value = getNumeric(priceText);
    if (!(value > 0)) continue;
    candidates.push({
      idx: idx++,
      text,
      priceText,
      value,
      strikethrough: isStrikethrough(el),
      nightContext: hasNightContext(el),
    });
  }

  if (!candidates.length) {
    return { extracted: null, candidates: [] };
  }

  const preferred = candidates
    .filter(c => c.nightContext && !c.strikethrough)
    .sort((a, b) => a.idx - b.idx);
  if (!preferred.length) {
    return { extracted: null, candidates: candidates.slice(0, 30), reason: 'no_nightly_context' };
  }
  const picked = preferred[preferred.length - 1];
  return {
    extracted: picked ? picked.priceText : null,
    candidates: candidates.slice(0, 30),
  };
}
"""
        try:
            values = await page.evaluate(js)
        except Exception:
            values = {}
        if isinstance(values, dict):
            candidates = values.get("candidates")
            if isinstance(candidates, list) and candidates:
                logger.info(
                    "Playwright PDP DOM discount-aware candidates count=%s sample=%s",
                    len(candidates),
                    [
                        {
                            "priceText": str(c.get("priceText") or ""),
                            "strike": bool(c.get("strikethrough")),
                            "night": bool(c.get("nightContext")),
                        }
                        for c in candidates[:8]
                        if isinstance(c, dict)
                    ],
                )
            extracted = values.get("extracted")
            if isinstance(extracted, str) and extracted.strip():
                logger.info("Playwright PDP DOM discount-aware match extracted=%s", extracted.strip())
                return extracted.strip()
        return None

    @staticmethod
    def _build_minimal_pdp_payload(price_text: Optional[str] = None) -> Dict[str, Any]:
        primary_line: Dict[str, Any] = {}
        if isinstance(price_text, str) and price_text.strip():
            primary_line = {
                "price": price_text.strip(),
                "qualifier": "night",
                "accessibilityLabel": price_text.strip(),
            }
        return {
            "data": {
                "presentation": {
                    "stayProductDetailPage": {
                        "sections": {
                            "sections": [
                                {
                                    "sectionId": "BOOK_IT_SIDEBAR",
                                    "section": {
                                        "available": True,
                                        "structuredDisplayPrice": {"primaryLine": primary_line} if primary_line else {},
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        }

    @classmethod
    def _inject_price_into_pdp_payload(cls, response_data: Dict[str, Any], price_text: str) -> Dict[str, Any]:
        if not isinstance(response_data, dict):
            return response_data
        sections = cls._extract_pdp_sections(response_data)
        if not sections:
            return response_data
        for entry in sections:
            sid = entry.get("sectionId")
            if sid not in ("BOOK_IT_FLOATING_FOOTER", "BOOK_IT_SIDEBAR", "BOOK_IT_NAV"):
                continue
            sec = entry.get("section")
            if not isinstance(sec, dict):
                continue
            sdp = sec.get("structuredDisplayPrice")
            if not isinstance(sdp, dict):
                sdp = {}
                sec["structuredDisplayPrice"] = sdp
            primary = sdp.get("primaryLine")
            if not isinstance(primary, dict):
                primary = {}
                sdp["primaryLine"] = primary
            primary["price"] = str(price_text)
            primary.setdefault("qualifier", "night")
            primary["accessibilityLabel"] = str(price_text)
            return response_data
        return response_data

    def search_listings(self) -> Tuple[int, Dict[str, Any]]:
        """Browser-only StaysSearch (no API replay)."""
        limiter = get_airbnb_rate_limiter()
        last_exc: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                with limiter.slot():
                    status_code, response_data = self._run_async(
                        self._search_via_browser(),
                        op_name="search_listings",
                    )
                if response_data.get("errors") and self._response_looks_auth_or_challenge_error(status_code, response_data):
                    raise RuntimeError("Browser StaysSearch returned auth/challenge-like GraphQL error")
                return status_code, response_data
            except Exception as exc:
                last_exc = exc
                is_rate_limited = isinstance(exc, AirbnbRateLimited)
                logger.warning(
                    "Browser StaysSearch attempt %s/2 failed%s: %s",
                    attempt,
                    " (rate-limited)" if is_rate_limited else "",
                    exc,
                )
                if attempt < 2:
                    if is_rate_limited:
                        limiter.penalize()
                        time.sleep(2.0 + random.random() * 2.0)
                    else:
                        self._reset_browser_connection_after_failure(exc, op_name="search_listings")
                        time.sleep(1.0)
        raise RuntimeError(f"Playwright browser search failed after 2 attempts: {last_exc}")

    @staticmethod
    def _raw_param_exists(raw_params: Any, filter_name: str) -> bool:
        if not isinstance(raw_params, list):
            return False
        for p in raw_params:
            if isinstance(p, dict) and p.get("filterName") == filter_name:
                return True
        return False

    @staticmethod
    def _set_raw_param(raw_params: Any, filter_name: str, filter_values: list[str]):
        if not isinstance(raw_params, list):
            return
        for p in raw_params:
            if isinstance(p, dict) and p.get("filterName") == filter_name:
                p["filterValues"] = filter_values
                return
        raw_params.append({"filterName": filter_name, "filterValues": filter_values})

    @staticmethod
    def _remove_raw_param(raw_params: Any, filter_name: str):
        if not isinstance(raw_params, list):
            return
        raw_params[:] = [p for p in raw_params if not (isinstance(p, dict) and p.get("filterName") == filter_name)]

    def _apply_disable_map_search(self, payload: Dict[str, Any]) -> None:
        """Disable map-oriented search path while keeping persisted-query shape safe."""
        if not self.disable_map_search:
            return
        variables = payload.get("variables")
        if not isinstance(variables, dict):
            return

        stays_req = variables.get("staysSearchRequest")
        if isinstance(stays_req, dict):
            stays_req["maxMapItems"] = 0

        map_req = variables.get("staysMapSearchRequestV2")
        if isinstance(map_req, dict):
            map_req["metadataOnly"] = True
            map_req["rawParams"] = []

    @staticmethod
    def _sanitize_captured_headers(headers: Dict[str, Any]) -> Dict[str, str]:
        """Drop HTTP/2 pseudo-headers and hop-by-hop headers before replay."""
        drop = {"content-length", "host", "connection", "accept-encoding"}
        clean: Dict[str, str] = {}
        for key, value in (headers or {}).items():
            k = str(key or "")
            if k.startswith(":"):
                continue
            if k.lower() in drop:
                continue
            clean[k] = str(value)
        return clean

    async def _capture_search_request_template(self, req, resp_url: str) -> None:
        """
        Persist the live StaysSearch POST request as a replayable template so
        subsequent searches can use direct HTTP (fetch_search_direct) instead of
        opening a browser tab. Mirrors the hardcoded PDP template approach.
        """
        try:
            method = str(getattr(req, "method", "") or "").upper()
            if method != "POST":
                return
            post_data = getattr(req, "post_data", None)
            if not post_data:
                return
            parsed_post = json.loads(post_data)
            if not isinstance(parsed_post, dict):
                return
            try:
                headers = await req.all_headers()
            except Exception:
                headers = dict(getattr(req, "headers", {}) or {})
            template = {
                "url": str(getattr(req, "url", "") or resp_url),
                "method": "POST",
                "headers": self._sanitize_captured_headers(headers),
                "post_data": parsed_post,
            }
            self.captured_search_req = template
            logger.info(
                "Captured StaysSearch request template for direct-HTTP replay url=%s",
                template["url"],
            )
        except Exception as exc:
            logger.debug("StaysSearch template capture skipped: %s", exc)

    @staticmethod
    def _force_url_query_params(url: str, **params: str) -> str:
        """Force query params onto a URL, overwriting any existing values."""
        try:
            parsed = urlparse(str(url or ""))
        except Exception:
            return url
        q = parsed.query
        for name, value in params.items():
            if re.search(rf"(^|&){re.escape(name)}=", q, flags=re.I):
                q = re.sub(
                    rf"(^|&){re.escape(name)}=[^&]*",
                    lambda m, n=name, v=value: f"{m.group(1)}{n}={v}",
                    q,
                    count=1,
                    flags=re.I,
                )
            else:
                q = f"{q}&{name}={value}" if q else f"{name}={value}"
        return urlunparse(parsed._replace(query=q))

    def _build_search_navigation_url(self, overrides: Optional[Dict[str, Any]] = None) -> str:
        ov = overrides or {}
        query_raw = (
            ov.get("query")
            or ov.get("locationSearch")
            or ov.get("location")
            or self.config.get("QUERY", "Mississauga, Ontario")
        )
        normalized_display_query = self._normalize_query_text(query_raw)
        path_query = normalized_display_query.replace(", ", ",")
        search_path = f"/s/{quote(path_query).replace('%2C', '--')}/homes"

        params: Dict[str, Any] = {
            "date_picker_type": self.config.get("DATE_PICKER_TYPE", "calendar"),
            "center_lat": ov.get("centerLat", self.config.get("CENTER_LAT", "")),
            "center_lng": ov.get("centerLng", self.config.get("CENTER_LNG", "")),
            "refinement_paths[]": "/homes",
            "place_id": ov.get("placeId", self.config.get("PLACE_ID", "")),
            "checkin": ov.get("checkin", self.config.get("CHECKIN", "")),
            "checkout": ov.get("checkout", self.config.get("CHECKOUT", "")),
            "adults": ov.get("adults", ov.get("guests", self.config.get("ADULTS", 1))),
            "query": normalized_display_query,
            "search_type": "AUTOSUGGEST",
            # Force English content + USD pricing on the search page (and on any
            # StaysSearch request template captured from it).
            "locale": self.locale,
            "currency": self.currency,
        }
        if "itemsPerGrid" in ov and ov.get("itemsPerGrid") is not None:
            params["items_per_grid"] = ov.get("itemsPerGrid")
        if "itemsOffset" in ov and ov.get("itemsOffset") is not None:
            params["items_offset"] = ov.get("itemsOffset")
        base_url = self._normalize_base_url(self.base_url)
        return f"{base_url}{search_path}?{urlencode(params)}"

    @staticmethod
    def _build_minimal_search_payload_from_listing_ids(listing_ids: list[str]) -> Optional[Dict[str, Any]]:
        """
        Convert listing IDs into the minimal StaysSearch-shaped payload expected
        by parse_search_response()/parse_search_listing_context().
        """
        if not isinstance(listing_ids, list):
            return None
        deduped: list[str] = []
        seen: set[str] = set()
        for lid in listing_ids:
            s = str(lid).strip()
            if not s or not s.isdigit() or s in seen:
                continue
            seen.add(s)
            deduped.append(s)
        if not deduped:
            return None
        search_results = [{"listingId": lid} for lid in deduped]
        return {
            "data": {
                "presentation": {
                    "staysSearch": {
                        "results": {
                            "searchResults": search_results,
                        }
                    }
                }
            }
        }

    @staticmethod
    async def _extract_search_listing_ids_from_rendered_dom(page) -> list[str]:
        """
        Rendered-DOM fallback: read listing IDs from the hydrated search page.
        This avoids relying on pre-hydration shell HTML.
        """
        js = r"""
() => {
  const ids = new Set();
  const addFromText = (v) => {
    const s = String(v || '');
    const re = /\/rooms\/(\d{5,})/g;
    let m;
    while ((m = re.exec(s)) !== null) ids.add(String(m[1]));
  };

  const anchors = Array.from(document.querySelectorAll('a[href*="/rooms/"]'));
  for (const a of anchors) {
    addFromText(a.getAttribute('href') || '');
    addFromText(a.href || '');
  }

  const attrs = ['data-testid', 'data-ev-label', 'data-ev-data', 'aria-label'];
  const nodes = Array.from(document.querySelectorAll('[data-testid], [data-ev-label], [data-ev-data], [aria-label]'));
  for (const n of nodes) {
    for (const key of attrs) addFromText(n.getAttribute(key) || '');
  }

  return Array.from(ids);
}
"""
        try:
            values = await page.evaluate(js)
        except Exception:
            values = []
        out: list[str] = []
        if isinstance(values, list):
            for v in values:
                s = str(v or "").strip()
                if s.isdigit():
                    out.append(s)
        return out

    async def _ensure_browser(self):
        if self._browser is not None and self._pw is not None:
            return self._browser
        if not self._cdp_url:
            raise RuntimeError("CDP_URL is required; Playwright scraper must attach to existing browser.")
        self._pw = await async_playwright().start()
        candidates = [u.strip() for u in self._cdp_url.split(",") if u.strip()]
        last_exc: Optional[Exception] = None
        for cdp_url in candidates:
            logger.info(
                "Connecting Playwright to existing browser via CDP: %s [thread=%s]",
                cdp_url,
                threading.get_ident(),
            )
            try:
                self._browser = await self._pw.chromium.connect_over_cdp(cdp_url, timeout=15000)
                return self._browser
            except Exception as exc:
                logger.warning("CDP connect failed for %s: %s", cdp_url, exc)
                last_exc = exc
        raise RuntimeError(
            f"Could not connect to any CDP endpoint ({self._cdp_url}): {last_exc}"
        )

    async def _get_thread_context(self):
        if self._context is not None:
            return self._context
        browser = await self._ensure_browser()
        if browser.contexts:
            # Prefer the existing persistent context so new pages open as tabs
            # in the original browser window.
            self._context = browser.contexts[0]
            self._context_owned = False
            logger.info(
                "Playwright context selected [thread=%s] mode=existing_context",
                threading.get_ident(),
            )
        else:
            # Fallback only when no existing browser context is available.
            user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            )
            viewport = {"width": 1280, "height": 800}
            self._context = await browser.new_context(user_agent=user_agent, viewport=viewport)
            self._context_owned = True
            logger.info(
                "Playwright context created [thread=%s] mode=new_context_fallback",
                threading.get_ident(),
            )
        return self._context

    def _snapshot_session_cookies(self) -> list[dict]:
        out: list[dict] = []
        with self._session_cookie_lock:
            for c in self.session.cookies:
                try:
                    out.append(
                        {
                            "name": c.name,
                            "value": c.value,
                            "domain": c.domain,
                            "path": c.path or "/",
                            "secure": bool(c.secure),
                        }
                    )
                except Exception:
                    continue
        return out

    def _sync_session_cookies_into_context(self, context) -> None:
        # When attached over CDP, rely on cookies from the existing browser profile/session.
        # Do not mutate context cookies from requests.Session.
        return

    def _ensure_session_cookies_from_browser(self) -> None:
        """
        Pull the logged-in CDP browser profile's cookies into self.session so
        direct-HTTP replays (search + PDP) are authenticated. Airbnb serves
        different prices to logged-out sessions (e.g. $297 vs the logged-in $299),
        so an unauthenticated requests.Session yields the wrong price. Runs once
        per client (idempotent); failures fall through to the unauthenticated
        session, matching prior behavior.
        """
        if self._session_cookies_warmed:
            return
        try:
            async def _warm() -> None:
                context = await self._get_thread_context()
                await self._sync_context_cookies_into_session(context)

            self._run_async(_warm(), op_name="warm_session_cookies")
            self._session_cookies_warmed = True
            logger.info(
                "[session] warmed %s cookies from CDP browser for direct-HTTP replay",
                len(self.session.cookies),
            )
        except Exception as exc:
            logger.debug("[session] cookie warm from CDP browser failed: %s", exc)

    async def _sync_context_cookies_into_session(self, context) -> None:
        try:
            context_cookies = await context.cookies()
        except Exception:
            return
        with self._session_cookie_lock:
            self.session.cookies.clear()
            for cookie in context_cookies:
                self.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie["domain"],
                    path=cookie["path"],
                )

    async def _close_extra_tabs_async(self) -> None:
        """Close all pages in the browser context except the first (blank) one."""
        context = self._context
        if context is None:
            return
        try:
            pages = context.pages
            # Keep at most one page open; close everything else.
            for page in pages[1:]:
                try:
                    await page.close()
                except Exception:
                    pass
        except Exception:
            pass
        # Reset the tab counter so the gate stays accurate.
        with self._tab_gate_lock:
            self._open_tab_count = 0
            self._tab_gate = threading.BoundedSemaphore(self._tab_limit)

    def close_extra_tabs(self) -> None:
        """Sync wrapper: close all but one tab in the browser context after a task."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._close_extra_tabs_async(), loop)
            future.result(timeout=10)
        except Exception:
            pass

    async def _close_browser_async(self) -> None:
        try:
            if self._context is not None and self._context_owned:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser is not None and not self._uses_external_browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._context = None
        self._context_owned = False
        self._browser = None
        self._pw = None

    def close_browser(self) -> None:
        loop = self._loop
        loop_thread = self._loop_thread
        if loop is None:
            return
        try:
            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self._close_browser_async(), loop)
                future.result(timeout=20)
            else:
                asyncio.run(self._close_browser_async())
        except Exception:
            pass
        try:
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        if loop_thread is not None and loop_thread.is_alive() and loop_thread.ident != threading.get_ident():
            try:
                loop_thread.join(timeout=5)
            except Exception:
                pass
        with self._browser_init_lock:
            self._loop = None
            self._loop_thread = None
            self._runtime_owner_tid = None
            self._loop_ready.clear()

    def ensure_browser_ready(self) -> None:
        async def _warm() -> None:
            await self._get_thread_context()

        self._run_async(_warm(), op_name="ensure_browser_ready")

    def __del__(self):
        try:
            self.close_browser()
        except Exception:
            pass

    async def _search_via_browser(self, overrides: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        """Run a real browser search and capture the live StaysSearch JSON response."""
        context = await self._get_thread_context()
        self._sync_session_cookies_into_context(context)
        page = await self._open_capped_page(context)
        try:
            captured_status: int = 0
            captured_data: Optional[Dict[str, Any]] = None
            captured_url: str = ""
            saw_stayssearch_response = False
            saw_rate_limit = False
            latest_nav_html: str = ""
            latest_nav_url: str = ""
            challenge_detected = False
            challenge_reason = ""
            response_tasks: set[asyncio.Task] = set()

            async def _handle_response(resp):
                nonlocal captured_status, captured_data, captured_url
                nonlocal saw_stayssearch_response, saw_rate_limit
                try:
                    req = resp.request
                    req_method = str(getattr(req, "method", "") or "").upper()
                    if req_method not in ("POST", "GET"):
                        return
                    resp_url = str(getattr(resp, "url", "") or "")
                    # Airbnb endpoints drift (slash/no-slash, locale/CDN variants).
                    # Match by operation token instead of exact hardcoded path.
                    if "stayssearch" not in resp_url.lower():
                        return
                    saw_stayssearch_response = True
                    if _is_rate_limited_status(resp.status):
                        saw_rate_limit = True
                    if captured_data is not None:
                        return
                    captured_status = int(resp.status)
                    payload = await resp.json()
                    if isinstance(payload, dict):
                        captured_data = payload
                        captured_url = resp_url
                        # Capture the request template for direct-HTTP replay
                        # (fetch_search_direct), mirroring the PDP template path.
                        await self._capture_search_request_template(req, resp_url)
                except Exception:
                    return

            def _on_response(resp):
                task = asyncio.create_task(_handle_response(resp))
                response_tasks.add(task)
                task.add_done_callback(lambda t: response_tasks.discard(t))

            page.on("response", _on_response)

            search_url = self._build_search_navigation_url(overrides)
            if str(search_url).lower().startswith("about:"):
                logger.warning("Resolved about:* search URL; rebuilding with safe default base.")
                safe_base = self._normalize_base_url(None)
                search_url = search_url.replace(str(self.base_url), safe_base, 1)
            logger.info("Playwright browser search navigate: %s", search_url)
            nav = await self._navigate_and_capture_html(
                page,
                url=search_url,
                wait_until="commit",
                timeout=30000,
                label="search_primary",
            )
            logger.info(
                "Playwright search nav result final_url=%s status=%s html_len=%s",
                str((nav or {}).get("final_url") or ""),
                str((nav or {}).get("status")),
                len(str((nav or {}).get("html") or "")),
            )
            latest_nav_html = str((nav or {}).get("html") or "")
            latest_nav_url = str((nav or {}).get("final_url") or "")
            if self._page_looks_challenged(
                str((nav or {}).get("html") or ""),
                str((nav or {}).get("final_url") or ""),
            ):
                challenge_detected = True
                challenge_reason = "Playwright browser search reached login/challenge page; StaysSearch is blocked"
                logger.warning(challenge_reason)
            if str((nav or {}).get("final_url") or "").lower().startswith("about:blank"):
                logger.warning("Browser remained on about:blank after search navigate; retrying with safe base URL.")
                safe_base = self._normalize_base_url(None)
                safe_search_url = self._build_search_navigation_url(
                    {**(overrides or {}), "query": (overrides or {}).get("query")}
                ).replace(self._normalize_base_url(self.base_url), safe_base, 1)
                logger.info("Playwright browser search re-navigate: %s", safe_search_url)
                nav = await self._navigate_and_capture_html(
                    page,
                    url=safe_search_url,
                    wait_until="commit",
                    timeout=30000,
                    label="search_about_blank_retry",
                )
                latest_nav_html = str((nav or {}).get("html") or "")
                latest_nav_url = str((nav or {}).get("final_url") or "")
            await page.wait_for_timeout(int(random.uniform(900, 1600)))
            await page.mouse.wheel(0, 600)

            for _ in range(24):
                if captured_data is not None:
                    break
                await page.wait_for_timeout(int(random.uniform(250, 550)))

            if captured_data is None:
                # One fallback nudge to trigger XHR search.
                fallback_url = search_url + ("&search_type=filter_change" if "search_type=" in search_url else "")
                nav = await self._navigate_and_capture_html(
                    page,
                    url=fallback_url,
                    wait_until="commit",
                    timeout=30000,
                    label="search_filter_change_fallback",
                )
                latest_nav_html = str((nav or {}).get("html") or "")
                latest_nav_url = str((nav or {}).get("final_url") or "")
                if self._page_looks_challenged(
                    str((nav or {}).get("html") or ""),
                    str((nav or {}).get("final_url") or ""),
                ):
                    challenge_detected = True
                    challenge_reason = (
                        "Playwright browser search fallback reached login/challenge page; "
                        "StaysSearch is blocked"
                    )
                    logger.warning(challenge_reason)
                await page.wait_for_timeout(int(random.uniform(900, 1600)))
                await page.mouse.wheel(0, 700)
                for _ in range(24):
                    if captured_data is not None:
                        break
                    await page.wait_for_timeout(int(random.uniform(250, 550)))

            if response_tasks:
                await asyncio.gather(*list(response_tasks), return_exceptions=True)

            await self._sync_context_cookies_into_session(context)
            self._save_cached_state()

            if captured_data is None:
                if challenge_detected:
                    html_preview = (latest_nav_html or "").replace("\n", " ").replace("\r", " ")
                    if len(html_preview) > 4000:
                        html_preview = html_preview[:4000] + "...<truncated>"
                    logger.warning(
                        "Playwright challenge fallback HTML snapshot final_url=%s html_len=%s html_preview=%s",
                        latest_nav_url,
                        len(latest_nav_html or ""),
                        html_preview,
                    )
                # Rendered-DOM fallback only: wait briefly for hydration, then
                # extract listing IDs from actual DOM state.
                await page.wait_for_timeout(1200)
                dom_listing_ids = await self._extract_search_listing_ids_from_rendered_dom(page)
                dom_fallback = self._build_minimal_search_payload_from_listing_ids(dom_listing_ids)
                if isinstance(dom_fallback, dict):
                    extracted = (
                        (((dom_fallback.get("data") or {}).get("presentation") or {}).get("staysSearch") or {})
                        .get("results", {})
                        .get("searchResults", [])
                    )
                    logger.warning(
                        "Playwright StaysSearch capture missing; using rendered-DOM fallback parser "
                        "final_url=%s extracted_listings=%s",
                        latest_nav_url,
                        len(extracted) if isinstance(extracted, list) else 0,
                    )
                    return 200, dom_fallback
                if challenge_detected:
                    logger.warning(
                        "Playwright challenge rendered-DOM fallback found no listing IDs final_url=%s html_len=%s",
                        latest_nav_url,
                        len(latest_nav_html or ""),
                    )
                if challenge_detected and challenge_reason:
                    raise RuntimeError(challenge_reason)
                if saw_rate_limit:
                    raise AirbnbRateLimited(
                        "Playwright browser search received HTTP 429/503 from StaysSearch"
                    )
                if saw_stayssearch_response:
                    raise RuntimeError(
                        "Playwright browser search saw StaysSearch traffic but could not parse JSON payload"
                    )
                raise RuntimeError(
                    "Playwright browser search did not capture StaysSearch response "
                    f"(search_url={search_url})"
                )
            if captured_url:
                logger.info("Playwright captured StaysSearch response url=%s status=%s", captured_url, captured_status)
            return (captured_status or 200), captured_data
        finally:
            await self._close_capped_page(page)

    async def _get_listing_details_via_browser(
        self,
        listing_id: str,
        checkin: str,
        checkout: str,
        adults: int,
    ) -> Tuple[int, Dict[str, Any]]:
        """Run a real browser PDP visit and capture live StaysPdpSections JSON response."""
        context = await self._get_thread_context()
        self._sync_session_cookies_into_context(context)
        page = await self._open_capped_page(context)
        try:
            captured_status: int = 0
            captured_data: Optional[Dict[str, Any]] = None
            terminal_reason: Optional[str] = None
            first_pdp_seen_at: Optional[float] = None
            challenged_at_nav: bool = False
            noncanonical_pdp_host: bool = False
            response_tasks: set[asyncio.Task] = set()

            async def _handle_response(resp):
                nonlocal captured_status, captured_data, terminal_reason, first_pdp_seen_at
                try:
                    req = resp.request
                    if req.method != "POST":
                        return
                    if "/api/v3/StaysPdpSections/" not in resp.url:
                        return
                    if first_pdp_seen_at is None:
                        first_pdp_seen_at = time.monotonic()
                    hash_m = _PDP_API_HASH_IN_URL_RE.search(resp.url)
                    if hash_m and hash_m.group(1) != self._pdp_api_hash_override:
                        self._pdp_api_hash_override = hash_m.group(1)
                        logger.info(
                            "[pdp_api] learned fresh persisted-query hash from live browser traffic"
                        )
                    captured_status = int(resp.status)
                    payload = await resp.json()
                    if isinstance(payload, dict):
                        logger.info(
                            "Playwright PDP API read listing=%s status=%s url=%s",
                            listing_id,
                            captured_status,
                            str(resp.url or ""),
                        )
                        captured_data = payload
                        has_price = self._pdp_booking_has_price(payload)
                        dates_unavailable = self._pdp_dates_unavailable(payload)
                        if dates_unavailable:
                            terminal_reason = "dates_unavailable"
                            # Keep captured_data = payload so location/metadata are still
                            # extractable. Only the booking price will be missing (correct).
                        elif has_price:
                            terminal_reason = "price"
                        logger.info(
                            "Playwright PDP payload listing=%s status=%s has_price=%s dates_unavailable=%s terminal=%s",
                            listing_id,
                            captured_status,
                            has_price,
                            dates_unavailable,
                            terminal_reason or "",
                        )
                except Exception:
                    return

            def _on_response(resp):
                task = asyncio.create_task(_handle_response(resp))
                response_tasks.add(task)
                task.add_done_callback(lambda t: response_tasks.discard(t))

            page.on("response", _on_response)

            listing_url = self._build_pdp_listing_url(
                listing_id=str(listing_id),
                checkin=checkin,
                checkout=checkout,
                adults=adults,
            )
            logger.info("Playwright browser PDP navigate: %s", listing_url)
            nav = await self._navigate_and_capture_html(
                page,
                url=listing_url,
                wait_until="domcontentloaded",
                timeout=35000,
                label=f"pdp_{listing_id}",
            )
            rendered_html = str((nav or {}).get("html") or "")
            nav_final_url = str((nav or {}).get("final_url") or "")
            if nav_final_url and not self._is_canonical_airbnb_url(nav_final_url):
                logger.warning(
                    "Playwright PDP redirected to non-canonical Airbnb host listing=%s final_url=%s; retrying canonical .com URL once",
                    listing_id,
                    nav_final_url,
                )
                nav = await self._navigate_and_capture_html(
                    page,
                    url=listing_url,
                    wait_until="domcontentloaded",
                    timeout=35000,
                    label=f"pdp_{listing_id}_canonical_retry",
                )
                nav_final_url = str((nav or {}).get("final_url") or "")
                if nav_final_url and not self._is_canonical_airbnb_url(nav_final_url):
                    noncanonical_pdp_host = True
                    logger.warning(
                        "Playwright PDP still non-canonical after retry listing=%s final_url=%s; ignoring this host for nightly extraction",
                        listing_id,
                        nav_final_url,
                    )
            rendered_html = str((nav or {}).get("html") or "")
            logger.info(
                "Playwright PDP nav result listing=%s final_url=%s status=%s html_len=%s",
                listing_id,
                str((nav or {}).get("final_url") or ""),
                str((nav or {}).get("status")),
                len(str((nav or {}).get("html") or "")),
            )
            if str((nav or {}).get("final_url") or "").lower().startswith("about:blank"):
                raise RuntimeError(f"Playwright PDP landed on about:blank for listing={listing_id}")
            if _is_rate_limited_status((nav or {}).get("status")):
                raise AirbnbRateLimited(
                    f"Playwright PDP nav returned HTTP {(nav or {}).get('status')} for listing={listing_id}"
                )
            await page.wait_for_timeout(int(random.uniform(900, 1600)))
            if self._page_looks_challenged(str((nav or {}).get("html") or ""), str((nav or {}).get("final_url") or "")):
                challenged_at_nav = True
                logger.info(
                    "Playwright PDP challenge/login detected listing=%s; proceeding to same-tab HTML reads",
                    listing_id,
                )
            await page.mouse.wheel(0, 1200)

            # Phase 1: wait until StaysPdpSections starts arriving.
            if not challenged_at_nav:
                prefetch_deadline = time.monotonic() + 10.0
                while first_pdp_seen_at is None and time.monotonic() < prefetch_deadline:
                    await page.wait_for_timeout(120)

            # Phase 2: API-only terminal detection.
            if (not challenged_at_nav) and first_pdp_seen_at is not None:
                phase2_deadline = time.monotonic() + 3.0
                while True:
                    if terminal_reason is not None:
                        break
                    if time.monotonic() >= phase2_deadline:
                        break
                    await page.wait_for_timeout(120)

            if terminal_reason == "dates_unavailable":
                logger.info(
                    "Playwright PDP API shows unavailable dates listing=%s; returning full payload (no price) for metadata extraction",
                    listing_id,
                )
                # Return the full payload so location/title/accommodates can be extracted.
                # The BOOK_IT sections have available=False so price parsing will correctly yield None.
                return (captured_status or 200), (captured_data or self._build_minimal_pdp_payload(None))

            if terminal_reason is not None:
                # Hold very briefly after terminal detection so last payload
                # updates can settle before closing the tab.
                await page.wait_for_timeout(100)

            if response_tasks:
                await asyncio.gather(*list(response_tasks), return_exceptions=True)

            await self._sync_context_cookies_into_session(context)
            self._save_cached_state()

            try:
                latest_html = await page.content() or ""
                if latest_html:
                    rendered_html = latest_html
            except Exception as exc:
                logger.info(
                    "Playwright PDP rendered HTML refresh failed listing=%s: %s",
                    listing_id,
                    exc,
                )
            if self._rendered_html_dates_unavailable(rendered_html):
                logger.info(
                    "Playwright PDP rendered HTML shows unavailable dates listing=%s; returning captured payload (no price) for metadata",
                    listing_id,
                )
                return (captured_status or 200), (captured_data or self._build_minimal_pdp_payload(None))

            has_api_price = bool(captured_data and self._pdp_booking_has_price(captured_data))
            if noncanonical_pdp_host and has_api_price:
                logger.warning(
                    "Playwright PDP API price ignored due to non-canonical host listing=%s",
                    listing_id,
                )
                has_api_price = False
                captured_data = self._build_minimal_pdp_payload(None)
            if not has_api_price:
                if noncanonical_pdp_host:
                    logger.info(
                        "Playwright PDP skipping DOM fallback on non-canonical host listing=%s",
                        listing_id,
                    )
                    return (captured_status or 200), self._build_minimal_pdp_payload(None)
                if captured_data is None:
                    logger.info(
                        "Playwright PDP API read missing listing=%s; switching to HTML read on same tab",
                        listing_id,
                    )
                else:
                    logger.info(
                        "Playwright PDP API price missing listing=%s; switching to HTML read on same tab",
                        listing_id,
                    )
                # Wait briefly for booking widget hydration before polling text.
                try:
                    await page.wait_for_function(
                        """() => {
                            const sels = [
                                '[data-testid="book-it-default"]',
                                '[data-testid="book-it-sidebar"]',
                                '[data-testid="price-block"]',
                                '[data-testid="book-it-price-breakdown"]',
                                '[data-section-id="BOOK_IT_SIDEBAR"]',
                                '[data-section-id="BOOK_IT_DEFAULT"]',
                            ];
                            for (const sel of sels) {
                                const el = document.querySelector(sel);
                                if (!el) continue;
                                const t = (el.innerText || el.textContent || '').trim();
                                if (t && /\\$\\s*\\d{2,}/.test(t)) return true;
                            }
                            return false;
                        }""",
                        timeout=8000,
                        polling=400,
                    )
                except Exception:
                    pass
                try:
                    latest_html = await page.content() or ""
                    if latest_html:
                        rendered_html = latest_html
                except Exception as exc:
                    logger.info(
                        "Playwright PDP post-hydration HTML refresh failed listing=%s: %s",
                        listing_id,
                        exc,
                    )
                if self._rendered_html_dates_unavailable(rendered_html):
                    logger.info(
                        "Playwright PDP post-hydration HTML shows unavailable dates listing=%s; returning captured payload (no price) for metadata",
                        listing_id,
                    )
                    return (captured_status or 200), (captured_data or self._build_minimal_pdp_payload(None))
                html_read_attempts = 1
                dom_price_text: Optional[str] = None
                for attempt in range(1, html_read_attempts + 1):
                    logger.info(
                        "Playwright PDP HTML read attempt %s/%s listing=%s",
                        attempt,
                        html_read_attempts,
                        listing_id,
                    )
                    dom_price_text = await self._read_dom_price_text(page, timeout_ms=1500)
                    if dom_price_text:
                        break
                    if attempt < html_read_attempts:
                        await page.wait_for_timeout(1500)
                if dom_price_text:
                    if captured_data is None:
                        captured_data = self._build_minimal_pdp_payload(dom_price_text)
                    else:
                        captured_data = self._inject_price_into_pdp_payload(captured_data, dom_price_text)
                    terminal_reason = "dom_price_fallback"
                    logger.info(
                        "Playwright PDP HTML read success listing=%s price_text=%s",
                        listing_id,
                        dom_price_text,
                    )
                else:
                    logger.info(
                        "Playwright PDP HTML read exhausted (%s attempt) listing=%s; skipping day with no price",
                        html_read_attempts,
                        listing_id,
                    )
                    if captured_data is None:
                        captured_data = self._build_minimal_pdp_payload(None)
            if terminal_reason is None:
                logger.warning(
                    "Playwright PDP had no price/unavailable terminal signal for listing=%s; returning latest payload.",
                    listing_id,
                )
            if captured_data.get("errors") and self._response_looks_auth_or_challenge_error(
                captured_status,
                captured_data,
            ):
                logger.info(
                    "Playwright PDP API returned auth/challenge-like errors listing=%s; returning no-price payload after HTML attempts",
                    listing_id,
                )
                return (captured_status or 200), self._build_minimal_pdp_payload(None)

            # Amenities fallback: some challenge/degraded PDP responses include only
            # booking + policy blocks (no amenity groups), while rendered HTML still
            # carries amenities under data.node.pdpPresentation.amenities.
            if not self._pdp_payload_has_amenity_groups(captured_data):
                html_amenities = self._extract_pdp_amenities_from_rendered_html(rendered_html)
                if isinstance(html_amenities, dict):
                    self._inject_pdp_presentation_amenities(captured_data, html_amenities)
                    preview_groups = html_amenities.get("previewAmenitiesGroups")
                    see_all_groups = html_amenities.get("seeAllAmenitiesGroups")
                    logger.info(
                        "Playwright PDP amenities fallback injected listing=%s preview_groups=%s see_all_groups=%s",
                        listing_id,
                        len(preview_groups) if isinstance(preview_groups, list) else 0,
                        len(see_all_groups) if isinstance(see_all_groups, list) else 0,
                    )
            return (captured_status or 200), captured_data
        finally:
            await self._close_capped_page(page)

    @staticmethod
    def _count_search_results(data: Dict[str, Any]) -> int:
        try:
            results = (
                (((data.get("data") or {}).get("presentation") or {}).get("staysSearch") or {})
                .get("results")
                or {}
            )
            sr = results.get("searchResults")
            if isinstance(sr, list):
                return len(sr)
        except Exception:
            pass
        return 0

    def _try_direct_search(
        self, overrides: Dict[str, Any]
    ) -> Optional[Tuple[int, Dict[str, Any]]]:
        """Attempt StaysSearch via direct HTTP; return None to signal browser fallback."""
        if not isinstance(self.captured_search_req, dict) and not isinstance(
            self.hardcoded_search_req, dict
        ):
            return None
        try:
            result = self.fetch_search_direct(overrides)
        except Exception as exc:
            logger.debug("[direct_search] raised; falling back to browser: %s", exc)
            return None
        if result is None:
            return None
        status_code, data = result
        if not isinstance(data, dict):
            return None
        if data.get("errors") and self._response_looks_auth_or_challenge_error(status_code, data):
            logger.info("[direct_search] challenge-like response; falling back to browser")
            return None
        count = self._count_search_results(data)
        if count <= 0:
            # A well-formed empty page at a deeper pagination offset is almost
            # always genuine end-of-results (the base page already succeeded via
            # this session), so trust it. A browser fallback here would cost a
            # multi-second navigation only to have Airbnb re-serve page 1.
            try:
                items_offset = int(overrides.get("itemsOffset") or 0)
            except (TypeError, ValueError):
                items_offset = 0
            if items_offset > 0 and not data.get("errors"):
                logger.info(
                    "[direct_search] empty results at itemsOffset=%s; treating as end of results",
                    items_offset,
                )
                return status_code, data
            logger.info("[direct_search] empty results; falling back to browser")
            return None
        logger.info(
            "[direct_search] StaysSearch served via direct HTTP status=%s results=%s",
            status_code,
            count,
        )
        return status_code, data

    def search_listings_with_overrides(
        self,
        overrides: Dict[str, Any],
        _already_retried: bool = False,
    ) -> Tuple[int, Dict[str, Any]]:
        """StaysSearch with overrides — direct HTTP replay first, browser fallback."""
        direct = self._try_direct_search(overrides)
        if direct is not None:
            return direct

        limiter = get_airbnb_rate_limiter()
        last_exc: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                with limiter.slot():
                    status_code, response_data = self._run_async(
                        self._search_via_browser(overrides),
                        op_name="search_listings_with_overrides",
                    )
                if response_data.get("errors") and self._response_looks_auth_or_challenge_error(status_code, response_data):
                    raise RuntimeError("Browser StaysSearch(with overrides) returned auth/challenge-like GraphQL error")
                return status_code, response_data
            except Exception as exc:
                last_exc = exc
                is_rate_limited = isinstance(exc, AirbnbRateLimited)
                logger.warning(
                    "Browser StaysSearch(with overrides) attempt %s/2 failed%s: %s",
                    attempt,
                    " (rate-limited)" if is_rate_limited else "",
                    exc,
                )
                if attempt < 2:
                    if is_rate_limited:
                        limiter.penalize()
                        time.sleep(2.0 + random.random() * 2.0)
                    else:
                        self._reset_browser_connection_after_failure(
                            exc,
                            op_name="search_listings_with_overrides",
                        )
                        time.sleep(1.0)
        raise RuntimeError(f"Playwright browser search(with overrides) failed after 2 attempts: {last_exc}")

    @staticmethod
    def _to_global_id(prefix: str, listing_id: str) -> str:
        return base64.b64encode(f"{prefix}:{listing_id}".encode("utf-8")).decode("utf-8")

    def _replace_listing_ids(self, payload: Any, listing_id: str, stay_gid: str, demand_gid: str):
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key == "demandStayListingId" and isinstance(value, str):
                    payload[key] = demand_gid
                elif key == "id" and isinstance(value, str) and value.startswith("U3RheUxpc3Rpbmc6"):
                    payload[key] = stay_gid
                else:
                    self._replace_listing_ids(value, listing_id, stay_gid, demand_gid)
        elif isinstance(payload, list):
            for item in payload:
                self._replace_listing_ids(item, listing_id, stay_gid, demand_gid)

    def fetch_pdp_price_direct(
        self,
        listing_id: str,
        checkin: str,
        checkout: str,
        adults: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """
        Direct HTTP POST to StaysPdpSections using session cookies — no browser tab opened.
        ~200-500ms per call vs 5-15s for browser navigation.
        Returns raw GraphQL response dict, or None on failure.
        """
        if not self.captured_pdp_req:
            return None
        # Authenticate with the logged-in browser's cookies (logged-out PDP
        # prices differ from logged-in ones).
        self._ensure_session_cookies_from_browser()
        tmpl = copy.deepcopy(self.captured_pdp_req)
        lid = str(listing_id)
        stay_gid = self._to_global_id("StayListing", lid)
        demand_gid = self._to_global_id("DemandStayListing", lid)
        self._replace_listing_ids(tmpl["post_data"], lid, stay_gid, demand_gid)
        pdp_req = tmpl["post_data"]["variables"]["pdpSectionsRequest"]
        pdp_req["checkIn"] = checkin
        pdp_req["checkOut"] = checkout
        pdp_req["adults"] = str(adults)
        date_range = tmpl["post_data"]["variables"].get("dateRange")
        if isinstance(date_range, dict):
            date_range["startDate"] = checkin
            date_range["endDate"] = checkout
        headers = dict(tmpl["headers"])
        safe_base = self._normalize_base_url(self.base_url)
        headers["referer"] = f"{safe_base}/rooms/{lid}?check_in={checkin}&check_out={checkout}"
        rand_str = lambda n: "".join(random.choices(string.ascii_lowercase + string.digits, k=n))
        headers["x-client-request-id"] = rand_str(30)
        headers["x-client-trace-id"] = rand_str(30)
        headers["x-airbnb-client-action-id"] = str(uuid.uuid4())
        p3_id = f"p3_{int(time.time())}_{rand_str(8)}"
        pdp_req["p3ImpressionId"] = p3_id
        if "p3ImpressionId" in tmpl["post_data"]["variables"]:
            tmpl["post_data"]["variables"]["p3ImpressionId"] = p3_id
        limiter = get_airbnb_rate_limiter()
        for attempt in range(3):
            try:
                with limiter.slot():
                    resp = self.session.post(
                        tmpl["url"],
                        headers=headers,
                        json=tmpl["post_data"],
                        timeout=8,
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        return data
                    return None
                if resp.status_code in (503, 429):
                    limiter.penalize()
                    wait = 1.0 + attempt * 1.5 + random.random()
                    logger.debug(
                        "[direct_pdp] listing=%s checkin=%s HTTP %s, retry in %.1fs (attempt %s/3)",
                        lid, checkin, resp.status_code, wait, attempt + 1,
                    )
                    time.sleep(wait)
                    continue
                logger.debug(
                    "[direct_pdp] listing=%s checkin=%s HTTP %s", lid, checkin, resp.status_code
                )
                return None
            except Exception as exc:
                logger.debug(
                    "[direct_pdp] listing=%s checkin=%s error: %s", lid, checkin, str(exc)[:120]
                )
                if attempt < 2:
                    time.sleep(0.5 + attempt * 0.5)
        return None

    def _apply_search_overrides_to_template(
        self, post_data: Dict[str, Any], overrides: Dict[str, Any]
    ) -> None:
        """
        Mutate a captured StaysSearch GraphQL payload in place to reflect the
        requested date window / location / pagination, reusing the rawParams
        helpers. Only fields present in ``overrides`` are changed; everything else
        is inherited from the captured live request.
        """
        variables = post_data.get("variables")
        if not isinstance(variables, dict):
            return
        ov = overrides or {}

        raw_targets: List[list] = []
        sr = variables.get("staysSearchRequest")
        if isinstance(sr, dict) and isinstance(sr.get("rawParams"), list):
            raw_targets.append(sr["rawParams"])
        mr = variables.get("staysMapSearchRequestV2")
        if isinstance(mr, dict) and isinstance(mr.get("rawParams"), list):
            raw_targets.append(mr["rawParams"])

        def _set_all(name: str, values: list) -> None:
            for rp in raw_targets:
                self._set_raw_param(rp, name, values)

        if ov.get("checkin"):
            _set_all("checkin", [str(ov["checkin"])])
        if ov.get("checkout"):
            _set_all("checkout", [str(ov["checkout"])])
        adults = ov.get("adults", ov.get("guests"))
        if adults is not None:
            try:
                _set_all("adults", [str(int(adults))])
            except (TypeError, ValueError):
                pass
        query = ov.get("query") or ov.get("locationSearch") or ov.get("location")
        if query:
            _set_all("query", [str(query)])
        if ov.get("itemsPerGrid") is not None:
            try:
                _set_all("itemsPerGrid", [str(int(ov["itemsPerGrid"]))])
            except (TypeError, ValueError):
                pass
        if ov.get("itemsOffset") is not None:
            try:
                _set_all("itemsOffset", [str(int(ov["itemsOffset"]))])
            except (TypeError, ValueError):
                pass
        if ov.get("searchByMap"):
            _set_all("searchByMap", ["true"])
            for key in ("neLat", "neLng", "swLat", "swLng"):
                if ov.get(key) is not None:
                    _set_all(key, [str(ov[key])])

        amenity_ids = ov.get("amenityIds")
        if amenity_ids and isinstance(amenity_ids, list):
            _set_all("amenities", [str(aid) for aid in amenity_ids if aid is not None])

        # ── Generalize a captured/hardcoded template to the requested search ──
        # A recorded template carries the location/session/pagination state of
        # wherever it was captured (a specific placeId, a page-N cursor, flexible
        # /monthly-date params). Airbnb resolves location from placeId over the
        # query string, so without stripping these every replay would echo the
        # captured city/page regardless of the overrides above. Drop the pinned
        # params so the search is driven purely by query + dates + map bounds.
        for name in (
            "placeId",
            "federatedSearchSessionId",
            "monthlyStartDate",
            "monthlyLength",
            "monthlyEndDate",
            "flexibleTripLengths",
        ):
            for rp in raw_targets:
                self._remove_raw_param(rp, name)

        # Realign the pagination cursor. Modern StaysSearch paginates by an
        # opaque base64 cursor (not the itemsOffset rawParam), so encode the
        # requested offset into a fresh cursor: None for the first page, else the
        # captured cursor's shape with the new items_offset.
        new_cursor: Optional[str] = None
        offset_val = ov.get("itemsOffset")
        if offset_val is not None:
            try:
                cursor_payload = {
                    "section_offset": 0,
                    "items_offset": int(offset_val),
                    "version": 1,
                }
                new_cursor = base64.b64encode(
                    json.dumps(cursor_payload, separators=(",", ":")).encode("utf-8")
                ).decode("utf-8")
            except (TypeError, ValueError):
                new_cursor = None
        for key in ("staysSearchRequest", "staysMapSearchRequestV2"):
            node = variables.get(key)
            if isinstance(node, dict) and "cursor" in node:
                node["cursor"] = new_cursor

        # Honor the disable-map-search toggle the same way the browser path would.
        self._apply_disable_map_search({"variables": variables})

    def fetch_search_direct(
        self, overrides: Dict[str, Any]
    ) -> Optional[Tuple[int, Dict[str, Any]]]:
        """
        Direct HTTP POST to StaysSearch — no browser tab. Returns (status, data)
        on success, or None when no template is available or the request failed
        (caller should then fall back to the browser path).

        Template selection mirrors the StaysPdpSections direct path: prefer a
        live browser-captured request (freshest persisted-query hash + headers),
        otherwise use the hardcoded template so comparable search works
        browser-free from a cold start (stl-scraper technique).
        """
        tmpl = self.captured_search_req or self.hardcoded_search_req
        if not isinstance(tmpl, dict) or not tmpl.get("url") or not isinstance(
            tmpl.get("post_data"), dict
        ):
            return None
        # Authenticate the replay with the logged-in browser's cookies so the
        # search prices match what a logged-in guest sees.
        self._ensure_session_cookies_from_browser()
        tmpl = copy.deepcopy(tmpl)
        # Pin currency=USD and an English locale on the replayed StaysSearch URL
        # so results stay priced in USD and returned in English even if the
        # captured/cached template predates these settings.
        tmpl["url"] = self._force_url_query_params(
            str(tmpl.get("url") or ""), currency="USD", locale=self.locale
        )
        post_data = tmpl["post_data"]
        try:
            self._apply_search_overrides_to_template(post_data, overrides)
        except Exception as exc:
            logger.debug("[direct_search] override application failed: %s", exc)
            return None

        headers = self._sanitize_captured_headers(dict(tmpl.get("headers") or {}))
        # Authenticate with the public web api-key header (stl-scraper technique)
        # so StaysSearch succeeds over plain HTTP without a browser tab, even when
        # the captured/hardcoded headers are missing or stale.
        headers["x-airbnb-api-key"] = PUBLIC_AIRBNB_API_KEY
        rand_str = lambda n: "".join(random.choices(string.ascii_lowercase + string.digits, k=n))
        headers["x-client-request-id"] = rand_str(30)
        headers["x-client-trace-id"] = rand_str(30)
        headers["x-airbnb-client-action-id"] = str(uuid.uuid4())

        limiter = get_airbnb_rate_limiter()
        for attempt in range(3):
            try:
                with limiter.slot():
                    resp = self.session.post(
                        tmpl["url"],
                        headers=headers,
                        json=post_data,
                        timeout=12,
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        return resp.status_code, data
                    return None
                if resp.status_code in (503, 429):
                    limiter.penalize()
                    wait = 1.0 + attempt * 1.5 + random.random()
                    logger.debug(
                        "[direct_search] HTTP %s, retry in %.1fs (attempt %s/3)",
                        resp.status_code, wait, attempt + 1,
                    )
                    time.sleep(wait)
                    continue
                logger.debug("[direct_search] HTTP %s", resp.status_code)
                return None
            except Exception as exc:
                logger.debug("[direct_search] error: %s", str(exc)[:120])
                if attempt < 2:
                    time.sleep(0.5 + attempt * 0.5)
        return None

    def copy_session_from(self, source: "PlaywrightScraper") -> None:
        """Copy session cookies and PDP template from another scraper instance."""
        with source._session_cookie_lock:
            src_cookies = list(source.session.cookies)
            src_pdp = copy.deepcopy(source.captured_pdp_req)
        with self._session_cookie_lock:
            self.session.cookies.clear()
            for c in src_cookies:
                self.session.cookies.set(
                    c.name, c.value, domain=c.domain, path=c.path,
                )
        self.captured_pdp_req = src_pdp

    def _resolve_pdp_window(
        self,
        checkin: Optional[str],
        checkout: Optional[str],
        adults: Optional[int],
    ) -> Tuple[str, str, int]:
        eff_checkin = checkin or self.config.get("CHECKIN", "")
        eff_checkout = checkout or self.config.get("CHECKOUT", "")
        try:
            eff_adults = int(adults if adults is not None else self.config.get("ADULTS", 1))
        except Exception:
            eff_adults = 1
        return eff_checkin, eff_checkout, eff_adults

    def _pdp_payload_is_usable(
        self,
        data: Any,
        listing_id: str,
        *,
        require_price: bool,
        log_tag: str,
    ) -> bool:
        """
        Shared acceptance check for a raw StaysPdpSections payload obtained
        outside the browser (airbnb_pdp_api or the captured-template replay).

        require_price=True  -> only accept payloads that carry a bookable price
                               or are explicitly dates-unavailable; otherwise
                               defer to the browser so its DOM price fallback can
                               recover prices the API omits (price paths).
        require_price=False -> accept any structurally-complete PDP payload
                               regardless of price (metadata/enrichment paths).
        """
        if not isinstance(data, dict):
            return False
        if data.get("errors") and self._response_looks_auth_or_challenge_error(200, data):
            logger.info(
                "[%s] listing=%s challenge-like response; falling back",
                log_tag,
                listing_id,
            )
            return False
        if not self._extract_pdp_sections(data) and not self._pdp_payload_has_amenity_groups(data):
            # Empty/degraded payload — not a real PDP response.
            return False
        if require_price:
            return bool(self._pdp_dates_unavailable(data) or self._pdp_booking_has_price(data))
        return True

    def _get_pdp_api_client(self) -> AirbnbPdpClient:
        """
        Lazily build the standalone airbnb_pdp_api client (own requests.Session,
        Airbnb's public web api-key, no CDP browser dependency). Reused for the
        life of this PlaywrightScraper instance; picks up a fresher persisted-query
        hash if one has been learned from real browser traffic in the meantime.
        """
        if self._pdp_api_client is None:
            # Match fetch_pdp_price_direct's budget (timeout=8) rather than the
            # library default (30s) — this client is tried FIRST in every PDP
            # cascade, including the benchmark hot path that's designed to fail
            # fast and interpolate rather than wait out a slow response.
            self._pdp_api_client = AirbnbPdpClient(
                locale=self.locale, currency=self.currency, timeout=8
            )
        if (
            self._pdp_api_hash_override
            and self._pdp_api_client.persisted_query_hash != self._pdp_api_hash_override
        ):
            self._pdp_api_client.persisted_query_hash = self._pdp_api_hash_override
        return self._pdp_api_client

    def _try_pdp_api_listing_details(
        self,
        listing_id: str,
        checkin: str,
        checkout: str,
        adults: int,
        *,
        require_price: bool,
    ) -> Optional[Dict[str, Any]]:
        """
        Satisfy a PDP detail fetch via the standalone airbnb_pdp_api crawler —
        the prioritized StaysPdpSections method. Works browser-free from a cold
        start (public web api-key, no captured template/cookies required), so it
        is tried before the captured-template replay and full browser capture
        below. Returns the raw GraphQL payload, or None to signal the caller
        should fall through to the next method in the chain.
        """
        client = self._get_pdp_api_client()
        # Best-effort cookie parity with the rest of the pipeline (Airbnb prices
        # logged-in and logged-out sessions differently). Only warms once per
        # scraper instance and reuses the already-running CDP browser, so this
        # is cheap when available and simply no-ops otherwise.
        self._ensure_session_cookies_from_browser()
        if len(self.session.cookies) > 0:
            for c in self.session.cookies:
                client.session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        try:
            data = client.fetch(
                str(listing_id),
                check_in=checkin or None,
                check_out=checkout or None,
                adults=max(1, int(adults or 1)),
            )
        except (PdpApiAntiBotError, PdpApiStaleHashError) as exc:
            logger.debug(
                "[pdp_api] listing=%s fetch failed (%s); falling back",
                listing_id,
                exc,
            )
            return None
        except Exception as exc:
            logger.debug(
                "[pdp_api] listing=%s fetch raised; falling back: %s",
                listing_id,
                str(exc)[:160],
            )
            return None
        if not self._pdp_payload_is_usable(data, listing_id, require_price=require_price, log_tag="pdp_api"):
            return None
        logger.info(
            "[pdp_api] listing=%s served via airbnb pdp api (%s path)",
            listing_id,
            "price" if require_price else "detail",
        )
        return data

    def fetch_pdp_payload_prioritized(
        self,
        listing_id: str,
        checkin: str,
        checkout: str,
        adults: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """
        PDP payload for hot paths that must stay browser-free (e.g. benchmark
        nightly pricing, where a failed fetch is simply treated as
        price-unavailable rather than paying for a 10-15s browser navigation).

        airbnb_pdp_api first, captured-template direct replay second; never
        falls back to the browser. Returns None if both fail.
        """
        pdp_api = self._try_pdp_api_listing_details(
            str(listing_id), checkin, checkout, adults, require_price=True
        )
        if pdp_api is not None:
            return pdp_api
        return self.fetch_pdp_price_direct(
            listing_id=str(listing_id), checkin=checkin, checkout=checkout, adults=adults
        )

    def _try_direct_listing_details(
        self,
        listing_id: str,
        checkin: str,
        checkout: str,
        adults: int,
        *,
        require_price: bool,
    ) -> Optional[Dict[str, Any]]:
        """
        Satisfy a PDP detail fetch via direct HTTP replay of the captured
        StaysPdpSections request (no browser tab, ~300ms vs several seconds of
        browser navigation). Returns the raw GraphQL payload when it is a usable
        PDP response, or None to signal the caller should fall back to the
        browser path.
        """
        if not self.captured_pdp_req:
            return None
        # Direct replay relies on authenticated session cookies; without them the
        # request would be challenged, so let the browser path handle it instead.
        # Warm cookies from the logged-in CDP browser first so the session is
        # populated (and the price reflects the logged-in guest).
        self._ensure_session_cookies_from_browser()
        if len(self.session.cookies) == 0:
            return None
        try:
            data = self.fetch_pdp_price_direct(
                listing_id=str(listing_id),
                checkin=checkin,
                checkout=checkout,
                adults=adults,
            )
        except Exception as exc:
            logger.debug(
                "[direct_pdp] listing=%s replay raised; browser fallback: %s",
                listing_id,
                str(exc)[:120],
            )
            return None
        if not self._pdp_payload_is_usable(data, listing_id, require_price=require_price, log_tag="direct_pdp"):
            return None
        logger.info(
            "[direct_pdp] listing=%s served via direct HTTP (%s path)",
            listing_id,
            "price" if require_price else "detail",
        )
        return data

    def get_listing_details(
        self,
        listing_id: str,
        checkin: Optional[str] = None,
        checkout: Optional[str] = None,
        adults: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        PDP detail fetch — airbnb_pdp_api first, captured-template direct
        replay second, browser capture fallback last.

        The price-aware gate defers no-price/bookable responses further down
        the chain so the browser's DOM price fallback can recover prices the
        StaysPdpSections API omits.
        """
        eff_checkin, eff_checkout, eff_adults = self._resolve_pdp_window(
            checkin, checkout, adults
        )
        pdp_api = self._try_pdp_api_listing_details(
            str(listing_id), eff_checkin, eff_checkout, eff_adults, require_price=True
        )
        if pdp_api is not None:
            return pdp_api
        direct = self._try_direct_listing_details(
            str(listing_id), eff_checkin, eff_checkout, eff_adults, require_price=True
        )
        if direct is not None:
            return direct
        return self._get_listing_details_browser(listing_id, checkin, checkout, adults)

    def _fetch_listing_page_html_direct(self, listing_id: str) -> Optional[str]:
        """
        Direct HTTP GET to the Airbnb listing page to extract server-rendered HTML.

        Airbnb SSR embeds the full amenity list (previewAmenitiesGroups +
        seeAllAmenitiesGroups) inside <script type="application/json"> blocks as
        data.node.pdpPresentation.amenities.  A plain session.get is ~200-500ms
        versus 5-15s for a full browser navigation, making it viable as a fast
        amenity enrichment step after a direct GraphQL PDP fetch.

        Returns the raw HTML string, or None on any error.
        """
        url = f"{self.base_url}/rooms/{listing_id}"
        try:
            resp = self.session.get(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/121.0.0.0 Safari/537.36"
                    ),
                },
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.text
            logger.debug(
                "[direct_pdp_html] listing=%s HTTP %s", listing_id, resp.status_code
            )
        except Exception as exc:
            logger.debug(
                "[direct_pdp_html] listing=%s GET failed: %s", listing_id, str(exc)[:120]
            )
        return None

    def _enrich_fast_detail_amenities(self, data: Dict[str, Any], listing_id: str, *, log_tag: str) -> None:
        """
        When a non-browser PDP payload lacks amenity groups (the normal case for
        the booking-section-only captured template, and possible for the public
        airbnb_pdp_api template too), fall back to a direct HTTP GET of the
        listing page HTML to extract the SSR-embedded amenity data. Mutates
        ``data`` in place.
        """
        if self._pdp_payload_has_amenity_groups(data):
            return
        html = self._fetch_listing_page_html_direct(str(listing_id))
        if not html:
            return
        html_amenities = self._extract_pdp_amenities_from_rendered_html(html)
        if isinstance(html_amenities, dict):
            self._inject_pdp_presentation_amenities(data, html_amenities)
            preview = html_amenities.get("previewAmenitiesGroups")
            see_all = html_amenities.get("seeAllAmenitiesGroups")
            logger.info(
                "[%s] listing=%s amenities enriched from page HTML "
                "preview_groups=%s see_all_groups=%s",
                log_tag,
                listing_id,
                len(preview) if isinstance(preview, list) else 0,
                len(see_all) if isinstance(see_all, list) else 0,
            )

    def get_listing_details_fast(
        self,
        listing_id: str,
        checkin: Optional[str] = None,
        checkout: Optional[str] = None,
        adults: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Metadata-oriented PDP fetch — airbnb_pdp_api first, captured-template
        direct replay second (both accept no-price payloads), browser capture
        fallback last. Used for comp/target structural detail where
        amenities/capacity/baths/type matter but the booking price does not, so
        we never pay for a browser navigation just to recover a price.
        """
        eff_checkin, eff_checkout, eff_adults = self._resolve_pdp_window(
            checkin, checkout, adults
        )
        pdp_api = self._try_pdp_api_listing_details(
            str(listing_id), eff_checkin, eff_checkout, eff_adults, require_price=False
        )
        if pdp_api is not None:
            self._enrich_fast_detail_amenities(pdp_api, str(listing_id), log_tag="pdp_api")
            return pdp_api
        direct = self._try_direct_listing_details(
            str(listing_id), eff_checkin, eff_checkout, eff_adults, require_price=False
        )
        if direct is not None:
            self._enrich_fast_detail_amenities(direct, str(listing_id), log_tag="direct_pdp")
            return direct
        return self._get_listing_details_browser(listing_id, checkin, checkout, adults)

    def _get_listing_details_browser(
        self,
        listing_id: str,
        checkin: Optional[str] = None,
        checkout: Optional[str] = None,
        adults: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Browser-only PDP capture (no request replay/session.post)."""
        def _is_cdp_connect_error(err: Exception) -> bool:
            msg = str(err or "").lower()
            return (
                "connect_over_cdp" in msg
                or "retrieving websocket url" in msg
                or "ws://127.0.0.1:9222/devtools/browser" in msg
            )

        effective_checkin = checkin or self.config.get("CHECKIN", "")
        effective_checkout = checkout or self.config.get("CHECKOUT", "")
        effective_adults = int(adults if adults is not None else self.config.get("ADULTS", 1))
        # Ordinary PDP failures remain single-shot. A dead browser handle gets
        # one reconnect attempt so later work is not sent into a stale context.
        max_attempts = 2
        try:
            pdp_attempt_timeout_seconds = max(
                5.0, float(self.config.get("PDP_BROWSER_ATTEMPT_TIMEOUT_SECONDS", 45))
            )
        except Exception:
            pdp_attempt_timeout_seconds = 45.0
        limiter = get_airbnb_rate_limiter()
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                with limiter.slot():
                    _, response_data = self._run_async(
                        self._get_listing_details_via_browser(
                            listing_id=str(listing_id),
                            checkin=effective_checkin,
                            checkout=effective_checkout,
                            adults=effective_adults,
                        ),
                        op_name="get_listing_details",
                        timeout_seconds=pdp_attempt_timeout_seconds,
                    )
                return response_data
            except Exception as exc:
                last_exc = exc
                if isinstance(exc, AirbnbRateLimited):
                    limiter.penalize()
                    logger.warning(
                        "Browser PDP attempt %s rate-limited for listing=%s: %s",
                        attempt,
                        listing_id,
                        exc,
                    )
                    if attempt < max_attempts:
                        time.sleep(2.0 + random.random() * 2.0)
                        continue
                    break
                will_retry = (
                    attempt < max_attempts
                    and self._reset_browser_connection_after_failure(
                        exc,
                        op_name="get_listing_details",
                    )
                )
                logger.warning(
                    "Browser PDP attempt %s failed for listing=%s retrying_after_browser_reset=%s: %s",
                    attempt,
                    listing_id,
                    will_retry,
                    exc,
                )
                if _is_cdp_connect_error(exc):
                    logger.warning(
                        "Browser PDP fast-fail on CDP connect error for listing=%s (skip retry)",
                        listing_id,
                    )
                    break
                if not will_retry:
                    break
        raise RuntimeError(
            f"Playwright browser PDP failed for listing={listing_id}: {last_exc}"
        )
