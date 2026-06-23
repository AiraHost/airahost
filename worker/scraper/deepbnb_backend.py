import base64
import copy
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from worker.scraper.parsers_deepbnb import (
    parse_deepbnb_pdp_to_stayspdp_payload,
    parse_deepbnb_search_to_stayssearch_payload,
)
from worker.scraper.scraper_errors import ScraperForbiddenError
from worker.scraper.stayspdp_template import HARDCODED_STAYS_PDP_TEMPLATE

logger = logging.getLogger(__name__)

_DEFAULT_STAYS_PDP_SECTION_IDS = [
    "BOOK_IT_FLOATING_FOOTER",
    "BOOK_IT_SIDEBAR",
    "BOOK_IT_NAV",
    "OVERVIEW_DEFAULT_V2",
    "GUEST_FAVORITE_BANNER",
    "HIGHLIGHTS_DEFAULT",
    "AMENITIES_DEFAULT",
    "POLICIES_DEFAULT",
]


class DeepBnbBackend:
    """
    Standalone backend adapter for direct Airbnb API fetches.

    This adapter is intentionally separated from AirbnbClient replay logic.
    It builds GraphQL requests directly and then converts responses into the
    existing parser-friendly payload shape used by this worker.
    """

    def __init__(self, config: Dict[str, Any], base_url: str, session: Optional[requests.Session] = None):
        self.config = config
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()

        # Configure connection pooling for optimal performance
        retry_strategy = Retry(
            total=2,
            connect=2,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=20,
            pool_maxsize=20,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.locale = str(config.get("LOCALE", os.getenv("AIRBNB_LOCALE", "en-CA")) or "en-CA")
        self.currency = str(config.get("CURRENCY", os.getenv("AIRBNB_CURRENCY", "USD")) or "USD")
        self.api_key = str(
            config.get("AIRBNB_API_KEY")
            or os.getenv("AIRBNB_API_KEY")
            or "d306zoyjsyarp7ifhu67rjxn52tv0t20"
        )
        # Keep hashes configurable because Airbnb rotates them frequently.
        self.stays_search_hash = str(
            config.get("AIRBNB_STAYSSEARCH_HASH")
            or os.getenv("AIRBNB_STAYSSEARCH_HASH")
            or "753d97c7b19a1a402d2fa63882ff4d6802004d11f2499647deef923a19a1641a"
        )
        self.pdp_platform_hash = str(
            config.get("AIRBNB_PDPLATFORM_HASH")
            or os.getenv("AIRBNB_PDPLATFORM_HASH")
            or "625a4ba56ba72f8e8585d60078eb95ea0030428cac8772fde09de073da1bcdd0"
        )
        self.stays_pdp_sections_hash = str(
            config.get("AIRBNB_STAYSPDPSECTIONS_HASH")
            or os.getenv("AIRBNB_STAYSPDPSECTIONS_HASH")
            or "f81911bce044e58b7c2ed3f44b3ca576af3c08988ce2c0b3ee0d6d444cfd25a1"
        )

    def _headers(self) -> Dict[str, str]:
        return {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "x-airbnb-api-key": self.api_key,
            "x-airbnb-graphql-platform": "web",
            "x-airbnb-graphql-platform-client": "minimalist-niobe",
            "x-csrf-without-token": "1",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            ),
        }

    def _stays_search_url(self) -> str:
        return (
            f"{self.base_url}/api/v3/StaysSearch/{self.stays_search_hash}"
            f"?operationName=StaysSearch&locale={self.locale}&currency={self.currency}"
        )

    def _pdp_platform_url(self) -> str:
        return (
            f"{self.base_url}/api/v3/PdpPlatformSections/{self.pdp_platform_hash}"
            f"?operationName=PdpPlatformSections&locale={self.locale}&currency={self.currency}"
        )

    def _stays_pdp_sections_url(self) -> str:
        return (
            f"{self.base_url}/api/v3/StaysPdpSections/{self.stays_pdp_sections_hash}"
            f"?operationName=StaysPdpSections&locale={self.locale}&currency={self.currency}"
        )

    @staticmethod
    def _first_qs_value(values: Dict[str, list[str]], *keys: str) -> Optional[str]:
        for key in keys:
            vals = values.get(key)
            if isinstance(vals, list) and vals:
                val = str(vals[0] or "").strip()
                if val:
                    return val
        return None

    @classmethod
    def _overrides_from_search_url(cls, search_url: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if not isinstance(search_url, str) or not search_url.strip():
            return out
        try:
            parsed = urlparse(search_url)
            qs = parse_qs(parsed.query or "", keep_blank_values=False)
        except Exception:
            return out

        # Preferred query text from explicit query param.
        query = cls._first_qs_value(qs, "query")
        if not query:
            # Fallback to /s/<slug>/homes path segment.
            parts = [p for p in (parsed.path or "").split("/") if p]
            if len(parts) >= 2 and parts[0] == "s":
                slug = unquote(parts[1])
                slug = re.sub(r"\*+", "", slug)
                slug = slug.replace("--", ",")
                slug = re.sub(r"\s*,\s*", ", ", slug).strip()
                query = slug
        if query:
            out["query"] = query

        checkin = cls._first_qs_value(qs, "checkin", "check_in")
        if checkin:
            out["checkin"] = checkin
        checkout = cls._first_qs_value(qs, "checkout", "check_out")
        if checkout:
            out["checkout"] = checkout

        adults = cls._first_qs_value(qs, "adults")
        if adults:
            out["adults"] = adults

        center_lat = cls._first_qs_value(qs, "center_lat", "centerLat", "lat")
        if center_lat:
            out["centerLat"] = center_lat
        center_lng = cls._first_qs_value(qs, "center_lng", "centerLng", "lng")
        if center_lng:
            out["centerLng"] = center_lng

        place_id = cls._first_qs_value(qs, "place_id", "placeId")
        if place_id:
            out["placeId"] = place_id

        items_per_grid = cls._first_qs_value(qs, "items_per_grid", "itemsPerGrid")
        if items_per_grid:
            out["itemsPerGrid"] = items_per_grid
        items_offset = cls._first_qs_value(qs, "items_offset", "itemsOffset")
        if items_offset:
            out["itemsOffset"] = items_offset

        search_type = cls._first_qs_value(qs, "search_type", "searchType")
        if search_type:
            out["searchType"] = search_type

        return out

    @staticmethod
    def _looks_blocked(status: int, response_json: dict) -> bool:
        if status in (401, 403):
            return True
        if not isinstance(response_json, dict):
            return False
        errors = response_json.get('errors')
        if not isinstance(errors, list):
            return False
        for err in errors:
            if not isinstance(err, dict):
                continue
            text = ' '.join(
                str(x or '')
                for x in (
                    err.get('message'),
                    err.get('errorType'),
                    err.get('code'),
                    (err.get('extensions') or {}).get('code'),
                    (err.get('extensions') or {}).get('errorType'),
                )
            ).lower()
            if any(k in text for k in ('forbidden', 'unauth', 'challenge', 'captcha', 'security', 'login', 'block', 'persistedquerynotfound', 'persisted query not found', 'not found')):
                return True
        return False

    @staticmethod
    def _raw_params_from_overrides(overrides: dict) -> list[dict]:
        mapping = {
            'checkin': 'checkin',
            'checkout': 'checkout',
            'adults': 'adults',
            'guests': 'guests',
            'query': 'query',
            'placeId': 'placeId',
            'itemsOffset': 'itemsOffset',
            'itemsPerGrid': 'itemsPerGrid',
            'searchByMap': 'searchByMap',
            'neLat': 'neLat',
            'neLng': 'neLng',
            'swLat': 'swLat',
            'swLng': 'swLng',
            'centerLat': 'lat',
            'centerLng': 'lng',
            'searchMode': 'searchMode',
            'searchType': 'searchType',
            'guestFavorite': 'guestFavorite',
            'minBedrooms': 'minBedrooms',
            'minBeds': 'minBeds',
            'minBathrooms': 'minBathrooms',
        }
        out = []
        for key, raw_name in mapping.items():
            val = overrides.get(key)
            if val is None:
                continue
            if isinstance(val, bool):
                val = 'true' if val else 'false'
            out.append({'filterName': raw_name, 'filterValues': [str(val)]})
        return out

    @staticmethod
    def _to_global_id(prefix: str, listing_id: str) -> str:
        return base64.b64encode(f"{prefix}:{listing_id}".encode("utf-8")).decode("utf-8")

    def _build_stays_pdp_payload(self, listing_id: str, *, checkin: str, checkout: str, adults: int) -> Dict[str, Any]:
        template_post_data = (
            (HARDCODED_STAYS_PDP_TEMPLATE or {}).get("post_data")
            if isinstance(HARDCODED_STAYS_PDP_TEMPLATE, dict)
            else None
        )
        if isinstance(template_post_data, dict):
            payload: Dict[str, Any] = copy.deepcopy(template_post_data)
        else:
            payload = {
                "operationName": "StaysPdpSections",
                "variables": {
                    "id": "",
                    "demandStayListingId": "",
                    "pdpSectionsRequest": {
                        "adults": "1",
                        "layouts": ["SIDEBAR", "SINGLE_COLUMN"],
                        "sectionIds": list(_DEFAULT_STAYS_PDP_SECTION_IDS),
                        "checkIn": "",
                        "checkOut": "",
                    },
                    "dateRange": {"startDate": "", "endDate": ""},
                },
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": self.stays_pdp_sections_hash,
                    }
                },
            }

        vars_obj = payload.setdefault("variables", {})
        stay_gid = self._to_global_id("StayListing", str(listing_id))
        demand_gid = self._to_global_id("DemandStayListing", str(listing_id))
        vars_obj["id"] = stay_gid
        vars_obj["demandStayListingId"] = demand_gid

        pdp_req = vars_obj.setdefault("pdpSectionsRequest", {})
        if isinstance(pdp_req, dict):
            pdp_req["adults"] = str(adults)
            pdp_req["checkIn"] = checkin
            pdp_req["checkOut"] = checkout
            pdp_req["layouts"] = ["SIDEBAR", "SINGLE_COLUMN"]
            pdp_req["sectionIds"] = list(_DEFAULT_STAYS_PDP_SECTION_IDS)

        # Some templates use `variables.request` (not `pdpSectionsRequest`).
        # Keep both shapes in sync so amenities sections are always requested.
        req_obj = vars_obj.get("request")
        if isinstance(req_obj, dict):
            req_obj["adults"] = str(adults)
            req_obj["checkIn"] = checkin
            req_obj["checkOut"] = checkout
            req_obj["layouts"] = ["SIDEBAR", "SINGLE_COLUMN"]
            req_obj["sectionIds"] = list(_DEFAULT_STAYS_PDP_SECTION_IDS)
            # Ensure GraphQL fragments carrying amenities are enabled.
            req_obj["includeGpAmenitiesFragment"] = True
            req_obj["includePdpMigrationAmenitiesFragment"] = True

        date_range = vars_obj.setdefault("dateRange", {})
        if isinstance(date_range, dict):
            date_range["startDate"] = checkin
            date_range["endDate"] = checkout

        payload["operationName"] = "StaysPdpSections"
        ext = payload.setdefault("extensions", {})
        pquery = ext.setdefault("persistedQuery", {})
        pquery["version"] = 1
        pquery["sha256Hash"] = self.stays_pdp_sections_hash
        return payload

    def search_listings_with_overrides(self, overrides: Dict[str, Any]) -> Optional[Tuple[int, Dict[str, Any]]]:
        search_url = str(overrides.get("searchUrl") or "").strip()
        url_overrides = self._overrides_from_search_url(search_url) if search_url else {}
        merged_overrides = dict(url_overrides)
        merged_overrides.update(overrides)
        if search_url:
            logger.info("fetch url-search input searchUrl=%s", search_url)
            logger.info(
                "fetch url-search parsed query=%s checkin=%s checkout=%s adults=%s centerLat=%s centerLng=%s",
                url_overrides.get("query"),
                url_overrides.get("checkin"),
                url_overrides.get("checkout"),
                url_overrides.get("adults"),
                url_overrides.get("centerLat"),
                url_overrides.get("centerLng"),
            )

        checkin = str(merged_overrides.get("checkin") or self.config.get("CHECKIN", "") or "")
        checkout = str(merged_overrides.get("checkout") or self.config.get("CHECKOUT", "") or "")
        query = str(merged_overrides.get("query") or self.config.get("QUERY", "") or "").strip()
        adults = int(merged_overrides.get("adults") or self.config.get("ADULTS", 2) or 2)
        if search_url:
            logger.info(
                "fetch url-search effective query=%s checkin=%s checkout=%s adults=%s searchType=%s",
                query,
                checkin,
                checkout,
                adults,
                str(merged_overrides.get("searchType") or "user_map_move"),
            )
        raw_params = self._raw_params_from_overrides(merged_overrides)
        map_enabled = str(merged_overrides.get("searchByMap", "")).strip().lower() in ("true", "1")
        # Ensure required baseline params always exist.
        baseline = [
            {"filterName": "adults", "filterValues": [str(adults)]},
            {"filterName": "query", "filterValues": [query]},
            {"filterName": "checkin", "filterValues": [checkin]},
            {"filterName": "checkout", "filterValues": [checkout]},
            {"filterName": "screenSize", "filterValues": ["large"]},
            {"filterName": "tabId", "filterValues": ["home_tab"]},
            {"filterName": "version", "filterValues": ["1.8.8"]},
            {"filterName": "searchMode", "filterValues": [str(merged_overrides.get("searchMode") or "regular_search")]},
        ]
        seen = {p["filterName"] for p in raw_params}
        for p in baseline:
            if p["filterName"] not in seen and p["filterValues"][0]:
                raw_params.append(p)

        payload = {
            "operationName": "StaysSearch",
            "variables": {
                "staysSearchRequest": {
                    "metadataOnly": False,
                    "requestedPageType": "STAYS_SEARCH",
                    "searchType": str(merged_overrides.get("searchType") or "user_map_move"),
                    "rawParams": raw_params,
                },
                "staysMapSearchRequestV2": {
                    "metadataOnly": False,
                    "requestedPageType": "STAYS_SEARCH",
                    "searchType": str(merged_overrides.get("searchType") or "user_map_move"),
                    "rawParams": raw_params,
                },
                "isLeanTreatment": False,
                "aiSearchEnabled": False,
            },
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": self.stays_search_hash}},
        }
        url = self._stays_search_url()
        logger.info("fetch search url=%s", url)
        try:
            resp = self.session.post(url, json=payload, headers=self._headers(), timeout=25)
            status = int(resp.status_code)
            if status in (401, 403):
                raise ScraperForbiddenError(f"DeepBnb StaysSearch blocked with status={status}")
            raw_json = resp.json() if resp.content else {}
            logger.info(
                "[deepbnb_search_response] checkin=%s checkout=%s adults=%s status=%s has_errors=%s",
                checkin,
                checkout,
                adults,
                status,
                bool((raw_json or {}).get("errors")) if isinstance(raw_json, dict) else False,
            )
            if self._looks_blocked(status, raw_json if isinstance(raw_json, dict) else {}):
                raise ScraperForbiddenError(f"DeepBnb StaysSearch blocked with status={status}")
        except Exception as exc:
            if isinstance(exc, ScraperForbiddenError):
                raise
            logger.warning("fetch StaysSearch failed: %s", exc)
            return None

        converted = parse_deepbnb_search_to_stayssearch_payload(
            raw_json if isinstance(raw_json, dict) else {},
            checkin=checkin,
            checkout=checkout,
            currency=self.currency,
        )
        return status, converted

    def get_listing_details_via_stays_pdp_sections(
        self,
        listing_id: str,
        *,
        checkin: str,
        checkout: str,
        adults: int,
    ) -> Optional[Dict[str, Any]]:
        payload = self._build_stays_pdp_payload(
            str(listing_id),
            checkin=checkin,
            checkout=checkout,
            adults=adults,
        )
        url = self._stays_pdp_sections_url()
        logger.info("fetch pdp url=%s", url)
        try:
            resp = self.session.post(url, json=payload, headers=self._headers(), timeout=25)
            status = int(resp.status_code)
            if status in (401, 403):
                raise ScraperForbiddenError(f"Fetch StaysPdpSections blocked with status={status}")
            raw_json = resp.json() if resp.content else {}
            if self._looks_blocked(status, raw_json if isinstance(raw_json, dict) else {}):
                raise ScraperForbiddenError(f"Fetch StaysPdpSections blocked with status={status}")
        except Exception as exc:
            if isinstance(exc, ScraperForbiddenError):
                raise
            logger.warning("fetch StaysPdpSections failed: %s", exc)
            return None

        converted = parse_deepbnb_pdp_to_stayspdp_payload(
            raw_json if isinstance(raw_json, dict) else {},
            listing_id=str(listing_id),
            checkin=checkin,
            checkout=checkout,
            currency=self.currency,
        )
        return converted

    def get_listing_details(
        self,
        listing_id: str,
        *,
        checkin: str,
        checkout: str,
        adults: int,
    ) -> Optional[Dict[str, Any]]:
        stays_pdp_result = self.get_listing_details_via_stays_pdp_sections(
            listing_id=listing_id,
            checkin=checkin,
            checkout=checkout,
            adults=adults,
        )
        if stays_pdp_result is not None:
            return stays_pdp_result

        payload = {
            "operationName": "PdpPlatformSections",
            "variables": {
                "request": {
                    "id": str(listing_id),
                    "layouts": ["SIDEBAR", "SINGLE_COLUMN"],
                    "adults": str(adults),
                    "checkIn": checkin,
                    "checkOut": checkout,
                    "sectionIds": None,
                }
            },
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": self.pdp_platform_hash}},
        }
        url = self._pdp_platform_url()
        logger.info("fetch fallback pdp url=%s", url)
        try:
            resp = self.session.post(url, json=payload, headers=self._headers(), timeout=25)
            status = int(resp.status_code)
            if status in (401, 403):
                raise ScraperForbiddenError(f"DeepBnb PdpPlatformSections blocked with status={status}")
            raw_json = resp.json() if resp.content else {}
            if self._looks_blocked(status, raw_json if isinstance(raw_json, dict) else {}):
                raise ScraperForbiddenError(f"DeepBnb PdpPlatformSections blocked with status={status}")
        except Exception as exc:
            if isinstance(exc, ScraperForbiddenError):
                raise
            logger.warning("fetch PdpPlatformSections failed: %s", exc)
            return None

        converted = parse_deepbnb_pdp_to_stayspdp_payload(
            raw_json if isinstance(raw_json, dict) else {},
            listing_id=str(listing_id),
            checkin=checkin,
            checkout=checkout,
            currency=self.currency,
        )
        return converted
