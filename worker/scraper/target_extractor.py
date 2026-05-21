"""
Target listing spec extraction.

Navigates to an Airbnb listing page via Playwright and extracts
structured property specs (title, location, capacity, amenities, etc.).

Extracted from price_estimator.py for modularity.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from datetime import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from worker.scraper.parsers import parse_pdp_response

logger = logging.getLogger("worker.scraper.target_extractor")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ListingSpec:
    url: str
    title: str = ""
    location: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""
    country_code: str = ""
    accommodates: Optional[int] = None
    bedrooms: Optional[int] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    property_type: str = ""
    nightly_price: Optional[float] = None
    # Original trip total amount when nightly_price was derived by dividing
    # a multi-night card total (e.g. "$500 for 2 nights" -> 250/night).
    query_total_price: Optional[float] = None
    currency: str = "USD"
    rating: Optional[float] = None
    reviews: Optional[int] = None
    amenities: List[str] = field(default_factory=list)
    # Number of nights the scraped price covered before per-night normalization.
    # 1 = normal 1-night search card; 2 = "for 2 nights" trip total, divided by 2; etc.
    scrape_nights: int = 1
    # Raw price_kind from JS extractor: "trip_total_*", "nightly_*", or "unknown".
    price_kind: str = "unknown"
    # Approximate WGS-84 coordinates (optional — best-effort from geocoding or page state).
    lat: Optional[float] = None
    lng: Optional[float] = None
    # Distance from the target listing (set by geo_filter, None if coords unavailable).
    distance_to_target_km: Optional[float] = None


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

MONEY_RE = re.compile(
    r"(?<!\w)(?:US)?\s?\$?\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?(?!\w)"
)
BEDROOM_RE = re.compile(r"(\d+)\s*(?:bedroom|bedrooms|bd|bdrm|間臥室|卧室)", re.I)
BED_RE = re.compile(r"(\d+)\s*(?:bed|beds|張床|床)", re.I)
BATH_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:bath|baths|ba|衛浴|浴室|衛生間|卫生间)", re.I
)
GUEST_RE = re.compile(r"(\d+)(?:\+)?\s*(?:guest|guests|位|人)", re.I)
PROPERTY_TYPE_HINTS = {
    "entire_home": [
        "entire home",
        "entire place",
        "entire rental unit",
        "entire condo",
        "entire townhouse",
    ],
    "private_room": ["private room"],
    "shared_room": ["shared room"],
}
AMENITY_HINTS = {
    "wifi": ["wifi", "wi-fi"],
    "kitchen": ["kitchen"],
    "washer": ["washer", "washing machine", "laundry"],
    "dryer": ["dryer"],
    "ac": ["air conditioning", "a/c", "ac"],
    "heating": ["heating", "heater"],
    "pool": ["pool"],
    "hot_tub": ["hot tub", "jacuzzi"],
    "free_parking": ["free parking", "parking on premises"],
    "gym": ["gym", "fitness"],
    "bbq": ["bbq", "barbecue", "grill"],
    "fire_pit": ["fire pit"],
    "pets_allowed": ["pets allowed", "allows pets", "pet-friendly", "pet friendly"],
    "waterfront": ["waterfront", "water front", "beachfront", "lakefront"],
    "guest_favorite": ["guest favorite", "top guest favorite"],
}


def clean(s: str) -> str:
    return (s or "").replace("\u00a0", " ").strip()


def to_int(x: str) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


def to_float(x: str) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


COUNTRY_NAME_TO_CODE = {
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "us": "US",
    "taiwan": "TW",
    "tw": "TW",
}


def normalize_country_code(value: Any) -> str:
    text = clean(str(value or ""))
    if not text:
        return ""

    upper = text.upper()
    if len(upper) == 2 and upper.isalpha():
        return upper

    return COUNTRY_NAME_TO_CODE.get(text.casefold(), "")


def derive_location_parts(location: str) -> Tuple[str, str, str]:
    parts = [part.strip() for part in clean(location).split(",") if part.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        if re.fullmatch(r"[A-Za-z]{2,3}", parts[1]):
            return parts[0], parts[1].upper(), ""
        return parts[0], "", parts[1]
    if len(parts) == 1:
        return parts[0], "", ""
    return "", "", ""


def extract_first_int(text: str, patterns: list) -> Optional[int]:
    for p in patterns:
        m = p.search(text or "")
        if m:
            return to_int(m.group(1))
    return None


def extract_first_float(text: str, patterns: list) -> Optional[float]:
    for p in patterns:
        m = p.search(text or "")
        if m:
            return to_float(m.group(1))
    return None


def parse_money_to_float(text: str) -> Optional[float]:
    if not text:
        return None
    m = MONEY_RE.search(text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None


def normalize_property_type(text: str) -> str:
    t = (text or "").lower()
    # Check private_room and shared_room first so "Private room in an entire home"
    # is not misclassified as entire_home.
    if "private room" in t:
        return "private_room"
    if "shared room" in t:
        return "shared_room"
    # Match any "Entire [property type]" variant — Airbnb uses many (home, guesthouse,
    # villa, cottage, cabin, loft, rental unit, condo, townhouse, etc.).
    # The old allowlist only covered 5 variants, causing all other types to score 0.35
    # on property-type similarity (below the 0.40 floor) and be silently dropped.
    if re.search(r"\bentire\b", t):
        return "entire_home"
    return ""


def extract_amenities(text: str) -> List[str]:
    t = (text or "").lower()
    out: List[str] = []
    for code, hints in AMENITY_HINTS.items():
        if any(h in t for h in hints):
            out.append(code)
    return out


# ---------------------------------------------------------------------------
# CDP / URL helpers
# ---------------------------------------------------------------------------


def check_cdp_endpoint(cdp_url: str, timeout_seconds: float = 2.0) -> Tuple[bool, str]:
    probe = cdp_url.rstrip("/") + "/json/version"
    try:
        req = Request(probe, headers={"User-Agent": "AiraHost-Worker/1.0"})
        with urlopen(req, timeout=timeout_seconds) as r:
            if r.status == 200:
                return True, "ok"
            return False, f"HTTP {r.status}"
    except Exception as exc:
        return False, str(exc)


_AIRBNB_HOST_RE = re.compile(
    r"^(?:[a-z]{2,5}(?:-[a-z]{1,5})?\.)?airbnb\.(?:com|ca)$", re.IGNORECASE
)
_CANONICAL_AIRBNB_HOST = "www.airbnb.com"


def normalize_airbnb_url(url: str) -> str:
    """Normalize localized Airbnb domains to www.airbnb.com.

    Any host matching *.airbnb.com (e.g. zh-t.airbnb.com, zh.airbnb.com,
    fr.airbnb.com) or *.airbnb.ca is rewritten to www.airbnb.com.
    Path, query string, and fragment are preserved verbatim.
    Non-Airbnb URLs are returned unchanged.
    """
    if not url:
        return url
    try:
        p = urlparse(url)
        if p.netloc and _AIRBNB_HOST_RE.match(p.netloc) and p.netloc != _CANONICAL_AIRBNB_HOST:
            p = p._replace(netloc=_CANONICAL_AIRBNB_HOST)
            return urlunparse(p)
    except Exception:
        pass
    return url


def safe_domain_base(url: str) -> str:
    p = urlparse(normalize_airbnb_url(url))
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}".rstrip("/")
    return "https://www.airbnb.com"


def extract_listing_id_from_url(listing_url: str) -> Optional[str]:
    m = re.search(r"/rooms/(\d+)", normalize_airbnb_url(listing_url or ""))
    return m.group(1) if m else None


def _normalize_property_type_for_spec(value: Any) -> str:
    v = clean(str(value or ""))
    if not v:
        return ""
    normalized = normalize_property_type(v)
    return normalized or v.lower().replace(" ", "_")


def map_pdp_to_listing_spec(parsed: Dict[str, Any], listing_url: str) -> ListingSpec:
    location = clean(str(parsed.get("location") or ""))
    city = clean(str(parsed.get("city") or ""))
    state = clean(str(parsed.get("state") or ""))
    country = clean(str(parsed.get("country") or ""))
    country_code = normalize_country_code(parsed.get("country_code") or country)
    if location and (not city or not state or not country):
        _city, _state, _country = derive_location_parts(location)
        city = city or _city
        state = state or _state
        country = country or _country

    return ListingSpec(
        url=normalize_airbnb_url(listing_url),
        title=clean(str(parsed.get("title") or "")),
        location=location or ", ".join([p for p in (city, state, country) if p]),
        city=city,
        state=state,
        postal_code=clean(str(parsed.get("postal_code") or "")),
        country=country,
        country_code=country_code,
        accommodates=parsed.get("accommodates"),
        bedrooms=parsed.get("bedrooms"),
        beds=parsed.get("beds"),
        baths=parsed.get("baths"),
        property_type=_normalize_property_type_for_spec(parsed.get("property_type")),
        nightly_price=parsed.get("nightly_price"),
        currency=clean(str(parsed.get("currency") or "USD")),
        amenities=list(parsed.get("amenities") or []),
    )


class _AsyncLocatorSyncAdapter:
    """Minimal sync-style wrapper around an async Playwright locator."""

    def __init__(self, scraper: Any, locator: Any) -> None:
        self._scraper = scraper
        self._locator = locator

    @property
    def first(self) -> "_AsyncLocatorSyncAdapter":
        return _AsyncLocatorSyncAdapter(self._scraper, self._locator.first)

    def inner_text(self, timeout: Optional[int] = None) -> str:
        kwargs = {"timeout": timeout} if timeout is not None else {}
        return self._scraper._run_async(
            self._locator.inner_text(**kwargs),
            op_name="target_extractor.locator.inner_text",
        )


class _AsyncPageSyncAdapter:
    """Minimal sync-style wrapper around an async Playwright page."""

    def __init__(self, scraper: Any, page: Any) -> None:
        self._scraper = scraper
        self._page = page

    def goto(self, url: str, **kwargs: Any) -> Any:
        self.wait_for_timeout(int(random.uniform(250, 1200)))
        return self._scraper._run_async(
            self._page.goto(url, **kwargs),
            op_name="target_extractor.page.goto",
        )

    def wait_for_timeout(self, timeout: int) -> Any:
        return self._scraper._run_async(
            self._page.wait_for_timeout(timeout),
            op_name="target_extractor.page.wait_for_timeout",
        )

    def evaluate(self, expression: str) -> Any:
        return self._scraper._run_async(
            self._page.evaluate(expression),
            op_name="target_extractor.page.evaluate",
        )

    def inner_text(self, selector: str, timeout: Optional[int] = None) -> str:
        kwargs = {"timeout": timeout} if timeout is not None else {}
        return self._scraper._run_async(
            self._page.inner_text(selector, **kwargs),
            op_name="target_extractor.page.inner_text",
        )

    def locator(self, selector: str) -> _AsyncLocatorSyncAdapter:
        return _AsyncLocatorSyncAdapter(self._scraper, self._page.locator(selector))


def _extract_target_spec_via_playwright_bridge(client: Any, listing_url: str) -> Tuple[Optional[ListingSpec], List[str]]:
    """
    Open the listing page through the async Playwright scraper, then expose a
    tiny sync-style adapter so the legacy DOM extractor can still run.
    """
    if not hasattr(client, "_get_playwright_scraper"):
        return None, ["Browser HTML extraction failed: missing Playwright scraper bridge on client"]

    scraper = client._get_playwright_scraper()
    context = None
    page = None

    try:
        context = scraper._run_async(
            scraper._get_thread_context(),
            op_name="target_extractor.get_thread_context",
        )
        scraper._sync_session_cookies_into_context(context)
        page = scraper._run_async(
            scraper._open_capped_page(context),
            op_name="target_extractor.open_capped_page",
        )
        return extract_target_spec(_AsyncPageSyncAdapter(scraper, page), listing_url)
    except Exception as exc:
        return None, [f"Browser HTML extraction failed: {exc}"]
    finally:
        if context is not None:
            try:
                scraper._run_async(
                    scraper._sync_context_cookies_into_session(context),
                    op_name="target_extractor.sync_context_cookies",
                )
                scraper._save_cached_state()
            except Exception:
                pass
        if page is not None:
            try:
                scraper._run_async(
                    scraper._close_capped_page(page),
                    op_name="target_extractor.close_capped_page",
                )
            except Exception:
                pass


def _human_pause_sync_page(page: Any, min_ms: int = 250, max_ms: int = 1200) -> None:
    timeout_ms = int(random.uniform(min_ms, max_ms))
    try:
        page.wait_for_timeout(timeout_ms)
    except Exception:
        try:
            time.sleep(timeout_ms / 1000.0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Page-level extraction
# ---------------------------------------------------------------------------


def _listing_spec_has_usable_fields(spec: ListingSpec) -> bool:
    return any(
        [
            spec.title,
            spec.location,
            spec.property_type,
            spec.accommodates is not None,
            spec.bedrooms is not None,
            spec.baths is not None,
            bool(spec.amenities),
        ]
    )


def _map_client_pdp_payload_to_spec(
    payload: Dict[str, Any],
    listing_id: str,
    listing_url: str,
) -> ListingSpec:
    parsed = parse_pdp_response(
        payload,
        str(listing_id),
        safe_domain_base(listing_url),
    )
    return map_pdp_to_listing_spec(parsed, listing_url)


def _extract_target_spec_via_client_payloads(
    client: Any,
    listing_url: str,
    listing_id: str,
) -> Tuple[Optional[ListingSpec], List[str]]:
    payload_warnings: List[str] = []
    config = getattr(client, "config", {}) or {}
    effective_checkin = clean(str(config.get("CHECKIN") or ""))
    effective_checkout = clean(str(config.get("CHECKOUT") or ""))
    try:
        effective_adults = max(1, int(config.get("ADULTS", 1) or 1))
    except Exception:
        effective_adults = 1

    deepbnb_scraper = getattr(client, "deepbnb_scraper", None)
    if deepbnb_scraper is not None and effective_checkin and effective_checkout:
        try:
            spec = _map_client_pdp_payload_to_spec(
                deepbnb_scraper.get_listing_details(
                    str(listing_id),
                    checkin=effective_checkin,
                    checkout=effective_checkout,
                    adults=effective_adults,
                ),
                listing_id,
                listing_url,
            )
            if _listing_spec_has_usable_fields(spec):
                return spec, payload_warnings
            payload_warnings.append(
                "Deepbnb PDP payload extraction returned no usable fields; trying browser PDP payload"
            )
        except Exception as exc:
            payload_warnings.append(f"Deepbnb PDP payload extraction failed: {exc}")

    client_payload_kwargs: Dict[str, Any] = {}
    if effective_checkin:
        client_payload_kwargs["checkin"] = effective_checkin
    if effective_checkout:
        client_payload_kwargs["checkout"] = effective_checkout
    client_payload_kwargs["adults"] = effective_adults

    try:
        spec = _map_client_pdp_payload_to_spec(
            client.get_listing_details(str(listing_id), **client_payload_kwargs),
            listing_id,
            listing_url,
        )
        if _listing_spec_has_usable_fields(spec):
            return spec, payload_warnings
        payload_warnings.append(
            "PDP payload extraction returned no usable fields; falling back to browser HTML extraction"
        )
    except Exception as exc:
        payload_warnings.append(f"PDP payload extraction failed: {exc}")

    return None, payload_warnings


def extract_target_spec(
    client,
    listing_url: str,
    *,
    fail_on_unusable: bool = False,
) -> Tuple[ListingSpec, List[str]]:
    """
    Extract listing specs. Preferred path uses AirbnbClient HTTP fetches.
    Legacy page extraction is retained as a fallback when a non-client object is passed.

    Returns (ListingSpec, extraction_warnings).
    """
    if hasattr(client, "get_listing_details"):
        warnings: List[str] = []
        listing_id = extract_listing_id_from_url(listing_url)
        if not listing_id:
            if fail_on_unusable:
                raise ValueError(f"Unable to parse listing_id from URL: {listing_url}")
            return (
                ListingSpec(url=normalize_airbnb_url(listing_url)),
                [f"Unable to parse listing_id from URL: {listing_url}"],
            )

        def _extract_via_browser_dom() -> Tuple[Optional[ListingSpec], List[str]]:
            if not hasattr(client, "_get_playwright_scraper"):
                return None, ["Browser DOM fallback unavailable: missing Playwright scraper bridge on client"]
            try:
                scraper = client._get_playwright_scraper()
                context = scraper._run_async(
                    scraper._get_thread_context(),
                    op_name="target_dom_get_context",
                )
                scraper._sync_session_cookies_into_context(context)
                page = scraper._run_async(
                    scraper._open_capped_page(context),
                    op_name="target_dom_open_page",
                )
                try:
                    spec, dom_warnings = extract_target_spec(page, listing_url)
                finally:
                    try:
                        scraper._run_async(
                            scraper._sync_context_cookies_into_session(context),
                            op_name="target_dom_sync_cookies",
                        )
                    except Exception:
                        pass
                    scraper._run_async(
                        scraper._close_capped_page(page),
                        op_name="target_dom_close_page",
                    )
                return spec, dom_warnings
            except Exception as exc:
                return None, [f"Browser DOM fallback failed: {exc}"]

        try:
            spec, payload_warnings = _extract_target_spec_via_client_payloads(
                client,
                listing_url,
                str(listing_id),
            )
            warnings.extend(payload_warnings)
            if spec is None:
                raise ValueError("PDP extraction returned no usable spec")
            if spec.accommodates is None:
                warnings.append("Missing accommodates from PDP response")
            if spec.bedrooms is None:
                warnings.append("Missing bedrooms from PDP response")
            if spec.beds is None:
                warnings.append("Missing beds from PDP response")
            if spec.baths is None:
                warnings.append("Missing baths from PDP response")
            if not spec.property_type:
                warnings.append("Missing property_type from PDP response")
            if not spec.location and spec.lat is None and spec.lng is None:
                dom_spec, dom_warnings = _extract_via_browser_dom()
                warnings.extend(dom_warnings)
                if isinstance(dom_spec, ListingSpec):
                    if dom_spec.title and not spec.title:
                        spec.title = dom_spec.title
                    if dom_spec.location and not spec.location:
                        spec.location = dom_spec.location
                    if dom_spec.city and not spec.city:
                        spec.city = dom_spec.city
                    if dom_spec.state and not spec.state:
                        spec.state = dom_spec.state
                    if dom_spec.country and not spec.country:
                        spec.country = dom_spec.country
                    if spec.lat is None and isinstance(dom_spec.lat, (int, float)):
                        spec.lat = float(dom_spec.lat)
                    if spec.lng is None and isinstance(dom_spec.lng, (int, float)):
                        spec.lng = float(dom_spec.lng)
            return spec, warnings
        except Exception as exc:
            warnings.append(f"PDP extraction failed: {exc}")
            dom_spec, dom_warnings = _extract_via_browser_dom()
            warnings.extend(dom_warnings)
            if isinstance(dom_spec, ListingSpec):
                return dom_spec, warnings
            if fail_on_unusable:
                raise ValueError(
                    f"PDP individual listing extraction failed (payload + rendered DOM): {listing_url}"
                )
            return (
                ListingSpec(url=normalize_airbnb_url(listing_url)),
                warnings,
            )

    # Legacy DOM fallback
    page = client
    warnings: List[str] = []

    _human_pause_sync_page(page)
    page.goto(listing_url, wait_until="domcontentloaded")
    page.wait_for_timeout(800)

    # JSON-LD
    ld = None
    try:
        ld = page.evaluate(
            """() => {
              const scripts = Array.from(
                document.querySelectorAll('script[type="application/ld+json"]')
              );
              return scripts.map(s => s.textContent || '').filter(Boolean);
            }"""
        )
    except Exception:
        ld = None
        warnings.append("Failed to extract JSON-LD data")

    body_text = ""
    try:
        body_text = page.inner_text("body", timeout=8000)
    except Exception:
        body_text = ""
        warnings.append("Failed to extract body text")

    title = ""
    location = ""
    city = ""
    state = ""
    postal_code = ""
    country = ""
    country_code = ""
    accommodates = None
    bedrooms = None
    beds = None
    baths = None
    property_type = ""

    # Title
    try:
        title = clean(page.locator("h1").first.inner_text(timeout=4000))
    except Exception:
        title = clean((body_text.splitlines() or [""])[0])
        warnings.append("Title extracted from body text fallback")

    # ── Location extraction (multi-strategy) ─────────────────────
    #
    # Airbnb listing pages show location in several places.  We try
    # multiple strategies in order of reliability.

    # Patterns matching "Entire home in City, State" / "整套房源位於 台北"
    _LOC_SUBTITLE_RE = [
        re.compile(
            r"(?:Entire\s+\w+|Private\s+room|Shared\s+room|Room|Hotel\s+room)"
            r"\s+in\s+(.+)",
            re.I,
        ),
        re.compile(
            r"(?:整套|獨立房間|合住房間|房間|飯店房間)\s*[·位於在]+\s*(.+)",
        ),
    ]

    def _clean_loc(raw: str) -> str:
        """Strip trailing noise like ★4.95 · 2 guests."""
        return re.split(r"\s*[·•★]", raw)[0].strip()

    # Strategy 1: DOM — subtitle near <h1> + breadcrumbs + meta tags
    dom_hints: List[str] = []
    try:
        dom_hints = page.evaluate(
            """() => {
              const out = [];
              // 1a) Elements near <h1> that describe location
              const h1 = document.querySelector('h1');
              if (h1) {
                let el = h1.nextElementSibling || h1.parentElement?.nextElementSibling;
                for (let i = 0; i < 6 && el; i++) {
                  const t = (el.innerText || '').trim().split('\\n')[0];
                  if (t && t.length > 3 && t.length < 200) out.push(t);
                  el = el.nextElementSibling;
                }
                // Also check the parent container's children
                const parent = h1.closest('section') || h1.closest('div[data-section-id]') || h1.parentElement;
                if (parent) {
                  for (const child of parent.querySelectorAll('span, div, h2, h3')) {
                    const t = (child.innerText || '').trim().split('\\n')[0];
                    if (t && t.length > 5 && t.length < 150 &&
                        (t.toLowerCase().includes(' in ') || t.includes('位於') || t.includes('·'))) {
                      out.push(t);
                    }
                  }
                }
              }
              // 1b) Breadcrumb navigation
              const bcLinks = document.querySelectorAll(
                'nav[aria-label*="readcrumb"] a, nav[aria-label*="breadcrumb"] a, ' +
                'ol[role="list"] a[href*="/s/"], nav a[href*="/s/"]'
              );
              if (bcLinks.length > 0) {
                const parts = Array.from(bcLinks)
                  .map(a => (a.innerText || '').trim())
                  .filter(t => t && t.length > 1 &&
                    !t.toLowerCase().includes('airbnb') &&
                    t.toLowerCase() !== 'home');
                if (parts.length > 0) out.push('BC:' + parts.join(', '));
              }
              // 1c) Meta og:title
              const og = document.querySelector('meta[property="og:title"]');
              if (og) out.push('META:' + (og.getAttribute('content') || ''));
              // 1d) Document title
              if (document.title) out.push('TITLE:' + document.title);
              return out;
            }"""
        )
    except Exception:
        dom_hints = []
        warnings.append("Failed to extract DOM hints for location")

    # 1a) Subtitle — "Entire home in City, State, Country"
    for hint in dom_hints:
        if hint.startswith(("BC:", "META:", "TITLE:")):
            continue
        for pat in _LOC_SUBTITLE_RE:
            m = pat.search(hint)
            if m:
                loc = _clean_loc(clean(m.group(1)))
                if loc and len(loc) >= 3:
                    location = loc
                    break
        if location:
            break

    # 1b) Breadcrumbs — "Country, Region, City" → use last 2 parts
    if not location:
        for hint in dom_hints:
            if hint.startswith("BC:"):
                bc_parts = [p.strip() for p in hint[3:].split(",") if p.strip()]
                if len(bc_parts) >= 2:
                    location = ", ".join(bc_parts[-2:])
                elif bc_parts:
                    location = bc_parts[-1]
                break

    # 1c/1d) Meta / title — "Name - vacation rental in City, State"
    if not location:
        for hint in dom_hints:
            if not hint.startswith(("META:", "TITLE:")):
                continue
            text = hint.split(":", 1)[1].strip()
            loc_m = re.search(
                r"(?:rental|home|apartment|place|room|stay|cabin|villa|condo)"
                r"\s+in\s+([^·|]+)",
                text, re.I,
            )
            if loc_m:
                loc = _clean_loc(clean(loc_m.group(1)))
                if loc and len(loc) >= 3:
                    location = loc
                    break
            # Fallback: split by · and find a part with commas (looks like "City, State")
            for part in re.split(r"\s*[·|]\s*", text):
                part = part.strip()
                if ("," in part and 5 <= len(part) <= 80
                        and not re.search(r"^\d|★|Airbnb|review", part, re.I)):
                    location = part
                    break
            if location:
                break

    # Strategy 2: Body-text scan — look for "in City, State" near top
    if not location:
        top_lines = [
            ln.strip() for ln in (body_text.splitlines()[:80]) if ln.strip()
        ]
        for ln in top_lines:
            for pat in _LOC_SUBTITLE_RE:
                m = pat.search(ln)
                if m:
                    loc = _clean_loc(clean(m.group(1)))
                    if loc and len(loc) >= 3:
                        location = loc
                        break
            if location:
                break

    if location:
        logger.info(f"Extracted location: '{location}'")
    else:
        warnings.append("Could not extract location from listing page")

    # Specs from body text
    accommodates = extract_first_int(body_text, [GUEST_RE])
    bedrooms = extract_first_int(body_text, [BEDROOM_RE])
    beds = extract_first_int(body_text, [BED_RE])
    baths = extract_first_float(body_text, [BATH_RE])

    if accommodates is None:
        warnings.append("Could not extract guest capacity")
    if bedrooms is None:
        warnings.append("Could not extract bedroom count")

    # Property type — scan top ~80 lines of body text
    top_slice = "\n".join(
        ln.strip() for ln in (body_text.splitlines()[:80]) if ln.strip()
    )
    for ln in top_slice.splitlines():
        if any(k in ln.lower() for k in ["entire", "private room", "shared room"]) or any(
            k in ln for k in ["整套", "獨立房間", "合住房間"]
        ):
            property_type = clean(ln)
            break

    # Rating / reviews
    rating = None
    reviews = None
    rate_match = re.search(
        r"(\d\.\d\d|\d\.\d)\s*(?:\(|·|・)?\s*(\d+)\s*review",
        body_text[:3000],
        re.I,
    )
    if rate_match:
        try:
            r = float(rate_match.group(1))
            if 2.5 <= r <= 5.0:
                rating = r
            n = int(rate_match.group(2))
            reviews = n
        except Exception:
            pass

    # Phase 3B: coordinates extracted from this page (populated in JSON-LD loop below)
    spec_lat: Optional[float] = None
    spec_lng: Optional[float] = None

    # Enrich from JSON-LD if available
    if isinstance(ld, list) and ld:
        for block in ld:
            try:
                obj = json.loads(block)
            except Exception:
                continue
            ld_items = obj if isinstance(obj, list) else [obj]
            for it in ld_items:
                if not isinstance(it, dict):
                    continue
                t = it.get("@type") or ""
                if t and any(
                    x in str(t)
                    for x in [
                        "LodgingBusiness", "Hotel", "Apartment",
                        "House", "Accommodation", "VacationRental",
                        "SingleFamilyResidence", "Residence",
                    ]
                ):
                    title = title or clean(str(it.get("name") or ""))
                    addr = it.get("address") or {}
                    if isinstance(addr, dict):
                        # Build location from most specific to least
                        locality = str(addr.get("addressLocality") or "").strip()
                        region = str(addr.get("addressRegion") or "").strip()
                        postal = str(addr.get("postalCode") or "").strip()
                        country_name = str(addr.get("addressCountry") or "").strip()
                        if locality:
                            city = locality
                        if region:
                            state = region
                        if postal:
                            postal_code = postal
                        if country_name:
                            country = country_name
                            country_code = country_code or normalize_country_code(country_name)
                        # Prefer "City, State" over broader heuristic matches
                        ld_parts = [p for p in [locality, region, country_name] if p]
                        if ld_parts:
                            ld_location = ", ".join(ld_parts)
                            if not location or len(locality) > 0:
                                location = ld_location
                                logger.info(f"Location from JSON-LD: '{location}'")
                    # aggregateRating
                    agg = it.get("aggregateRating") or {}
                    if isinstance(agg, dict):
                        if rating is None and agg.get("ratingValue"):
                            try:
                                rating = float(agg["ratingValue"])
                            except Exception:
                                pass
                        if reviews is None and agg.get("reviewCount"):
                            try:
                                reviews = int(agg["reviewCount"])
                            except Exception:
                                pass
                    # Phase 3B: geo coordinates from JSON-LD (best-effort)
                    geo_block = it.get("geo") or {}
                    if isinstance(geo_block, dict):
                        try:
                            _lat = float(geo_block.get("latitude") or 0)
                            _lng = float(geo_block.get("longitude") or 0)
                            if (-90 <= _lat <= 90) and (-180 <= _lng <= 180) and (_lat != 0 or _lng != 0):
                                # Only set if not already populated by a prior block
                                if spec_lat is None:
                                    spec_lat = _lat
                                    spec_lng = _lng
                                    logger.info(
                                        f"Target coords from JSON-LD geo: "
                                        f"({spec_lat:.5f}, {spec_lng:.5f})"
                                    )
                        except Exception:
                            pass
                    break

    if location and (not city or not country):
        derived_city, derived_state, derived_country = derive_location_parts(location)
        city = city or derived_city
        state = state or derived_state
        country = country or derived_country
        country_code = country_code or normalize_country_code(country)

    amenities = extract_amenities(body_text)

    spec = ListingSpec(
        url=listing_url,
        title=title,
        location=location,
        city=city,
        state=state,
        postal_code=postal_code,
        country=country,
        country_code=country_code,
        accommodates=accommodates,
        bedrooms=bedrooms,
        beds=beds,
        baths=baths,
        property_type=normalize_property_type(property_type or body_text),
        amenities=amenities,
        rating=rating,
        reviews=reviews,
        lat=spec_lat,
        lng=spec_lng,
    )

    return spec, warnings


def extract_listing_page_title(page, listing_url: str) -> Tuple[str, List[str]]:
    """
    Navigate to an Airbnb listing page and extract the canonical listing title.

    Returns (title, warnings). This is lighter-weight than extract_target_spec
    and is intended for post-processing comparable listings whose search-card
    title looks suspicious.
    """
    warnings: List[str] = []
    title = ""

    try:
        _human_pause_sync_page(page)
        page.goto(listing_url, wait_until="domcontentloaded")
        page.wait_for_timeout(600)
    except Exception as exc:
        warnings.append(f"Failed to open listing page: {exc}")
        return "", warnings

    try:
        title = clean(page.locator("h1").first.inner_text(timeout=3000))
    except Exception:
        title = ""

    if title:
        return title, warnings

    try:
        ld = page.evaluate(
            """() => {
              const scripts = Array.from(
                document.querySelectorAll('script[type="application/ld+json"]')
              );
              return scripts.map(s => s.textContent || '').filter(Boolean);
            }"""
        )
    except Exception:
        ld = []

    if isinstance(ld, list):
        for block in ld:
            try:
                obj = json.loads(block)
            except Exception:
                continue
            items = obj if isinstance(obj, list) else [obj]
            for it in items:
                if not isinstance(it, dict):
                    continue
                name = clean(str(it.get("name") or ""))
                if name:
                    return name, warnings

    try:
        body_text = page.inner_text("body", timeout=5000)
        for line in body_text.splitlines():
            line = clean(line)
            if len(line) >= 8:
                warnings.append("Title extracted from body text fallback")
                return line, warnings
    except Exception:
        pass

    warnings.append("Could not extract listing page title")
    return "", warnings


# ---------------------------------------------------------------------------
# Benchmark price extraction — direct listing page visit with dates
# ---------------------------------------------------------------------------

_NIGHTLY_PRICE_RES = [
    re.compile(r"\$\s*(\d{1,4}(?:,\d{3})?)\s*/\s*night", re.I),
    re.compile(r"\$\s*(\d{1,4}(?:,\d{3})?)\s+per\s+night", re.I),
    re.compile(r"(\d{1,4}(?:,\d{3})?)\s+USD\s*/\s*night", re.I),
    # "for 1 night" — already a per-night price, no division needed.
    # \s* (not \s+) before "for" so that adjacent inline spans concatenated without
    # whitespace ("$321for 1 night") are still matched.
    re.compile(r"\$\s*(\d{1,4}(?:,\d{3})?)\s*for\s+1\s+nights?", re.I),
    # CAD/AUD/NZD/GBP/EUR suffix format used on non-USD Airbnb domains (e.g. .ca, .com.au).
    # Airbnb displays "$267 CAD" as the nightly price without a "/night" label.
    # Negative lookahead excludes:
    #   "$195 CAD total"       — explicit trip-total label
    #   "$267 CAD for 2 nights" — multi-night trip total (handled by _TRIP_TOTAL_RE instead)
    re.compile(r"\$\s*(\d{1,4}(?:,\d{3})?)\s+(?:CAD|AUD|NZD|GBP|EUR)(?!\s+(?:total\b|for\s+\d))", re.I),
]

# Matches trip-total format: "$300 for 2 nights" — requires division by N.
# Group 1 = price digits, Group 2 = night count (N ≥ 2).
# \s* (not \s+) before "for" — same inline-span concatenation reason as above.
# Optional non-capturing group allows currency suffix between amount and "for":
#   "$267 CAD for 2 nights" (Airbnb .ca trip-total widget format).
_TRIP_TOTAL_RE = re.compile(
    r"\$\s*(\d{1,4}(?:,\d{3})?)(?:\s+(?:CAD|AUD|NZD|GBP|EUR))?\s*for\s+([2-9]|\d{2,})\s+nights?",
    re.I,
)

# Matches booking-widget trip total like:
#   "$330 CAD total" / "$330 total"
# Currency token is intentionally permissive (any 3-letter code) and optional.
_TOTAL_WIDGET_RE = re.compile(
    r"\$\s*(\d{1,6}(?:,\d{3})?)(?:\s+[A-Z]{3})?\s+total\b",
    re.I,
)

# JavaScript run inside the browser to classify price candidates in the booking widget.
# For each price element found near a "/night" label it records:
#   value       — numeric price
#   strikethrough — True if the element (or an ancestor ≤4 levels) has
#                   CSS text-decoration:line-through or is a <s>/<del> tag
#   domIndex    — position in the widget's element list (document order)
#
# The caller (Python) then picks the last non-strikethrough candidate, which is
# the current/discounted nightly price rather than the crossed-out original.
_BOOKING_WIDGET_PRICE_JS = """
() => {
  // Returns true if el or any ancestor up to 4 levels has line-through styling.
  function isLineThrough(el) {
    let node = el;
    for (let i = 0; i < 4 && node && node !== document.body; i++) {
      const s = window.getComputedStyle(node);
      const td = (s.textDecoration || '') + (s.textDecorationLine || '');
      if (td.includes('line-through')) return true;
      if (node.tagName === 'S' || node.tagName === 'DEL') return true;
      node = node.parentElement;
    }
    return false;
  }

  // Matches a bare price amount with optional symbol/prefix/suffix currency, e.g.:
  // "$465", "$1,234", "$363 CAD", "C$363", "€299", "299 EUR"
  const BARE_PRICE_RE = /^(?:[^\\d\\s]{1,4}\\s*)?(\\d{1,6}(?:,\\d{3})*(?:\\.\\d{1,2})?)\\s*(?:[A-Z]{3})?\\s*$/i;

  // Matches price+night label in a SINGLE element (Strategy 1, compound-text scan).
  // Handles: "$X /night" | "$X per night" | "$X for 1 night" | "$X for N nights"
  // \\s* (not \\s+) between digits and "for" so adjacent inline spans concatenated
  // without whitespace ("$321for 1 night") are still matched.
  const PRICE_NIGHT_RE = /\\$\\s*(\\d{1,4}(?:,\\d{3})?)\\s*(?:\\/\\s*night|per\\s+night|for\\s+\\d+\\s+nights?)/i;

  // Extracts the night count from "for N nights" text.
  const TRIP_NIGHTS_RE = /for\\s+(\\d+)\\s+nights?/i;

  // Per-night label in isolation (Strategy 2, sibling scan).
  // "/night" or "per night" -- tripNights always 1.
  const PER_NIGHT_ISO_RE = /^\\/\\s*night$|^per\\s+night$/i;

  // "for N nights" label in isolation -- captures N for trip-total division.
  // Matches "for 1 night" (N=1, per-night), "for 2 nights" (N=2, divide by 2), etc.
  // This is the MISSING case from the old code: Airbnb's discount widget shows
  // "$450 $321 for 1 night" with the label as a separate sibling span.
  const FOR_N_NIGHTS_ISO_RE = /^for\\s+(\\d+)\\s+nights?$/i;

  // Find the booking widget container.
  const WIDGET_SELS = [
    '[data-testid="book-it-default"]',
    '[data-testid="book-it-sidebar"]',
    '[data-testid="price-block"]',
    '[data-testid="book-it-price-breakdown"]',
    '[data-section-id="BOOK_IT_SIDEBAR"]',
    '[data-section-id="BOOK_IT_DEFAULT"]',
  ];
  let widget = null;
  for (const s of WIDGET_SELS) {
    widget = document.querySelector(s);
    if (widget) break;
  }
  if (!widget) return { candidates: [], hasWidget: false, widgetText: '', labelsFound: [] };

  // allEls: inline-only elements used for stable domIndex tracking.
  const allEls = Array.from(widget.querySelectorAll('span, b, strong, em'));
  // containerEls: includes div so a parent <div> with compound "$321for 1 night"
  // innerText is also found in the compound-text scan.
  const containerEls = Array.from(widget.querySelectorAll('span, b, strong, em, div'));

  // Strategy 1: compound-text scan.
  // Find elements whose innerText already contains BOTH price AND label.
  const nightContainers = [];
  for (const el of containerEls) {
    const t = (el.innerText || '').trim();
    if (PRICE_NIGHT_RE.test(t) && t.length < 250) {
      nightContainers.push(el);
    }
  }
  // Smallest innerText first so most-specific container wins.
  nightContainers.sort((a, b) =>
    (a.innerText || '').length - (b.innerText || '').length
  );

  const candidates = [];
  const seenEls = new Set();

  for (const container of nightContainers.slice(0, 3)) {
    const containerText = (container.innerText || '').trim();
    const tripM = containerText.match(TRIP_NIGHTS_RE);
    const tripNights = tripM ? parseInt(tripM[1], 10) : 1;

    const priceEls = container.querySelectorAll('span, b, strong, em');
    for (const el of priceEls) {
      if (seenEls.has(el)) continue;
      if (el.querySelectorAll('div, p, h1, h2, h3').length > 0) continue;
      const text = (el.textContent || '').trim();
      const m = text.match(BARE_PRICE_RE);
      if (!m) continue;
      const value = parseFloat(m[1].replace(',', ''));
      if (isNaN(value) || value < 10 || value > 150000) continue;
      seenEls.add(el);
      candidates.push({
        value,
        tripNights,
        strikethrough: isLineThrough(el),
        domIndex: allEls.indexOf(el),
      });
    }
    if (candidates.length > 0) break;
  }

  // Strategy 2: sibling label scan.
  // Modern Airbnb (discount-widget layout) renders price and label as SEPARATE
  // sibling elements, e.g.:
  //   <span style="text-decoration:line-through">$450</span>  <- crossed-out
  //   <span>$321</span>                                        <- current
  //   <span>for 1 night</span>                                 <- label (SEPARATE)
  //
  // The old code only searched for "/night" and "per night" labels in isolation,
  // missing "for 1 night" and "for N nights" entirely.
  //
  // Fix: scan ALL widget elements for ANY per-night label in isolation.
  // When found, search parent/grandparent for bare price spans.
  const labelsFound = [];
  if (candidates.length === 0) {
    const allWidgetEls = Array.from(widget.querySelectorAll('span, b, strong, em, div'));
    for (const labelEl of allWidgetEls) {
      const lt = (labelEl.innerText || labelEl.textContent || '').trim();
      if (!lt || lt.length > 80) continue;

      const isPerNightIso = PER_NIGHT_ISO_RE.test(lt);
      const forNightsMatch = lt.match(FOR_N_NIGHTS_ISO_RE);
      if (!isPerNightIso && !forNightsMatch) continue;

      labelsFound.push(lt);
      // tripNights from the label: "for 2 nights" -> 2; "/night"/"for 1 night" -> 1.
      const labelTripNights = forNightsMatch ? parseInt(forNightsMatch[1], 10) : 1;

      const searchRoots = [
        labelEl.parentElement,
        labelEl.parentElement?.parentElement,
      ].filter(Boolean);

      for (const root of searchRoots) {
        const priceEls = root.querySelectorAll('span, b, strong, em');
        for (const el of priceEls) {
          if (seenEls.has(el)) continue;
          if (el.querySelectorAll('div, p').length > 0) continue;
          const text = (el.textContent || '').trim();
          const m = text.match(BARE_PRICE_RE);
          if (!m) continue;
          const value = parseFloat(m[1].replace(',', ''));
          if (isNaN(value) || value < 10 || value > 150000) continue;
          seenEls.add(el);
          candidates.push({
            value,
            tripNights: labelTripNights,
            strikethrough: isLineThrough(el),
            domIndex: allEls.indexOf(el),
          });
        }
        if (candidates.length > 0) break;
      }
      if (candidates.length > 0) break;
    }
  }

  const widgetText = (widget.innerText || '').substring(0, 800);
  return {
    candidates,
    hasWidget: true,
    widgetText,
    nightContainersFound: nightContainers.length,
    usedSiblingFallback: candidates.length > 0 && nightContainers.length === 0,
    labelsFound,
  };
}

