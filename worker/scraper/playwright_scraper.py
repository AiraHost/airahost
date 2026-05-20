import base64
import asyncio
import copy
import json
import logging
import os
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlparse

import requests
from playwright.async_api import async_playwright
from worker.scraper.stayspdp_template import HARDCODED_STAYS_PDP_TEMPLATE

logger = logging.getLogger(__name__)


class PlaywrightScraper:
    """Legacy Playwright capture/replay strategy restored from pre-deepbnb history."""
    _refresh_lock = threading.Lock()
    _tab_gate_lock = threading.Lock()
    _tab_gate = None
    _tab_limit = 5
    _open_tab_count = 0
    _CANONICAL_AIRBNB_HOST_RE = re.compile(
        r"^(?:[a-z]{2,5}(?:-[a-z]{1,5})?\.)?airbnb\.com$",
        re.IGNORECASE,
    )

    def __init__(self, config: dict):
        self.config = config
        self.base_url = self._normalize_base_url(self.config.get("AIRBNB_BASE_URL", "https://www.airbnb.com"))
        self.locale = str(self.config.get("LOCALE", os.getenv("AIRBNB_LOCALE", "en-CA")) or "en-CA")
        self.currency = str(self.config.get("CURRENCY", os.getenv("AIRBNB_CURRENCY", "CAD")) or "CAD")
        self.session = requests.Session()
        self.captured_search_req = None
        self.captured_pdp_req = None
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
                str(os.getenv("AIRBNB_USE_HARDCODED_STAYSPDP_TEMPLATE", "0")).strip().lower()
                in ("1", "true", "yes", "on")
            )
        else:
            self.use_hardcoded_stayspdp_template = bool(hardcoded_pdp_cfg)
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
        self._ensure_tab_gate()
        if self.use_hardcoded_stayspdp_template:
            self._load_hardcoded_stayspdp_template()

    @staticmethod
    def _normalize_base_url(raw_base: Any) -> str:
        raw = str(raw_base or "").strip()
        if not raw:
            return "https://www.airbnb.com"
        try:
            parsed = urlparse(raw)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                host = str(parsed.hostname or "").strip().lower()
                if re.fullmatch(r"(?:[a-z]{2,5}(?:-[a-z]{1,5})?\.)?airbnb\.(?:com|ca)", host):
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

    @classmethod
    def _ensure_tab_gate(cls) -> None:
        with cls._tab_gate_lock:
            if cls._tab_gate is not None:
                return
            raw_limit = os.getenv("AIRBNB_PLAYWRIGHT_MAX_TABS", "5")
            try:
                parsed_limit = int(str(raw_limit).strip())
            except Exception:
                parsed_limit = 5
            # Hard safety ceiling: never allow more than 5 concurrent tabs.
            cls._tab_limit = max(1, min(parsed_limit, 5))
            cls._tab_gate = threading.BoundedSemaphore(cls._tab_limit)

    @classmethod
    async def _acquire_tab_slot(cls, timeout_seconds: float = 120.0) -> None:
        cls._ensure_tab_gate()
        assert cls._tab_gate is not None
        deadline = time.monotonic() + timeout_seconds
        while True:
            if cls._tab_gate.acquire(blocking=False):
                with cls._tab_gate_lock:
                    cls._open_tab_count += 1
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Timed out waiting for Playwright tab slot (limit={cls._tab_limit})"
                )
            await asyncio.sleep(0.05)

    @classmethod
    def _release_tab_slot(cls) -> None:
        with cls._tab_gate_lock:
            cls._open_tab_count = max(0, cls._open_tab_count - 1)
        if cls._tab_gate is not None:
            try:
                cls._tab_gate.release()
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
    async def _goto_with_logging(page, url: str, *, wait_until: str, timeout: int, label: str):
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
        return self._run_async(_run(), op_name="browse_url_html")

    def _load_hardcoded_stayspdp_template(self) -> bool:
        if not isinstance(HARDCODED_STAYS_PDP_TEMPLATE, dict):
            return False
        required = ("url", "headers", "post_data")
        if not all(key in HARDCODED_STAYS_PDP_TEMPLATE for key in required):
            return False
        template = copy.deepcopy(HARDCODED_STAYS_PDP_TEMPLATE)
        safe_base = self._normalize_base_url(self.base_url)
        try:
            raw_url = str(template.get("url") or "").strip()
            if raw_url:
                parsed = urlparse(raw_url)
                query = f"?{parsed.query}" if parsed.query else ""
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
        self.captured_pdp_req = template
        logger.info("Loaded hardcoded StaysPdpSections template.")
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
        clone.session = requests.Session()
        clone.captured_search_req = copy.deepcopy(self.captured_search_req)
        clone.captured_pdp_req = copy.deepcopy(self.captured_pdp_req)
        clone.disable_map_search = self.disable_map_search
        clone.enable_ai_search = self.enable_ai_search
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
        last_exc: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                status_code, response_data = self._run_async(
                    self._search_via_browser(),
                    op_name="search_listings",
                )
                if response_data.get("errors") and self._response_looks_auth_or_challenge_error(status_code, response_data):
                    raise RuntimeError("Browser StaysSearch returned auth/challenge-like GraphQL error")
                return status_code, response_data
            except Exception as exc:
                last_exc = exc
                logger.warning("Browser StaysSearch attempt %s/2 failed: %s", attempt, exc)
                if attempt < 2:
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
        logger.info(
            "Connecting Playwright to existing browser via CDP: %s [thread=%s]",
            self._cdp_url,
            threading.get_ident(),
        )
        self._browser = await self._pw.chromium.connect_over_cdp(self._cdp_url, timeout=15000)
        return self._browser

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
            latest_nav_html: str = ""
            latest_nav_url: str = ""
            challenge_detected = False
            challenge_reason = ""
            response_tasks: set[asyncio.Task] = set()

            async def _handle_response(resp):
                nonlocal captured_status, captured_data, captured_url, saw_stayssearch_response
                try:
                    if captured_data is not None:
                        return
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
                    captured_status = int(resp.status)
                    payload = await resp.json()
                    if isinstance(payload, dict):
                        captured_data = payload
                        captured_url = resp_url
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
                        if has_price:
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
            logger.info(
                "Playwright PDP nav result listing=%s final_url=%s status=%s html_len=%s",
                listing_id,
                str((nav or {}).get("final_url") or ""),
                str((nav or {}).get("status")),
                len(str((nav or {}).get("html") or "")),
            )
            if str((nav or {}).get("final_url") or "").lower().startswith("about:blank"):
                raise RuntimeError(f"Playwright PDP landed on about:blank for listing={listing_id}")
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

            if terminal_reason is not None:
                # Hold very briefly after terminal detection so last payload
                # updates can settle before closing the tab.
                await page.wait_for_timeout(100)

            if response_tasks:
                await asyncio.gather(*list(response_tasks), return_exceptions=True)

            await self._sync_context_cookies_into_session(context)
            self._save_cached_state()

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
                dom_price_text: Optional[str] = None
                for attempt in range(1, 6):
                    logger.info(
                        "Playwright PDP HTML read attempt %s/5 listing=%s",
                        attempt,
                        listing_id,
                    )
                    dom_price_text = await self._read_dom_price_text(page, timeout_ms=1500)
                    if dom_price_text:
                        break
                    if attempt < 5:
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
                        "Playwright PDP HTML read exhausted (5 attempts) listing=%s; skipping day with no price",
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

    def search_listings_with_overrides(
        self,
        overrides: Dict[str, Any],
        _already_retried: bool = False,
    ) -> Tuple[int, Dict[str, Any]]:
        """Browser-only StaysSearch with overrides (no API replay)."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                status_code, response_data = self._run_async(
                    self._search_via_browser(overrides),
                    op_name="search_listings_with_overrides",
                )
                if response_data.get("errors") and self._response_looks_auth_or_challenge_error(status_code, response_data):
                    raise RuntimeError("Browser StaysSearch(with overrides) returned auth/challenge-like GraphQL error")
                return status_code, response_data
            except Exception as exc:
                last_exc = exc
                logger.warning("Browser StaysSearch(with overrides) attempt %s/2 failed: %s", attempt, exc)
                if attempt < 2:
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

    def get_listing_details(
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
        # PDP is single-shot by design: callers handle fallback after one failure.
        max_attempts = 1
        try:
            pdp_attempt_timeout_seconds = max(
                5.0, float(self.config.get("PDP_BROWSER_ATTEMPT_TIMEOUT_SECONDS", 45))
            )
        except Exception:
            pdp_attempt_timeout_seconds = 45.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
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
                logger.warning(
                    "Browser PDP attempt %s/%s failed for listing=%s: %s",
                    attempt,
                    max_attempts,
                    listing_id,
                    exc,
                )
                if _is_cdp_connect_error(exc):
                    logger.warning(
                        "Browser PDP fast-fail on CDP connect error for listing=%s (skip retry)",
                        listing_id,
                    )
                    break
        raise RuntimeError(
            f"Playwright browser PDP failed after {max_attempts} attempts for listing={listing_id}: {last_exc}"
        )
