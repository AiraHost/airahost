"""
Airbnb StaysPdpSections crawler.

Fetches the PDP (property detail page) sections for an Airbnb listing via the
public `StaysPdpSections` GraphQL persisted-query endpoint and parses out the
useful structured fields (title, location, ratings, host, house rules,
safety features, cancellation policy, ...).

Usage:
    python airbnb_crawler.py 47273102
    python airbnb_crawler.py https://www.airbnb.com/rooms/47273102
    python airbnb_crawler.py 47273102 --raw --out listing.json
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# Constants pulled from the browser request.
#
# The API key below is Airbnb's public web client key (visible in every browser
# request). The sha256 hash identifies a specific persisted GraphQL query and
# can change when Airbnb ships a new frontend build -- if requests start
# returning "PersistedQueryNotFound", grab a fresh hash from the network tab.
# ---------------------------------------------------------------------------
API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"
PERSISTED_QUERY_HASH = (
    "cf2b57ebaa6b7d053f0979d843a2cc622fe06f6dfed6ea5cd13ebc3676b72961"
)
OPERATION_NAME = "StaysPdpSections"
ENDPOINT = "https://www.airbnb.com/api/v3/{op}/{hash}"

DEFAULT_SECTION_IDS = [
    "BOOK_IT_CALENDAR_SHEET",
    "CANCELLATION_POLICY_PICKER_MODAL",
    "BOOK_IT_FLOATING_FOOTER",
    "POLICIES_DEFAULT",
    "BOOK_IT_SIDEBAR",
    "BOOK_IT_NAV",
    "AMENITIES_DEFAULT",
]

# Every fragment toggle the persisted query declares. A persisted query is
# validated against its stored document, so *all* of these must be supplied or
# the request fails with a GraphQL ValidationError. Copied verbatim from a real
# browser request; the "PdpMigration" (legacy) flags are False and the "Gp"
# (guest platform) flags are True.
FRAGMENT_TOGGLES: dict[str, bool] = {
    "includePdpMigrationAccessibilityFeaturesModalFragment": False,
    "includeGpAccessibilityFeaturesFragment": True,
    "includePdpMigrationAccessibilityFeaturesPreviewCarouselFragment": False,
    "includePdpMigrationLuxeServicesFragment": False,
    "includeGpLuxeServicesFragment": True,
    "includeGpAdminBannerFragment": True,
    "includePdpMigrationBookItNavFragment": False,
    "includeGpBookItFragment": True,
    "includePdpMigrationAmenitiesFragment": False,
    "includeGpAmenitiesFragment": True,
    "includeGpCancellationPolicyPickerModalFragment": True,
    "includePdpMigrationAvailabilityCalendarInlineFragment": False,
    "includeGpAvailabilityCalendarInlineFragment": True,
    "includePdpMigrationAvailabilityCalendarFragment": False,
    "includeGpAvailabilityCalendarFragment": True,
    "includePdpMigrationDescriptionFragment": False,
    "includeGpDescriptionFragment": True,
    "includePdpMigrationHeroFragment": False,
    "includeGpHeroFragment": True,
    "includePdpMigrationHighlightsCompactFragment": False,
    "includeGpHighlightsCompactFragment": True,
    "includePdpMigrationHighlightsFragment": False,
    "includeGpHighlightsFragment": True,
    "includePdpMigrationLocationPdpFragment": False,
    "includeGpLocationPdpFragment": True,
    "includePdpMigrationMeetYourHostFragment": False,
    "includeGpMeetYourHostFragment": True,
    "includePdpMigrationMessageBannerFragment": False,
    "includeGpMessageBannerFragment": True,
    "includePdpMigrationNavFragment": False,
    "includeGpNavFragment": True,
    "includePdpMigrationNavMobileFragment": False,
    "includeGpNavMobileFragment": True,
    "includePdpMigrationBookItFloatingFooterFragment": False,
    "includePdpMigrationBookItSidebarFragment": False,
    "includePdpMigrationBookItCalendarSheetFragment": False,
    "includePdpMigrationBookItNonExperiencedGuestFragment": False,
    "includeGpBookItNonExperiencedGuestFragment": True,
    "includePdpMigrationBathroomFragment": False,
    "includeGpBathroomFragment": True,
    "includePdpMigrationOverviewV2Fragment": False,
    "includeGpOverviewV2Fragment": True,
    "includePdpMigrationPropertyAvailableRoomsFragment": False,
    "includeGpPropertyAvailableRoomsFragment": True,
    "includePdpMigrationReviewsHighlightBannerFragment": False,
    "includeGpReviewsHighlightBannerFragment": True,
    "includePdpMigrationHostOverviewDefaultFragment": False,
    "includeGpHostOverviewDefaultFragment": True,
    "includePdpMigrationNonExperiencedGuestLearnMoreModalFragment": False,
    "includeGpNonExperiencedGuestLearnMoreModalFragment": True,
    "includePdpMigrationReportToAirbnbFragment": False,
    "includeGpReportToAirbnbFragment": True,
    "includePdpMigrationReviewsFragment": False,
    "includeGpReviewsFragment": True,
    "includePdpMigrationReviewsEmptyFragment": False,
    "includeGpReviewsEmptyFragment": True,
    "includePdpMigrationSeoLinksFragment": False,
    "includeGpSeoLinksFragment": True,
    "includePdpMigrationSleepingArrangementFragment": False,
    "includeGpSleepingArrangementFragment": True,
    "includePdpMigrationSleepingArrangementImagesFragment": False,
    "includeGpSleepingArrangementImagesFragment": True,
    "includePdpMigrationTitleFragment": False,
    "includeGpTitleFragment": True,
    "includeGpUgcTranslationFragment": True,
    "includePdpMigrationPoliciesFragment": False,
    "includeGpPoliciesFragment": True,
    "includePdpMigrationMarqueeBookItFloatingFooterFragment": False,
    "includeGpMarqueeBookItFloatingFooterFragment": True,
    "includePdpMigrationMarqueeBookItNavFragment": False,
    "includeGpMarqueeBookItNavFragment": True,
    "includePdpMigrationMarqueeBookItSidebarFragment": False,
    "includeGpMarqueeBookItSidebarFragment": True,
    "includePdpMigrationOnlyOnBookItFragment": False,
    "includePdpMigrationOnlyOnBookItNavFragment": False,
    "includePdpMigrationPdpEducationFragment": False,
}


class AntiBotError(RuntimeError):
    """Raised when Airbnb serves an anti-bot challenge instead of data.

    Signalled by a 403/429/503, an HTML challenge page in place of JSON, or an
    "airlock" marker in the body. Recovering means re-establishing a trusted
    browser session (see get_hash.extract_hash), not just retrying.
    """


class StaleHashError(RuntimeError):
    """Raised when the persisted-query hash is no longer accepted."""


def _b64(value: str) -> str:
    """URL-safe-free standard base64 encode of a UTF-8 string."""
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def parse_listing_id(value: str) -> str:
    """Accept a raw numeric id or any Airbnb rooms URL and return the id."""
    value = value.strip()
    if value.isdigit():
        return value
    match = re.search(r"/rooms/(?:plus/)?(\d+)", value)
    if match:
        return match.group(1)
    # Fall back to the first run of digits.
    match = re.search(r"(\d{3,})", value)
    if match:
        return match.group(1)
    raise ValueError(f"Could not extract a listing id from: {value!r}")


def parse_listing(value: str) -> tuple[str, Optional[str], Optional[str]]:
    """Return (listing_id, check_in, check_out).

    Dates are read from ``check_in``/``check_out`` query params if the value is
    a URL that carries them (as Airbnb's share URLs do); otherwise None.
    """
    listing_id = parse_listing_id(value)
    check_in = check_out = None
    if "check_in" in value or "check_out" in value:
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(value).query)
        check_in = (qs.get("check_in") or [None])[0]
        check_out = (qs.get("check_out") or [None])[0]
    return listing_id, check_in, check_out


class AirbnbPdpClient:
    """Thin client around the StaysPdpSections persisted query."""

    def __init__(
        self,
        api_key: str = API_KEY,
        persisted_query_hash: str = PERSISTED_QUERY_HASH,
        locale: str = "en",
        currency: str = "USD",
        timeout: int = 30,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key
        self.persisted_query_hash = persisted_query_hash
        self.locale = locale
        self.currency = currency
        self.timeout = timeout
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "x-airbnb-api-key": self.api_key,
            "x-airbnb-graphql-platform": "web",
            "x-airbnb-graphql-platform-client": "minimalist-niobe",
            "x-airbnb-supports-airlock-v2": "true",
            "x-csrf-without-token": "1",
            # Unique per-request ids; Airbnb accepts arbitrary values here.
            "x-airbnb-client-action-id": str(uuid.uuid4()),
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
        }

    def _build_body(
        self,
        listing_id: str,
        section_ids: Optional[list[str]] = None,
        adults: int = 1,
        check_in: Optional[str] = None,
        check_out: Optional[str] = None,
    ) -> dict[str, Any]:
        section_ids = section_ids or DEFAULT_SECTION_IDS
        return {
            "operationName": OPERATION_NAME,
            "variables": {
                "id": _b64(f"StayListing:{listing_id}"),
                "demandStayListingId": _b64(f"DemandStayListing:{listing_id}"),
                "pdpSectionsRequest": {
                    "adults": str(adults),
                    "amenityFilters": None,
                    "bypassTargetings": False,
                    "categoryTag": None,
                    "causeId": None,
                    "children": None,
                    "disasterId": None,
                    "discountedGuestFeeVersion": None,
                    "federatedSearchId": None,
                    "forceBoostPriorityMessageType": None,
                    "hostPreview": False,
                    "infants": None,
                    "interactionType": None,
                    "layouts": ["SIDEBAR", "SINGLE_COLUMN"],
                    "pets": 0,
                    "pdpTypeOverride": None,
                    "photoId": None,
                    "preview": False,
                    "previousStateCheckIn": None,
                    "previousStateCheckOut": None,
                    "priceDropSource": None,
                    "partner": None,
                    "partnerProgram": None,
                    "privateBooking": False,
                    "promotionUuid": None,
                    "relaxedAmenityIds": None,
                    "searchId": None,
                    "selectedCancellationPolicyId": None,
                    "selectedRatePlanId": None,
                    "splitStays": None,
                    "staysBookingMigrationEnabled": False,
                    "translateUgc": None,
                    "useNewSectionWrapperApi": False,
                    "sectionIds": section_ids,
                    "checkIn": check_in,
                    "checkOut": check_out,
                    "p3ImpressionId": None,
                },
                "categoryTag": None,
                "federatedSearchId": None,
                "federatedSearchSessionId": None,
                "p3ImpressionId": None,
                "photoId": None,
                "amenityIds": None,
                "dateRange": None,
                "guestCounts": None,
                "numberOfChildren": None,
                "numberOfInfants": None,
                "numberOfPets": None,
                # Persisted queries validate the *exact* declared variable set,
                # so every fragment toggle must be present. "PdpMigration"
                # variants are the legacy path (off); "Gp" (guest platform)
                # variants are the current one (on).
                **FRAGMENT_TOGGLES,
            },
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": self.persisted_query_hash,
                }
            },
        }

    def fetch(
        self,
        listing_id: str,
        section_ids: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Fetch and return the raw JSON response for a listing."""
        url = ENDPOINT.format(op=OPERATION_NAME, hash=self.persisted_query_hash)
        params = {
            "operationName": OPERATION_NAME,
            "locale": self.locale,
            "currency": self.currency,
        }
        body = self._build_body(listing_id, section_ids=section_ids, **kwargs)
        resp = self.session.post(
            url,
            params=params,
            headers=self._headers(),
            json=body,
            timeout=self.timeout,
        )

        # --- anti-bot detection ------------------------------------------
        # Airbnb serves challenges as 403/429/503, or as an HTML "airlock"
        # page where a JSON body is expected.
        if resp.status_code in (403, 429, 503):
            raise AntiBotError(
                f"anti-bot challenge (HTTP {resp.status_code})"
            )
        content_type = resp.headers.get("content-type", "")
        if "application/json" not in content_type:
            snippet = resp.text[:200].replace("\n", " ")
            raise AntiBotError(
                f"non-JSON response (challenge page?): {snippet!r}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise AntiBotError(f"unparseable response body: {exc}") from exc
        if "airlock" in resp.text[:2000].lower():
            raise AntiBotError("airlock marker in response body")

        # --- stale-hash detection ----------------------------------------
        if resp.status_code in (400, 404):
            raise StaleHashError(f"HTTP {resp.status_code} (stale hash?)")
        resp.raise_for_status()

        if data.get("errors"):
            msg = json.dumps(data["errors"])
            if "ValidationError" in msg or "PersistedQuery" in msg:
                raise StaleHashError(f"GraphQL errors: {msg}")
            raise RuntimeError(f"GraphQL errors: {msg}")
        return data

    def apply_cookies(self, cookies: list[dict]) -> None:
        """Load Playwright-style cookies into the requests session."""
        for c in cookies or []:
            name, value = c.get("name"), c.get("value")
            if not name:
                continue
            self.session.cookies.set(
                name, value, domain=c.get("domain", ".airbnb.com")
            )


# ---------------------------------------------------------------------------
# Parsing helpers -- walk the deeply nested response into a flat dict.
# ---------------------------------------------------------------------------


@dataclass
class Listing:
    listing_id: str
    title: Optional[str] = None
    property_type: Optional[str] = None
    room_type: Optional[str] = None
    city: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    person_capacity: Optional[int] = None
    bedrooms: Optional[str] = None
    beds: Optional[str] = None
    baths: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    category_ratings: dict[str, float] = field(default_factory=dict)
    host_name: Optional[str] = None
    host_id: Optional[str] = None
    is_superhost: Optional[bool] = None
    years_hosting: Optional[str] = None
    is_guest_favorite: Optional[bool] = None
    house_rules: list[str] = field(default_factory=list)
    safety_features: list[str] = field(default_factory=list)
    amenities: list[str] = field(default_factory=list)          # available only
    cancellation_policy: Optional[str] = None
    image_url: Optional[str] = None
    canonical_url: Optional[str] = None
    # Pricing (only populated when check-in/check-out dates are supplied;
    # otherwise the API returns "Add dates for prices").
    price_available: Optional[bool] = None
    price_display: Optional[str] = None          # "$1,018 USD"
    price_qualifier: Optional[str] = None        # "for 2 nights"
    price_label: Optional[str] = None            # "$1,018 USD for 2 nights"
    price_total: Optional[float] = None          # 1017.33 (from breakdown)
    price_per_night: Optional[float] = None      # 508.67
    price_original_total: Optional[float] = None  # pre-discount, if any
    price_nights: Optional[int] = None           # 2
    price_currency: Optional[str] = None         # "USD"
    price_style: Optional[str] = None            # "TOTAL_ONLY"
    price_breakdown: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


def _find_section(sections: list[dict], component_type: str) -> Optional[dict]:
    for container in sections:
        if container.get("sectionComponentType") == component_type:
            return container.get("section")
    return None


_MONEY_RE = re.compile(r"([0-9][0-9,]*\.?[0-9]*)")
_CURRENCY_RE = re.compile(r"\b([A-Z]{3})\b")


def _clean(text: Optional[str]) -> Optional[str]:
    """Normalize Airbnb's non-breaking spaces to plain spaces."""
    if text is None:
        return None
    return text.replace(chr(0xA0), ' ').replace(chr(0x202F), ' ').strip()


def _parse_money(text: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """Pull a numeric amount and currency code out of e.g. '$1,017.33 USD'."""
    if not text:
        return None, None
    amount: Optional[float] = None
    m = _MONEY_RE.search(text.replace(",", ""))
    if m:
        try:
            amount = float(m.group(1))
        except ValueError:
            amount = None
    cur = _CURRENCY_RE.search(text)
    return amount, (cur.group(1) if cur else None)


def _extract_amenities(sections: list[dict], out: "Listing") -> None:
    """Fill out.amenities (offered amenity titles) from AMENITIES_DEFAULT.

    Uses the full `seeAllAmenitiesGroups`, falling back to the preview groups,
    and keeps only amenities the place actually offers (available != false).
    """
    sec = _find_section(sections, "AMENITIES_DEFAULT")
    if not sec:
        return
    groups = sec.get("seeAllAmenitiesGroups") or sec.get(
        "previewAmenitiesGroups"
    ) or []
    for group in groups:
        for a in group.get("amenities", []) or []:
            title = _clean(a.get("title"))
            if title and a.get("available", True):
                out.amenities.append(title)


def _extract_price(sections: list[dict], out: "Listing") -> None:
    """Fill out.price_* from the first BookItSection that carries a price.

    The same structuredDisplayPrice appears in every BOOK_IT_* section; the
    SIDEBAR one additionally has a full breakdown in productItemDetail.
    """
    book_it_types = (
        "BOOK_IT_SIDEBAR",
        "BOOK_IT_CALENDAR_SHEET",
        "BOOK_IT_NAV",
        "BOOK_IT_FLOATING_FOOTER",
    )
    sec = None
    for t in book_it_types:
        candidate = _find_section(sections, t)
        if candidate and candidate.get("structuredDisplayPrice"):
            sec = candidate
            break
    if not sec:
        return

    sdp = sec.get("structuredDisplayPrice", {}) or {}
    primary = sdp.get("primaryLine", {}) or {}
    price_str = _clean(primary.get("price"))
    out.price_style = sdp.get("displayPriceStyle")
    out.price_display = price_str
    out.price_qualifier = _clean(primary.get("qualifier"))
    out.price_label = _clean(primary.get("accessibilityLabel"))

    # "Add dates for prices" -> a plain BasicDisplayPriceLine with no number.
    is_qualified = primary.get("__typename") == "QualifiedDisplayPriceLine"
    total_amount, currency = _parse_money(price_str)
    out.price_available = bool(is_qualified or total_amount is not None)
    if not out.price_available:
        out.price_display = None  # it's just a placeholder string
        return
    if currency:
        out.price_currency = currency

    # Nights: prefer the qualifier ("for 2 nights"), else the breakdown.
    if out.price_qualifier:
        m = re.search(r"(\d+)\s+night", out.price_qualifier)
        if m:
            out.price_nights = int(m.group(1))

    # Detailed breakdown from explanationData (and productItemDetail, richer).
    explanation = sec.get("structuredDisplayPrice", {}).get("explanationData")
    detail = (sec.get("productItemDetail") or {}).get("explanationData")
    for source in (detail, explanation):
        if not source:
            continue
        for group in source.get("priceDetails", []) or []:
            for item in group.get("items", []) or []:
                desc = _clean(item.get("description"))
                pstr = _clean(item.get("priceString"))
                amount, _ = _parse_money(pstr)
                out.price_breakdown.append(
                    {"description": desc, "price": pstr, "amount": amount}
                )
                if desc and "Price after discount" in desc:
                    out.price_total = amount if amount is not None else out.price_total
                    orig, _ = _parse_money(item.get("originalPriceString"))
                    out.price_original_total = orig
                elif desc and re.search(r"\d+\s+night.*x", desc):
                    # "2 nights x $508.67 USD"
                    per, _ = _parse_money(desc.split("x", 1)[1])
                    out.price_per_night = per
                    if out.price_total is None:
                        out.price_total = amount
                    nm = re.search(r"(\d+)\s+night", desc)
                    if nm and not out.price_nights:
                        out.price_nights = int(nm.group(1))
        if out.price_breakdown:
            break  # detail took precedence; don't duplicate with explanation

    # Fall back to the headline number if the breakdown didn't yield a total.
    if out.price_total is None:
        out.price_total = total_amount
    if (
        out.price_per_night is None
        and out.price_total is not None
        and out.price_nights
    ):
        out.price_per_night = round(out.price_total / out.price_nights, 2)


def parse(raw: dict[str, Any], listing_id: str) -> Listing:
    """Extract the interesting fields from a raw StaysPdpSections response."""
    out = Listing(listing_id=listing_id)

    presentation = (
        raw.get("data", {}).get("presentation", {}) or {}
    )
    pdp = presentation.get("stayProductDetailPage", {}) or {}
    sections_root = pdp.get("sections", {}) or {}
    sections = sections_root.get("sections", []) or []
    metadata = sections_root.get("metadata", {}) or {}

    # --- metadata: sharing config + logging context ------------------------
    sharing = metadata.get("sharingConfig", {}) or {}
    out.property_type = sharing.get("propertyType")
    out.person_capacity = sharing.get("personCapacity")
    out.image_url = sharing.get("imageUrl")
    out.review_count = sharing.get("reviewCount")
    out.rating = sharing.get("starRating")

    logging_ctx = (
        metadata.get("loggingContext", {}).get("eventDataLogging", {}) or {}
    )
    out.room_type = logging_ctx.get("roomType")
    out.latitude = logging_ctx.get("listingLat")
    out.longitude = logging_ctx.get("listingLng")
    out.is_superhost = logging_ctx.get("isSuperhost")
    if logging_ctx.get("guestSatisfactionOverall") is not None:
        out.rating = logging_ctx.get("guestSatisfactionOverall")
    for key, label in [
        ("accuracyRating", "accuracy"),
        ("checkinRating", "checkin"),
        ("cleanlinessRating", "cleanliness"),
        ("communicationRating", "communication"),
        ("locationRating", "location"),
        ("valueRating", "value"),
    ]:
        if logging_ctx.get(key) is not None:
            out.category_ratings[label] = logging_ctx[key]

    seo = metadata.get("seoFeatures", {}) or {}
    out.canonical_url = seo.get("canonicalUrl")

    # --- SBUI overview / host / guest-favorite -----------------------------
    # sbuiData is a sibling of `sections`/`metadata` inside the StayPDPSections
    # object (sections_root), not on stayProductDetailPage.
    sbui = sections_root.get("sbuiData", {}) or {}
    sbui_sections = (
        sbui.get("sectionConfiguration", {})
        .get("root", {})
        .get("sections", [])
    )
    for sc in sbui_sections:
        data = sc.get("sectionData", {}) or {}
        typ = data.get("__typename")
        if typ == "PdpOverviewV2Section":
            if data.get("title"):
                out.title = out.title or data["title"]
            for item in data.get("overviewItems", []) or []:
                t = (item.get("title") or "").lower()
                if "bedroom" in t:
                    out.bedrooms = item["title"]
                elif "bed" in t:
                    out.beds = item["title"]
                elif "bath" in t:
                    out.baths = item["title"]
        elif typ == "PdpHostOverviewDefaultSection":
            host_title = data.get("title", "")  # "Hosted by Ela"
            if host_title.lower().startswith("hosted by "):
                out.host_name = host_title[len("Hosted by "):]
            for item in data.get("overviewItems", []) or []:
                if "hosting" in (item.get("title") or "").lower():
                    out.years_hosting = item["title"]
            host_evt = (
                data.get("hostAvatar", {})
                .get("loggingEventData", {})
                .get("eventData", {})
            )
            out.host_id = (host_evt.get("pdpContext", {}) or {}).get("hostId")
        elif typ == "PdpReviewsHighlightBannerSection":
            out.is_guest_favorite = not data.get("hasGoldenLaurel", False)
            rd = data.get("reviewData", {}) or {}
            if rd.get("ratingScore") is not None:
                out.rating = rd["ratingScore"]

    # --- POLICIES_DEFAULT: house rules + safety + cancellation -------------
    policies = _find_section(sections, "POLICIES_DEFAULT")
    if policies:
        for rule_section in policies.get("houseRulesSections", []) or []:
            for item in rule_section.get("items", []) or []:
                if item.get("title"):
                    out.house_rules.append(item["title"])
        for safety_section in policies.get("safetyAndPropertiesSections", []) or []:
            for item in safety_section.get("items", []) or []:
                if item.get("title"):
                    out.safety_features.append(item["title"])
        cancel = policies.get("cancellationPolicyForDisplay", {}) or {}
        subtitles = cancel.get("subtitles") or []
        out.cancellation_policy = cancel.get("title") or (
            subtitles[0] if subtitles else None
        )

    # --- amenities (AMENITIES_DEFAULT) -------------------------------------
    _extract_amenities(sections, out)

    # --- pricing (BOOK_IT_* sections) --------------------------------------
    _extract_price(sections, out)

    # --- fall back to booking-prefetch cancellation name -------------------
    if not out.cancellation_policy:
        prefetch = metadata.get("bookingPrefetchData", {}) or {}
        cps = prefetch.get("cancellationPolicies") or []
        if cps:
            out.cancellation_policy = cps[0].get(
                "localized_cancellation_policy_name"
            )

    # --- node/DemandStayListing: location, host, ratings -------------------
    node = raw.get("data", {}).get("node", {}) or {}
    pres = node.get("pdpPresentation", {}) or {}
    out.person_capacity = out.person_capacity or pres.get("personCapacity")
    # `location` sits directly on the DemandStayListing node.
    loc = node.get("location", {}) or {}
    out.city = out.city or loc.get("city")
    coord = loc.get("coordinate", {}) or {}
    out.latitude = out.latitude if out.latitude is not None else coord.get("latitude")
    out.longitude = (
        out.longitude if out.longitude is not None else coord.get("longitude")
    )
    quality = pres.get("quality", {}) or {}
    stats = quality.get("listingRatingStats", {}) or {}
    overall = stats.get("overallRatingStats", {}) or {}
    if overall.get("ratingAverage") is not None:
        out.rating = overall["ratingAverage"]
    if overall.get("ratingCount") is not None:
        try:
            out.review_count = int(overall["ratingCount"])
        except (TypeError, ValueError):
            pass

    return out


# ---------------------------------------------------------------------------
# Fetch with browser-assisted recovery.
# ---------------------------------------------------------------------------


def fetch_with_recovery(
    client: "AirbnbPdpClient",
    listing_id: str,
    check_in: Optional[str] = None,
    check_out: Optional[str] = None,
    auto_recover: bool = True,
) -> dict[str, Any]:
    """Fetch a listing, healing anti-bot / stale-hash failures via Playwright.

    On an `AntiBotError` or `StaleHashError`, a real Chromium session is driven
    to the listing to (a) sniff the current persisted-query hash and (b) collect
    the challenge-cleared session cookies. Both are applied to the client and
    the request is retried once.
    """

    def _do() -> dict[str, Any]:
        return client.fetch(listing_id, check_in=check_in, check_out=check_out)

    try:
        return _do()
    except (AntiBotError, StaleHashError) as exc:
        if not auto_recover:
            raise
        reason = (
            "anti-bot challenge"
            if isinstance(exc, AntiBotError)
            else "stale query hash"
        )
        print(
            f"Hit {reason}: {exc}\n"
            "Recovering via a real browser (Playwright)...",
            file=sys.stderr,
        )
        try:
            from get_hash import extract_hash, update_crawler
        except ImportError as imp:
            raise RuntimeError(
                "Browser recovery needs get_hash.py + playwright installed "
                "(pip install playwright && python -m playwright install "
                "chromium)."
            ) from imp

        found = extract_hash(listing_id)

        # Carry the browser's session cookies over -- this is what actually
        # clears the anti-bot wall for the direct request.
        cookies = found.get("cookies") or []
        client.apply_cookies(cookies)
        if cookies:
            print(f"Applied {len(cookies)} browser cookies.", file=sys.stderr)

        new_hash = found.get("hash")
        if new_hash and new_hash != client.persisted_query_hash:
            update_crawler(new_hash)  # persist for next run
            client.persisted_query_hash = new_hash
            print(f"Refreshed hash -> {new_hash}", file=sys.stderr)

        return _do()


def main(argv: Optional[list[str]] = None) -> int:
    # Windows consoles often default to a legacy codepage (cp950/cp1252) that
    # can't encode listing text; force UTF-8 so printing never crashes.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Airbnb PDP sections crawler")
    parser.add_argument("listing", help="Listing id or rooms URL")
    parser.add_argument("--raw", action="store_true", help="Print raw JSON")
    parser.add_argument("--out", help="Write output JSON to this file")
    parser.add_argument("--locale", default="en")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--check-in", dest="check_in", default=None)
    parser.add_argument("--check-out", dest="check_out", default=None)
    parser.add_argument(
        "--hash",
        dest="persisted_hash",
        default=PERSISTED_QUERY_HASH,
        help="Persisted query sha256 hash (override if it goes stale)",
    )
    parser.add_argument(
        "--no-auto-recover",
        "--no-auto-hash",
        dest="auto_hash",
        action="store_false",
        help="Don't auto-recover from anti-bot / stale-hash errors via "
        "Playwright",
    )
    args = parser.parse_args(argv)

    listing_id, url_check_in, url_check_out = parse_listing(args.listing)
    # Explicit flags win; otherwise fall back to dates embedded in the URL.
    check_in = args.check_in or url_check_in
    check_out = args.check_out or url_check_out
    client = AirbnbPdpClient(
        persisted_query_hash=args.persisted_hash,
        locale=args.locale,
        currency=args.currency,
    )
    try:
        raw = fetch_with_recovery(
            client,
            listing_id,
            check_in=check_in,
            check_out=check_out,
            auto_recover=args.auto_hash,
        )
    except (RuntimeError, ValueError, requests.HTTPError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    result: Any = raw if args.raw else parse(raw, listing_id).to_dict()
    text = json.dumps(result, indent=2, ensure_ascii=False)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