"""


def select_nightly_price_from_candidates(
    candidates: List[Dict[str, Any]],
) -> Optional[Tuple[float, str]]:
    """
    Select the current nightly price from structured DOM price candidates.

    Rules
    -----
    * Non-strikethrough candidates are the current/discounted price.
    * Strikethrough candidates are the original/crossed-out price — excluded.
    * When multiple non-strikethrough candidates exist, pick the **last** one
      in DOM order: Airbnb always places the original price before the discounted
      price in the DOM, so the last non-strikethrough entry is the discounted one.
    * If ``tripNights`` > 1 on the chosen candidate, the raw value is a trip total
      that must be divided by tripNights to produce the per-night figure.
    * Returns (price, price_kind) or None if no valid candidate exists.
      price_kind: "nightly_discounted" if strikethrough originals are also present,
                  "nightly_standard" otherwise.
    """
    if not candidates:
        return None

    non_st = [c for c in candidates if not c.get("strikethrough")]
    has_strikethrough = any(c.get("strikethrough") for c in candidates)

    if not non_st:
        return None

    # Sort by domIndex (document order); take the last (= discounted price when
    # a discount is active, or the sole price when there is no discount).
    non_st_sorted = sorted(non_st, key=lambda c: c.get("domIndex", 0))
    best = non_st_sorted[-1]
    raw_val = float(best["value"])
    trip_nights = int(best.get("tripNights") or 1)
    if trip_nights < 1:
        trip_nights = 1

    # Divide trip totals to arrive at per-night price.
    price_val = raw_val / trip_nights if trip_nights > 1 else raw_val

    if not (10 <= price_val <= 10000):
        return None

    price_kind = "nightly_discounted" if has_strikethrough else "nightly_standard"
    return price_val, price_kind


def _extract_text_price_matches(text: str) -> List[Tuple[int, float]]:
    """
    Scan ``text`` for all nightly-price and trip-total-price patterns and return
    a list of (position, per_night_price) tuples.

    Handles:
      * ``$X /night``        → per-night as-is
      * ``$X per night``     → per-night as-is
      * ``$X for 1 night``   → per-night as-is
      * ``$X for N nights``  → divide by N to get per-night (N >= 2)
      * ``X USD /night``     → per-night as-is

    "N nights minimum" labels are NOT matched because the pattern requires a
    dollar sign immediately before the amount.
    """
    matches: List[Tuple[int, float]] = []

    # Per-night patterns — use raw price value directly.
    for pat in _NIGHTLY_PRICE_RES:
        for m in pat.finditer(text):
            try:
                p = float(m.group(1).replace(",", ""))
                if 10 <= p <= 10000:
                    matches.append((m.start(), p))
            except Exception:
                continue

    # Trip-total pattern — divide by night count.
    for m in _TRIP_TOTAL_RE.finditer(text):
        try:
            raw = float(m.group(1).replace(",", ""))
            nights = int(m.group(2))
            if nights < 2:
                continue
            per_night = raw / nights
            if 10 <= per_night <= 10000:
                matches.append((m.start(), per_night))
        except Exception:
            continue

    return matches


def extract_nightly_price_from_listing_page(
    page,
    listing_url: str,
    checkin: str,
    checkout: str,
) -> Tuple[Optional[float], str]:
    """
    Navigate to a listing page with check-in/check-out dates appended and
    extract the displayed nightly price from the booking widget.

    Returns (nightly_price, confidence) where confidence is one of:
      "high"   — price found in ld+json structured data
      "medium" — price found via DOM data-testid selectors
      "low"    — price found via body-text regex
      "failed" — price could not be extracted

    Extraction layers run in confidence order and stop at first success.
    One retry is attempted on navigation failure before giving up.
    """
    try:
        stay_nights = max(
            1,
            (dt.strptime(checkout, "%Y-%m-%d") - dt.strptime(checkin, "%Y-%m-%d")).days,
        )
    except Exception:
        stay_nights = 1

    if hasattr(page, "get_listing_details"):
        # Client-backed path: fetch PDP payload for this exact date window and
        # parse nightly price directly. This avoids mixing async Playwright
        # page objects with sync extraction logic.
        def _extract_from_rendered_html_via_client() -> Tuple[Optional[float], str]:
            if not hasattr(page, "browse_url_html"):
                return None, "failed"
            try:
                parsed_rb = urlparse(listing_url)
                url_with_dates_rb = (
                    f"{parsed_rb.scheme}://{parsed_rb.netloc}{parsed_rb.path}"
                    f"?check_in={checkin}&check_out={checkout}&guests=1&adults=1"
                )
                nav = page.browse_url_html(
                    url_with_dates_rb,
                    label="pdp_rendered_html_fallback",
                    wait_until="domcontentloaded",
                    timeout=35000,
                )
                html = str((nav or {}).get("html") or "")
                if not html:
                    return None, "failed"

                # Try price-near-night patterns first.
                matches = _extract_text_price_matches(html[:50000])
                if matches:
                    matches.sort(key=lambda x: x[0])
                    return matches[-1][1], "low"

                # Fallback: strip tags and retry.
                plain = re.sub(r"<[^>]+>", " ", html[:50000])
                matches2 = _extract_text_price_matches(plain)
                if matches2:
                    matches2.sort(key=lambda x: x[0])
                    return matches2[-1][1], "low"
                return None, "failed"
            except Exception:
                return None, "failed"

        try:
            listing_id = extract_listing_id_from_url(listing_url)
            if not listing_id:
                return None, "failed"
            pdp = page.get_listing_details(
                str(listing_id),
                checkin=checkin,
                checkout=checkout,
                adults=1,
            )
            parsed_pdp = parse_pdp_response(
                pdp,
                str(listing_id),
                safe_domain_base(listing_url),
            )
            nightly = parsed_pdp.get("nightly_price")
            if nightly is None:
                total = parsed_pdp.get("total_price")
                if isinstance(total, (int, float)) and total > 0:
                    nightly = round(float(total) / max(1, stay_nights), 2)
            if isinstance(nightly, (int, float)) and nightly > 0:
                return float(nightly), "high"
            # PDP returned but had no usable price: switch immediately to rendered HTML fallback.
            return _extract_from_rendered_html_via_client()
        except Exception as exc:
            _msg = str(exc or "").lower()
            _cdp_connect_failed = (
                "connect_over_cdp" in _msg
                or "retrieving websocket url" in _msg
                or "devtools/browser" in _msg
            )
            if _cdp_connect_failed:
                logger.warning(
                    "[price_extract] PDP client CDP connect failed; using rendered HTML fallback "
                    "listing=%s checkin=%s checkout=%s",
                    listing_url,
                    checkin,
                    checkout,
                )
                return _extract_from_rendered_html_via_client()
            # Non-CDP PDP errors: switch immediately to rendered HTML fallback.
            return _extract_from_rendered_html_via_client()

    parsed = urlparse(listing_url)
    # Reconstruct with only check_in / check_out — drop any pre-existing params.
    url_with_dates = (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        f"?check_in={checkin}&check_out={checkout}"
    )

    # Navigate with one retry on transient failure
    for attempt in range(2):
        try:
            _human_pause_sync_page(page)
            page.goto(url_with_dates, wait_until="domcontentloaded", timeout=15000)
            break
        except Exception as exc:
            if attempt == 1:
                logger.warning(
                    f"[benchmark] Failed to navigate to benchmark page after retry: {exc}"
                )
                return None, "failed"
            logger.warning(
                f"[benchmark] Navigation attempt {attempt + 1} failed, retrying in 2s: {exc}"
            )
            time.sleep(2.0)

    # Phase 1 — wait for the booking widget container to appear in the DOM.
    # React hydration happens after domcontentloaded, so the data-testid elements
    # may not yet be present immediately after page.goto() returns.
    _WIDGET_SELECTOR = ", ".join([
        '[data-testid="book-it-default"]',
        '[data-testid="book-it-sidebar"]',
        '[data-testid="price-block"]',
        '[data-testid="book-it-price-breakdown"]',
        '[data-section-id="BOOK_IT_SIDEBAR"]',
        '[data-section-id="BOOK_IT_DEFAULT"]',
    ])
    try:
        page.wait_for_selector(_WIDGET_SELECTOR, timeout=6000)
    except Exception:
        pass  # Widget may not appear (blocked, unavailable dates) — proceed anyway

    # Phase 2 — wait for actual price content to load inside the widget.
    # After the widget container mounts, Airbnb fires a pricing XHR that can take
    # 2–4 s to resolve. Without this step the widget is present but shows an empty
    # or loading-skeleton state ("CHECK-IN ... loading"), causing both L2 DOM scan
    # and the regex fallback to find nothing.
    # We poll (up to 8 s) until a dollar price ($X) appears; stop as soon as it does.
    _PRICE_READY_JS = r"""() => {
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
            if (t && /\$\s*\d{2,}/.test(t)) return true;
        }
        return false;
    }"""
    try:
        page.wait_for_function(_PRICE_READY_JS, timeout=8000, polling=400)
    except Exception:
        pass  # Timeout or price genuinely absent — proceed to extraction layers

    # ── Layer 1: ld+json structured data (high confidence) ────────────────
    # Airbnb's LodgingBusiness schema includes rating and address but deliberately
    # omits per-night pricing. This layer is a forward-looking check: if Airbnb
    # ever adds priceSpecification to their ld+json, we capture it at high confidence.
    # In current practice this layer almost always falls through to L2.
    try:
        ld_texts: List[str] = page.evaluate(
            """() => Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                         .map(s => s.textContent || '')
                         .filter(Boolean)"""
        )
        for blob in (ld_texts or []):
            try:
                obj = json.loads(blob)
                items = obj if isinstance(obj, list) else [obj]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    ld_candidates = item.get("priceSpecification") or item.get("offers") or []
                    if isinstance(ld_candidates, dict):
                        ld_candidates = [ld_candidates]
                    for spec in (ld_candidates if isinstance(ld_candidates, list) else []):
                        if not isinstance(spec, dict):
                            continue
                        for key in ("price", "lowPrice", "minPrice"):
                            raw = spec.get(key)
                            if raw is None:
                                continue
                            try:
                                price = float(str(raw).replace(",", ""))
                                if 10 <= price <= 10000:
                                    logger.info(
                                        f"[benchmark] ld+json price=${price} "
                                        f"from {listing_url}"
                                    )
                                    return price, "high"
                                else:
                                    logger.warning(
                                        f"[benchmark] ld+json price ${price} out of range "
                                        f"(raw={raw!r}), skipping"
                                    )
                            except Exception:
                                continue
            except Exception:
                continue
    except Exception:
        pass

    # ── Layer 2: DOM price-candidate classification (medium confidence) ────
    # Runs _BOOKING_WIDGET_PRICE_JS to find price elements near the "/night"
    # label and classify each as strikethrough (original/crossed-out) or
    # current (discounted or standard).  select_nightly_price_from_candidates()
    # then picks the last non-strikethrough price in DOM order, which is the
    # discounted price when a discount is active.
    try:
        dom_result: Dict[str, Any] = page.evaluate(_BOOKING_WIDGET_PRICE_JS)
        if isinstance(dom_result, dict):
            dom_candidates: List[Dict[str, Any]] = dom_result.get("candidates") or []
            widget_text: str = dom_result.get("widgetText") or ""
            has_widget: bool = bool(dom_result.get("hasWidget"))
            night_containers_found: int = dom_result.get("nightContainersFound", -1)
            used_sibling_fallback: bool = bool(dom_result.get("usedSiblingFallback"))
            labels_found: List[str] = dom_result.get("labelsFound") or []

            # Always log at INFO so we can diagnose without enabling DEBUG
            logger.info(
                f"[price_extract] L2 widget={has_widget} "
                f"containers={night_containers_found} "
                f"siblingFallback={used_sibling_fallback} "
                f"candidates={len(dom_candidates)} "
                f"labels={labels_found or '(none)'} "
                f"url={listing_url}"
            )
            if dom_candidates:
                _cand_summary = [
                    f"${c['value']}{'(st)' if c.get('strikethrough') else ''}"
                    f"x{c.get('tripNights',1)}n"
                    for c in dom_candidates[:6]
                ]
                logger.info(f"[price_extract] L2 candidates: {_cand_summary}")

            selected = select_nightly_price_from_candidates(dom_candidates)
            if selected is not None:
                price, price_kind = selected
                non_st_count = sum(1 for c in dom_candidates if not c.get("strikethrough"))
                st_count = sum(1 for c in dom_candidates if c.get("strikethrough"))
                logger.info(
                    f"[price_extract] L2 DOM price=${price} kind={price_kind} "
                    f"(non_st={non_st_count} st={st_count} sibling={used_sibling_fallback})"
                )
                return price, "medium"

            # Structured scan found nothing — fall back to regex on the widget text.
            if widget_text:
                # Check for loading/skeleton state: widget has text but no price yet
                _wt_lower = widget_text.lower()
                _looks_loading = (
                    "$" not in widget_text
                    and any(tok in _wt_lower for tok in ("loading", "check-in", "check in", "check-out"))
                )
                if _looks_loading:
                    logger.warning(
                        f"[price_extract] L2 widget appears still loading "
                        f"(no $ in text). widgetText[:120]={widget_text[:120]!r}"
                    )
                text_matches = _extract_text_price_matches(widget_text)
                if text_matches:
                    text_matches.sort(key=lambda x: x[0])
                    price = text_matches[-1][1]
                    logger.info(
                        f"[price_extract] L2 widget-text regex price=${price} "
                        f"(last of {len(text_matches)} matches)"
                    )
                    return price, "medium"
                total_matches: List[Tuple[int, float]] = []
                for m in _TOTAL_WIDGET_RE.finditer(widget_text):
                    try:
                        total_price = float(m.group(1).replace(",", ""))
                    except Exception:
                        continue
                    if 10 <= total_price <= 200000:
                        total_matches.append((m.start(), total_price))
                if total_matches:
                    total_matches.sort(key=lambda x: x[0])
                    total_price = total_matches[-1][1]
                    nightly = round(total_price / stay_nights, 2)
                    if 10 <= nightly <= 10000:
                        logger.info(
                            f"[price_extract] L2 widget-total price=${nightly} "
                            f"(total={total_price}, nights={stay_nights}, matches={len(total_matches)})"
                        )
                        return nightly, "medium"
                else:
                    logger.warning(
                        f"[price_extract] L2 widget found, labels={labels_found or '(none)'}, "
                        f"0 regex matches. "
                        f"widgetText[:300]={widget_text[:300]!r}"
                    )
            else:
                if has_widget:
                    logger.warning(
                        f"[price_extract] L2 widget found but widgetText is empty "
                        f"(still hydrating?). url={listing_url}"
                    )

            if not has_widget:
                logger.warning(
                    f"[price_extract] L2 no booking widget found via any data-testid. "
                    f"url={listing_url}"
                )
    except Exception as exc:
        logger.warning(f"[price_extract] Layer 2 JS evaluation failed: {exc}")

    # ── Layer 3: body-text regex (low confidence) ──────────────────────────
    # Most fragile — depends on rendered text order, but catches edge cases
    # where booking widget data-testid attributes are absent.
    try:
        body = page.inner_text("body", timeout=8000)
    except Exception:
        logger.warning("[benchmark] Failed to read body text from listing page")
        return None, "failed"

    if not body:
        return None, "failed"

    # Scan the first 8 000 characters where the booking widget typically renders.
    # Collect ALL matches then pick the LAST one that appears before any
    # "Show price breakdown" section — the discounted price always follows the
    # original in text order, so the last pre-breakdown match is the current price.
    search_area = body[:8000]

    breakdown_pos = search_area.lower().find("show price breakdown")
    cutoff = breakdown_pos if breakdown_pos > 0 else len(search_area)

    body_matches: List[Tuple[int, float]] = _extract_text_price_matches(search_area)

    if body_matches:
        body_matches.sort(key=lambda x: x[0])
        pre_breakdown = [(pos, p) for pos, p in body_matches if pos < cutoff]
        chosen_pos, price = (pre_breakdown[-1] if pre_breakdown else body_matches[-1])
        logger.info(
            f"[price_extract] L3 body-text price=${price} "
            f"(last of {len(body_matches)} matches, pre_breakdown={len(pre_breakdown)}) "
            f"from {listing_url}"
        )
        return price, "low"

    # All three layers failed — log a body-text sample so the next run can diagnose
    # whether the price IS present in the page but in a non-matching format, or
    # whether the page returned an error/unavailable state.
    logger.warning(
        f"[price_extract] All layers failed for {listing_url}. "
        f"body[:400]={search_area[:400]!r}"
    )
    try:
        raw_html = page.content()
        logger.info(
            f"[price_extract_raw_html] url={listing_url} "
            f"checkin={checkin} checkout={checkout} html={raw_html}"
        )
    except Exception as exc:
        logger.warning(f"[price_extract_raw_html] failed to read page.content(): {exc}")
    return None, "failed"


# ---------------------------------------------------------------------------
# Target listing live price capture — standalone browser session
# ---------------------------------------------------------------------------


def capture_target_live_price(
    listing_url: str,
    checkin: str,
    checkout: str,
    cdp_url: Optional[str] = None,
    cdp_connect_timeout_ms: int = 15000,
    client: Optional[Any] = None,
    allow_retry_matrix: bool = True,
) -> Dict[str, Any]:
    """
    Open a short Playwright session to extract the target listing's current
    nightly price for the given checkin/checkout dates.

    Date basis: checkin = first day of the report window, checkout = checkin+1.
    This gives the "price as shown to a guest booking that specific night."

    Returns a dict with:
      observedListingPrice           — int | None
      observedListingPriceDate       — str (checkin, YYYY-MM-DD)
      observedListingPriceCapturedAt — str (ISO-8601 UTC)
      observedListingPriceSource     — "ld_json" | "booking_widget" | "body_text" | None
      observedListingPriceConfidence — "high" | "medium" | "low" | "failed"
      livePriceStatus                — "captured" | "scrape_failed" | "no_price_found"
      livePriceStatusReason          — str
    """
    from datetime import datetime, timezone, timedelta

    captured_at = datetime.now(timezone.utc).isoformat()

    _CONFIDENCE_TO_SOURCE = {
        "high": "ld_json",
        "medium": "booking_widget",
        "low": "body_text",
    }

    listing_url = normalize_airbnb_url(listing_url)

    try:
        if client is None:
            from worker.scraper.airbnb_client import AirbnbClient
            use_deepbnb_live = str(
                os.getenv("AIRBNB_USE_DEEPBNB_FOR_LIVE_PRICE", "1")
            ).strip().lower() in ("1", "true", "yes", "on")
            client = AirbnbClient(
                {
                    "CHECKIN": checkin,
                    "CHECKOUT": checkout,
                    "ADULTS": 1,
                    # Keep current behavior by default (Deepbnb enabled). This env switch
                    # allows explicit Playwright-only live-capture debugging when needed.
                    "USE_DEEPBNB_BACKEND": use_deepbnb_live,
                }
            )

        listing_id = extract_listing_id_from_url(listing_url)
        if not listing_id:
            raise ValueError(f"Unable to parse listing_id from URL: {listing_url}")

        # Prefer browser-rendered HTML extraction for live price capture.
        # This restores pre-GraphQL behavior where booking-widget DOM is source of truth.
        try:
            _html_price, _html_conf = extract_nightly_price_from_listing_page(
                client,
                listing_url=listing_url,
                checkin=checkin,
                checkout=checkout,
            )
            if isinstance(_html_price, (int, float)) and _html_price > 0:
                return {
                    "observedListingPrice": round(float(_html_price)),
                    "observedListingPriceDate": checkin,
                    "observedListingPriceCapturedAt": captured_at,
                    "observedListingPriceSource": _CONFIDENCE_TO_SOURCE.get(_html_conf, "unknown"),
                    "observedListingPriceConfidence": _html_conf,
                    "livePriceStatus": "captured",
                    "livePriceStatusReason": (
                        f"Nightly price captured from browser HTML for {checkin}/{checkout} "
                        f"(confidence={_html_conf})"
                    ),
                }
        except Exception:
            pass

        if not allow_retry_matrix:
            return {
                "observedListingPrice": None,
                "observedListingPriceDate": checkin,
                "observedListingPriceCapturedAt": captured_at,
                "observedListingPriceSource": None,
                "observedListingPriceConfidence": "failed",
                "livePriceStatus": "no_price_found",
                "livePriceStatusReason": (
                    f"No nightly price found on listing page for {checkin}/{checkout} "
                    "(single-attempt mode)"
                ),
            }

        playwright_client: Optional[Any] = None

        def _extract_price_for_window(
            _checkin: str,
            _checkout: str,
            _adults: int,
        ) -> tuple[Optional[float], str, Dict[str, Any]]:
            nonlocal playwright_client
            _meta: Dict[str, Any] = {
                "checkin": _checkin,
                "checkout": _checkout,
                "adults": _adults,
                "nightly": None,
                "total": None,
                "has_errors": False,
                "section_ids": [],
                "backend": "playwright",
            }

            def _extract_with_client(_client_obj: Any, _backend_label: str) -> tuple[Optional[float], Optional[float], List[str], bool]:
                _pdp_data = _client_obj.get_listing_details(
                    str(listing_id),
                    checkin=_checkin,
                    checkout=_checkout,
                    adults=_adults,
                )
                try:
                    _raw_preview = json.dumps(_pdp_data, ensure_ascii=False, default=str)
                except Exception:
                    _raw_preview = str(_pdp_data)
                if len(_raw_preview) > 5000:
                    _raw_preview = _raw_preview[:5000] + "...<truncated>"
                _has_errors = bool((_pdp_data or {}).get("errors"))
                _sections = (
                    (((_pdp_data or {}).get("data") or {}).get("presentation") or {})
                    .get("stayProductDetailPage", {})
                    .get("sections", {})
                    .get("sections", [])
                )
                _section_ids: List[str] = []
                if isinstance(_sections, list):
                    _section_ids = [
                        str(s.get("sectionId"))
                        for s in _sections
                        if isinstance(s, dict) and s.get("sectionId") is not None
                    ][:8]
                logger.info(
                    "[target_live_price_raw] listing=%s checkin=%s checkout=%s adults=%s backend=%s has_errors=%s section_ids=%s",
                    listing_id,
                    _checkin,
                    _checkout,
                    _adults,
                    _backend_label,
                    _has_errors,
                    _section_ids,
                )
                logger.info(
                    "[target_live_price_pdp_payload] listing=%s checkin=%s checkout=%s adults=%s backend=%s payload=%s",
                    listing_id,
                    _checkin,
                    _checkout,
                    _adults,
                    _backend_label,
                    _raw_preview,
                )
                _parsed = parse_pdp_response(_pdp_data, str(listing_id), safe_domain_base(listing_url))
                return _parsed.get("nightly_price"), _parsed.get("total_price"), _section_ids, _has_errors

            _nightly, _total, _section_ids, _has_errors = _extract_with_client(
                client,
                _meta["backend"],
            )
            _meta["has_errors"] = _has_errors
            _meta["section_ids"] = _section_ids
            _meta["nightly"] = _nightly
            _meta["total"] = _total

            # Retry this same window with a dedicated Playwright-only client before giving up.
            if (_nightly is None and _total is None):
                try:
                    if playwright_client is None:
                        from worker.scraper.airbnb_client import AirbnbClient
                        playwright_client = AirbnbClient(
                            {
                                "CHECKIN": _checkin,
                                "CHECKOUT": _checkout,
                                "ADULTS": _adults,
                                "USE_DEEPBNB_BACKEND": False,
                            }
                        )
                    _fb_nightly, _fb_total, _fb_section_ids, _fb_has_errors = _extract_with_client(
                        playwright_client,
                        "playwright_fallback",
                    )
                    _meta["fallback_backend"] = "playwright"
                    _meta["fallback_section_ids"] = _fb_section_ids
                    _meta["fallback_has_errors"] = _fb_has_errors
                    _meta["fallback_nightly"] = _fb_nightly
                    _meta["fallback_total"] = _fb_total
                    if _fb_nightly is not None or _fb_total is not None:
                        _meta["backend"] = "playwright_fallback"
                        _nightly = _fb_nightly
                        _total = _fb_total
                        _meta["nightly"] = _nightly
                        _meta["total"] = _total
                        _meta["section_ids"] = _fb_section_ids
                        _meta["has_errors"] = _fb_has_errors
                except Exception as _fb_exc:
                    _meta["fallback_backend"] = "playwright"
                    _meta["fallback_error"] = str(_fb_exc)[:300]

            _price = _nightly
            if _price is None and isinstance(_total, (int, float)):
                _nights = max(1, (dt.strptime(_checkout, "%Y-%m-%d") - dt.strptime(_checkin, "%Y-%m-%d")).days)
                _price = round(float(_total) / _nights, 2)
            return (_price if isinstance(_price, (int, float)) and _price > 0 else None), ("high" if _price else "failed"), _meta

        # Retry matrix: same window adults=1, then adults=2, then next-day window.
        attempts: List[tuple[str, str, int]] = [(checkin, checkout, 1), (checkin, checkout, 2)]
        try:
            _d0 = dt.strptime(checkin, "%Y-%m-%d").date()
            _d1 = dt.strptime(checkout, "%Y-%m-%d").date()
            if _d0 <= datetime.now(timezone.utc).date():
                _span = max(1, (_d1 - _d0).days)
                _next_checkin = (_d0 + timedelta(days=1)).strftime("%Y-%m-%d")
                _next_checkout = (_d0 + timedelta(days=1 + _span)).strftime("%Y-%m-%d")
                attempts.append((_next_checkin, _next_checkout, 2))
        except Exception:
            pass

        price: Optional[float] = None
        confidence = "failed"
        used_checkin = checkin
        used_checkout = checkout
        debug_attempts: List[Dict[str, Any]] = []

        for a_checkin, a_checkout, a_adults in attempts:
            a_price, a_conf, a_meta = _extract_price_for_window(a_checkin, a_checkout, a_adults)
            debug_attempts.append(a_meta)
            if isinstance(a_price, (int, float)) and a_price > 0:
                price = a_price
                confidence = a_conf
                used_checkin = a_checkin
                used_checkout = a_checkout
                break

        if price is None:
            logger.warning(
                f"[target_live_price] No price extracted for listing_id={listing_id}. "
                f"attempts={debug_attempts}"
            )
            try:
                from urllib.parse import urlencode
                _render_url = (
                    f"{safe_domain_base(listing_url)}/rooms/{listing_id}?"
                    + urlencode(
                        {
                            "check_in": checkin,
                            "check_out": checkout,
                            "adults": 1,
                            "guests": 1,
                        }
                    )
                )
                if hasattr(client, "browse_url_html"):
                    _render = client.browse_url_html(
                        _render_url,
                        label="target_live_price_rendered_html",
                        wait_until="commit",
                        timeout=35000,
                    )
                    _html = str((_render or {}).get("html") or "")
                    _preview = _html.replace("\n", " ").replace("\r", " ")
                    if len(_preview) > 5000:
                        _preview = _preview[:5000] + "...<truncated>"
                    logger.warning(
                        "[target_live_price_rendered_html] listing=%s url=%s final_url=%s status=%s html_len=%s html_preview=%s",
                        listing_id,
                        _render_url,
                        str((_render or {}).get("final_url") or ""),
                        str((_render or {}).get("status")),
                        len(_html),
                        _preview,
                    )
            except Exception as _render_exc:
                logger.warning(
                    "[target_live_price_rendered_html] listing=%s snapshot failed: %s",
                    listing_id,
                    _render_exc,
                )
    except Exception as exc:
        logger.warning(f"[target_live_price] HTTP extraction failed: {exc}")
        return {
            "observedListingPrice": None,
            "observedListingPriceDate": checkin,
            "observedListingPriceCapturedAt": captured_at,
            "observedListingPriceSource": None,
            "observedListingPriceConfidence": "failed",
            "livePriceStatus": "scrape_failed",
            "livePriceStatusReason": str(exc)[:300],
        }

    if price is None:
        return {
            "observedListingPrice": None,
            "observedListingPriceDate": checkin,
            "observedListingPriceCapturedAt": captured_at,
            "observedListingPriceSource": None,
            "observedListingPriceConfidence": "failed",
            "livePriceStatus": "no_price_found",
            "livePriceStatusReason": f"No nightly price found on listing page for {checkin}/{checkout}",
        }

    return {
        "observedListingPrice": round(price),
        "observedListingPriceDate": used_checkin,
        "observedListingPriceCapturedAt": captured_at,
        "observedListingPriceSource": _CONFIDENCE_TO_SOURCE.get(confidence, "unknown"),
        "observedListingPriceConfidence": confidence,
        "livePriceStatus": "captured",
        "livePriceStatusReason": f"Nightly price captured for {used_checkin}/{used_checkout} (confidence={confidence})",
    }
