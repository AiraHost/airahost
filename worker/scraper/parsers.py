import re
import base64
import logging
from typing import Any, Dict, List, Optional

from worker.scraper.price_normalizer import normalize_raw_price

logger = logging.getLogger("worker.scraper")


def _find_keys(obj: Any, target_key: str) -> List[Any]:
    """Recursively search for a target key in a nested JSON object."""
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == target_key:
                results.append(v)
            elif isinstance(v, (dict, list)):
                results.extend(_find_keys(v, target_key))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_find_keys(item, target_key))
    return results


def _walk_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_dicts(item)


def _walk_strings(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_strings(item)
    elif isinstance(obj, str):
        yield obj


def _get_nested(data: Dict[str, Any], path: List[str]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _decode_graphql_id(encoded: str) -> Optional[str]:
    """
    Decode global GraphQL id like:
    'RGVtYW5kU3RheUxpc3Rpbmc6MTYyMzMxODQwMDE2NTU1MDM0Mg=='
    -> 'DemandStayListing:1623318400165550342'
    """
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except Exception:
        return None
    if ":" not in decoded:
        return None
    numeric = decoded.split(":")[-1]
    return numeric if numeric.isdigit() else None


def _extract_listing_id_from_search_result_item(r: Dict[str, Any]) -> Optional[str]:
    """
    Extract listing id from either:
    - demandStayListing.id (base64 global id), or
    - SkinnyListingItem.listingId (already numeric string/int)
    """
    if not isinstance(r, dict):
        return None

    demand = r.get("demandStayListing")
    if isinstance(demand, dict):
        lid = _decode_graphql_id(demand.get("id"))
        if lid:
            return lid

    raw_listing_id = r.get("listingId")
    if isinstance(raw_listing_id, int):
        return str(raw_listing_id)
    if isinstance(raw_listing_id, str):
        s = raw_listing_id.strip()
        if s.isdigit():
            return s
    return None


def parse_search_response(data: Dict[str, Any]) -> List[str]:
    """Extract listing IDs from staysSearch.results.searchResults[*].demandStayListing.id."""
    listing_ids: List[str] = []
    results = _get_nested(data, ["data", "presentation", "staysSearch", "results", "searchResults"])
    if not isinstance(results, list):
        return listing_ids

    for r in results:
        lid = _extract_listing_id_from_search_result_item(r)
        if lid and lid not in listing_ids:
            listing_ids.append(lid)

    return listing_ids


def parse_search_total_listings(data: Dict[str, Any]) -> Optional[int]:
    """
    Extract Airbnb's reported total listings count from StaysSearch payload.

    Source: data.presentation.staysSearch.results.filters.filterPanel.searchButtonText
    Example values: "Show 309 places", "Show 1 place"
    """
    text = _get_nested(
        data,
        [
            "data",
            "presentation",
            "staysSearch",
            "results",
            "filters",
            "filterPanel",
            "searchButtonText",
        ],
    )
    if not isinstance(text, str) or not text.strip():
        return None
    m = re.search(r"(\d[\d,]*)", text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_price_string(price_str: str) -> Optional[float]:
    """Extract numeric price from text like '$1,200 CAD' or 'US$241 / night'."""
    value, _prefix = _parse_price_with_prefix(price_str)
    return value


def _parse_price_with_prefix(price_str: str) -> tuple[Optional[float], Optional[str]]:
    """
    Extract numeric price plus the raw prefix string immediately before it.

    Prefix is intentionally not validated against known currencies.
    """
    if not price_str:
        return None, None
    text = str(price_str).replace("\xa0", " ")

    # Context-first match without requiring a specific prefix string:
    # "241 / night", "241 per night", "241 total", "241 for 2 nights"
    contextual = re.search(
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:/\s*night|per\s+night|night|total|for\s+\d+\s+nights?)",
        text,
        re.I,
    )
    if contextual:
        numeric = contextual.group(1).replace(",", "").strip()
        if numeric:
            prefix_match = re.search(r"([^\d\s]+)\s*$", text[:contextual.start(1)])
            prefix = prefix_match.group(1).strip() if prefix_match else None
            return float(numeric), prefix

    # Generic fallback: first plausible numeric token.
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", text):
        numeric = m.group(0).replace(",", "").strip()
        if not numeric:
            continue
        try:
            value = float(numeric)
        except Exception:
            continue
        if value > 0:
            prefix_match = re.search(r"([^\d\s]+)\s*$", text[:m.start()])
            prefix = prefix_match.group(1).strip() if prefix_match else None
            return value, prefix
    return None, None


def _extract_price_nights(text: str) -> int:
    if not isinstance(text, str) or not text:
        return 1
    m = re.search(r"for\s+(\d+)\s+nights?", text.lower())
    if not m:
        return 1
    try:
        n = int(m.group(1))
        return n if n > 0 else 1
    except Exception:
        return 1


def _parse_dollar_amount_currency(text: str) -> tuple[Optional[float], Optional[str]]:
    """
    Parse Airbnb primaryLine price text across common currency shapes, e.g.:
    '$173 CAD', 'US$241 CAD', '€199', '199 EUR', 'C$363'.
    Returns (amount, currency_code).
    """
    if not isinstance(text, str) or not text.strip():
        return None, None
    s = text.replace("\xa0", " ").strip()
    symbol_to_ccy = {
        "€": "EUR",
        "£": "GBP",
        "¥": "JPY",
        "₹": "INR",
        "₩": "KRW",
        "$": "USD",
    }
    amount: Optional[float] = None
    currency: Optional[str] = None

    # Pattern A: [prefix/code]$<amount> [CCY]
    m = re.search(
        r"(?:(?P<prefix>[A-Za-z]{1,3})\s*)?(?P<sym>[$€£¥₹₩])\s*"
        r"(?P<amt>[0-9][0-9,]*(?:\.[0-9]+)?)"
        r"(?:\s+(?P<ccy>[A-Za-z]{3}))?",
        s,
    )
    if m:
        try:
            amount = float(str(m.group("amt")).replace(",", ""))
        except Exception:
            amount = None
        ccy = (m.group("ccy") or "").strip().upper()
        prefix = (m.group("prefix") or "").strip().upper()
        sym = m.group("sym")
        if ccy:
            currency = ccy
        elif sym == "$" and prefix in ("US", "CA", "C", "AU", "A", "NZ"):
            currency = {
                "US": "USD",
                "CA": "CAD",
                "C": "CAD",
                "AU": "AUD",
                "A": "AUD",
                "NZ": "NZD",
            }.get(prefix, "USD")
        else:
            currency = symbol_to_ccy.get(sym, "USD")
    else:
        # Pattern B: <amount> <CCY>
        m2 = re.search(
            r"(?P<amt>[0-9][0-9,]*(?:\.[0-9]+)?)\s+(?P<ccy>[A-Za-z]{3})\b",
            s,
        )
        if m2:
            try:
                amount = float(str(m2.group("amt")).replace(",", ""))
            except Exception:
                amount = None
            currency = str(m2.group("ccy") or "").strip().upper() or None

    if amount is None:
        return None, None
    try:
        amount = float(amount)
    except Exception:
        return None, None
    currency = str(currency or "USD").strip().upper()
    return (amount if amount > 0 else None), currency


def _mentions_original_price(text: Any) -> bool:
    return bool(re.search(r"\boriginal(?:ly)?\b", str(text or ""), re.I))


def _parse_pdp_booking_breakdown_nightly(
    structured_display_price: Dict[str, Any],
) -> Optional[tuple[float, Optional[str], int]]:
    """
    Return Airbnb's base nightly amount from the booking breakdown.

    PDP primaryLine can be a fee-inclusive trip total. explanationData keeps
    the base-rate row, usually as "1 night x $430" or "$430 x 2 nights".
    """
    if not isinstance(structured_display_price, dict):
        return None

    explanation_data = structured_display_price.get("explanationData")
    if not isinstance(explanation_data, dict):
        return None
    price_details = explanation_data.get("priceDetails")
    if not isinstance(price_details, list):
        return None

    first_item: Optional[Dict[str, Any]] = None
    current_price_item: Optional[Dict[str, Any]] = None
    current_price_markers = (
        "price after discount",
        "discounted price",
        "discounted total",
    )
    fee_markers = (
        "cleaning",
        "service fee",
        "tax",
        "taxes",
        "discount",
        "subtotal",
        "total",
        "price after discount",
    )

    primary = structured_display_price.get("primaryLine")
    primary_context = ""
    if isinstance(primary, dict):
        primary_context = " ".join(
            str(primary.get(key) or "")
            for key in ("discountedPrice", "price", "accessibilityLabel", "qualifier")
        )

    def _infer_price_nights_from_context(*texts: str) -> Optional[int]:
        for text in texts:
            s = str(text or "")
            if not s.strip():
                continue
            if re.search(r"\b(?:min(?:imum)?|at least)\b", s, re.I):
                continue
            m = re.search(r"\bfor\s+(\d+)\s+nights?\b", s, re.I)
            if m:
                return max(1, int(m.group(1)))
            m = re.search(r"\b(\d+)\s+nights?\b", s, re.I)
            if m:
                return max(1, int(m.group(1)))
        return None

    def _matching_primary_display_total(
        raw_total: float,
        price_nights: int,
        fallback_currency: Optional[str],
    ) -> Optional[tuple[float, Optional[str]]]:
        if not isinstance(primary, dict):
            return None
        accessibility_label = str(primary.get("accessibilityLabel") or "")
        candidate_keys = (
            ("discountedPrice", "accessibilityLabel", "price")
            if _mentions_original_price(accessibility_label)
            else ("discountedPrice", "price", "accessibilityLabel")
        )
        qualifier = str(primary.get("qualifier") or "")
        for key in candidate_keys:
            candidate = primary.get(key)
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            primary_amount, primary_currency = _parse_dollar_amount_currency(candidate)
            if primary_amount is None:
                continue
            primary_nights = _infer_price_nights_from_context(candidate, qualifier, primary_context)
            if primary_nights is not None and int(primary_nights) != int(price_nights):
                continue
            if abs(float(primary_amount) - float(raw_total)) <= 1.0:
                return float(primary_amount), primary_currency or fallback_currency
        return None

    for detail in price_details:
        if not isinstance(detail, dict):
            continue
        items = detail.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if first_item is None:
                first_item = item

            description = str(item.get("description") or "").replace("\xa0", " ").strip()
            if not description:
                continue
            desc_lower = description.lower()
            if current_price_item is None and any(marker in desc_lower for marker in current_price_markers):
                current_price_item = item
            if "x" not in desc_lower or "night" not in desc_lower:
                continue

            parts = re.split(r"\s+x\s+", description, maxsplit=1, flags=re.I)
            if len(parts) != 2:
                continue
            left, right = parts[0].strip(), parts[1].strip()
            if "night" in left.lower():
                price_text = right
            elif "night" in right.lower():
                price_text = left
            else:
                continue

            amount, currency = _parse_dollar_amount_currency(price_text)
            if amount is None:
                continue
            night_match = re.search(r"\b(\d+)\s+nights?\b", description, re.I)
            nights = 1
            if night_match:
                try:
                    nights = max(1, int(night_match.group(1)))
                except Exception:
                    nights = 1
            primary_total = _matching_primary_display_total(
                float(amount) * max(1, int(nights)),
                nights,
                currency,
            )
            if primary_total is not None:
                display_total, display_currency = primary_total
                return round(display_total / max(1, int(nights)), 2), display_currency, nights
            return amount, currency, nights

    if isinstance(current_price_item, dict):
        description = str(current_price_item.get("description") or "").replace("\xa0", " ").strip()
        price_string = str(current_price_item.get("priceString") or "").replace("\xa0", " ").strip()
        price_nights = _infer_price_nights_from_context(description, price_string, primary_context)
        if price_nights is not None:
            amount, currency = _parse_dollar_amount_currency(price_string)
            if amount is None:
                amount, currency = _parse_dollar_amount_currency(description)
            if amount is not None:
                primary_total = _matching_primary_display_total(
                    float(amount),
                    price_nights,
                    currency,
                )
                if primary_total is not None:
                    amount, currency = primary_total
                normalized = normalize_raw_price(
                    amount,
                    qualifier="total",
                    context_text=f"for {price_nights} nights",
                    stay_nights=price_nights,
                    source="pdp_breakdown",
                    produce_nightly_from_total=True,
                )
                if normalized is not None and normalized.nightly_price is not None:
                    return float(normalized.nightly_price), currency, price_nights

    if not isinstance(first_item, dict):
        return None

    description = str(first_item.get("description") or "").replace("\xa0", " ").strip()
    price_string = str(first_item.get("priceString") or "").replace("\xa0", " ").strip()
    desc_lower = description.lower()
    if any(marker in desc_lower for marker in fee_markers):
        return None
    if description and re.search(r"[A-Za-z]", description) and "night" not in desc_lower and "base" not in desc_lower:
        return None

    amount, currency = _parse_dollar_amount_currency(price_string)
    if amount is None:
        amount, currency = _parse_dollar_amount_currency(description)
    if amount is None:
        return None
    return amount, currency, 1


_GUEST_RE = re.compile(r"(\d+)\s*(?:guests?|guest|people|person)\b", re.I)
_BEDROOM_RE = re.compile(r"(\d+)\s*(?:bedrooms?|bedroom|br)\b", re.I)
_BED_RE = re.compile(r"(\d+)\s*beds?\b", re.I)
_BATH_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:(?:private|shared|full|half)\s+)?baths?\b",
    re.I,
)
_MIN_NIGHTS_RE = re.compile(r"(?:minimum|min\.?|at least)\s*(?:stay\s*of\s*)?(\d+)\s*nights?", re.I)


def _normalize_property_type_from_text(text: str) -> str:
    t = str(text or "").lower()
    # Any explicit "room in ..." style is typically not an entire place.
    if re.search(r"\broom\s+in\b", t):
        return "private_room"
    if "private room" in t:
        return "private_room"
    if "shared room" in t:
        return "shared_room"
    # Search cards frequently use labels like "Home in X", "Guest suite in X",
    # "Apartment in X" without the word "entire".
    if re.search(
        r"\b(home|house|apartment|apt|condo|loft|townhouse|villa|cottage|bungalow|cabin|guesthouse|guest suite|suite|tiny home|farm stay|hotel)\s+in\b",
        t,
    ):
        return "entire_home"
    if re.search(r"\bentire\b", t):
        return "entire_home"
    return ""


def _extract_structural_context_from_search_result(r: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "location": None,
        "lat": None,
        "lng": None,
        "accommodates": None,
        "bedrooms": None,
        "beds": None,
        "baths": None,
        "property_type": None,
        "rating": None,
        "reviews": None,
        "amenities": [],
    }

    # First pass: read explicit typed fields if present in nested objects.
    for d in _walk_dicts(r):
        if out["accommodates"] is None:
            for key in ("personCapacity", "maxGuestCapacity", "maxGuests", "guestCapacity", "accommodates"):
                val = d.get(key)
                if isinstance(val, (int, float)) and int(val) > 0:
                    out["accommodates"] = int(val)
                    break

        if out["bedrooms"] is None:
            for key in ("bedroomCount", "bedrooms", "numBedrooms"):
                val = d.get(key)
                if isinstance(val, (int, float)) and int(val) >= 0:
                    out["bedrooms"] = int(val)
                    break

        if out["beds"] is None:
            for key in ("bedCount", "beds", "numBeds"):
                val = d.get(key)
                if isinstance(val, (int, float)) and int(val) >= 0:
                    out["beds"] = int(val)
                    break

        if out["baths"] is None:
            for key in ("bathroomCount", "bathrooms", "numBathrooms"):
                val = d.get(key)
                if isinstance(val, (int, float)) and float(val) >= 0:
                    out["baths"] = float(val)
                    break

        if not out["property_type"]:
            for key in ("roomTypeCategory", "spaceType", "propertyType", "propertyTypeLabel", "typeName"):
                val = d.get(key)
                if isinstance(val, str) and val.strip():
                    norm = _normalize_property_type_from_text(val)
                    if norm:
                        out["property_type"] = norm
                        break

        if out["rating"] is None:
            for key in ("avgRating", "rating", "starRating", "averageRating"):
                val = d.get(key)
                if isinstance(val, (int, float)) and 0 < float(val) <= 5:
                    out["rating"] = round(float(val), 2)
                    break

        if out["reviews"] is None:
            for key in ("reviewCount", "reviewsCount", "numberOfReviews", "reviews"):
                val = d.get(key)
                if isinstance(val, (int, float)) and int(val) >= 0:
                    out["reviews"] = int(val)
                    break

        if not out["location"]:
            city = d.get("city") if isinstance(d.get("city"), str) else None
            state = d.get("state") if isinstance(d.get("state"), str) else None
            country = d.get("country") if isinstance(d.get("country"), str) else None
            parts = [p.strip() for p in (city, state, country) if isinstance(p, str) and p.strip()]
            if parts:
                out["location"] = ", ".join(parts)

        if out["lat"] is None or out["lng"] is None:
            lat = d.get("lat", d.get("latitude", d.get("locationLat")))
            lng = d.get("lng", d.get("longitude", d.get("locationLng")))
            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                out["lat"] = float(lat)
                out["lng"] = float(lng)

    # Second pass: parse text fragments (titles, subtitles, labels) for missing fields.
    for s in _walk_strings(r):
        if not isinstance(s, str):
            continue
        text = s.strip()
        if not text:
            continue

        if not out["property_type"]:
            norm = _normalize_property_type_from_text(text)
            if norm:
                out["property_type"] = norm

        if out["accommodates"] is None:
            m = _GUEST_RE.search(text)
            if m:
                try:
                    out["accommodates"] = int(m.group(1))
                except Exception:
                    pass

        if out["bedrooms"] is None:
            m = _BEDROOM_RE.search(text)
            if m:
                try:
                    out["bedrooms"] = int(m.group(1))
                except Exception:
                    pass

        if out["beds"] is None:
            m = _BED_RE.search(text)
            if m:
                try:
                    out["beds"] = int(m.group(1))
                except Exception:
                    pass

        if out["baths"] is None:
            m = _BATH_RE.search(text)
            if m:
                try:
                    out["baths"] = float(m.group(1))
                except Exception:
                    pass

        if out["rating"] is None:
            m = re.search(r"([0-5](?:\.\d+)?)\s*(?:out of 5|stars?)", text, re.I)
            if m:
                try:
                    rv = float(m.group(1))
                    if 0 < rv <= 5:
                        out["rating"] = round(rv, 2)
                except Exception:
                    pass

        if out["reviews"] is None:
            m = re.search(r"\b(\d{1,6})\s*(?:reviews?|ratings?)\b", text, re.I)
            if m:
                try:
                    out["reviews"] = int(m.group(1))
                except Exception:
                    pass

        if not out["location"]:
            # Common card text shapes:
            # - "Home in Oceano"
            # - "Guest suite in Belmont, CA"
            # - "Cabin at Lake Tahoe, California"
            m = re.search(
                r"\b(?:in|at)\s+([A-Za-z][A-Za-z .'\-]*(?:,\s*[A-Za-z][A-Za-z .'\-]*){0,2})\s*$",
                text,
                re.I,
            )
            if m:
                cand = m.group(1).strip(" .,-")
                # Guard against obvious non-location tails.
                if cand and not re.search(r"\b(night|guests?|beds?|baths?|reviews?)\b", cand, re.I):
                    out["location"] = cand

    return out


def _extract_availability_context_from_search_result(r: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "is_available": True,
        "availability_reason": None,
        "min_nights": None,
    }

    strong_unavailable_patterns = (
        r"\bsold out\b",
        r"\bno longer available\b",
        r"\bnot available\b",
    )

    # Typed fields first.
    for d in _walk_dicts(r):
        for key in ("isAvailable", "available"):
            v = d.get(key)
            if isinstance(v, bool):
                out["is_available"] = bool(v)
                if not out["is_available"] and not out["availability_reason"]:
                    out["availability_reason"] = "unavailable"
        for key in ("isSoldOut", "soldOut", "isBooked"):
            v = d.get(key)
            if isinstance(v, bool) and v:
                out["is_available"] = False
                out["availability_reason"] = "sold_out"

    # Text fallback for availability/min-stay.
    for s in _walk_strings(r):
        if not isinstance(s, str):
            continue
        txt = s.strip()
        if not txt:
            continue
        txt_l = txt.lower()

        if out["min_nights"] is None:
            m = _MIN_NIGHTS_RE.search(txt)
            if m:
                try:
                    n = int(m.group(1))
                    if n > 0:
                        out["min_nights"] = n
                except Exception:
                    pass

        is_strong_unavailable = any(
            re.search(pattern, txt_l) for pattern in strong_unavailable_patterns
        )
        if is_strong_unavailable:
            out["is_available"] = False
            if "sold out" in txt_l:
                out["availability_reason"] = "sold_out"
            elif not out["availability_reason"]:
                out["availability_reason"] = "unavailable"

        if "minimum stay" in txt_l or ("at least" in txt_l and "night" in txt_l):
            if not out["availability_reason"]:
                out["availability_reason"] = "minimum_nights_not_met"

    return out


def _normalize_property_type_from_room_type_value(value: str) -> Optional[str]:
    t = str(value or "").strip().lower()
    if not t:
        return None
    if "entire" in t or t in {"home", "house", "apartment", "apt"}:
        return "entire_home"
    if "private room" in t:
        return "private_room"
    if "shared room" in t:
        return "shared_room"
    return None


def _extract_search_global_context(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract search-level fallback context from StaysSearch payload.

    Priority:
      1) results.filterState (authoritative selected values)
      2) results.searchInput.staysSearchInput.guests.adults.searchParams.params[*]
      3) results.filters.filterPanel...ROOMS_AND_BEDS...searchParams.params[*]
    """
    out: Dict[str, Any] = {
        "location": None,
        "accommodates": None,
        "bedrooms": None,
        "beds": None,
        "baths": None,
        "property_type": None,
    }

    results_root = _get_nested(data, ["data", "presentation", "staysSearch", "results"])
    if not isinstance(results_root, dict):
        return out

    # Location fallback from logging metadata (commonly present for map area searches).
    if not out["location"]:
        rmd = _get_nested(results_root, ["loggingMetadata", "remarketingLoggingData"])
        if isinstance(rmd, dict):
            canonical = rmd.get("canonicalLocation")
            if isinstance(canonical, str) and canonical.strip():
                out["location"] = canonical.strip()
            else:
                city = rmd.get("city")
                state = rmd.get("state")
                country = rmd.get("country")
                parts = [
                    p.strip()
                    for p in (city, state, country)
                    if isinstance(p, str) and p.strip()
                ]
                if parts:
                    out["location"] = ", ".join(parts)

    filter_state = results_root.get("filterState")
    if isinstance(filter_state, list):
        for item in filter_state:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            value = item.get("value")
            if not key:
                continue

            if key == "query" and not out["location"]:
                if isinstance(value, dict):
                    s = value.get("stringValue")
                    if isinstance(s, str) and s.strip():
                        out["location"] = s.strip()

            if key == "adults" and out["accommodates"] is None:
                if isinstance(value, dict):
                    iv = value.get("integerValue")
                    if isinstance(iv, (int, float)) and int(iv) > 0:
                        out["accommodates"] = int(iv)

            if key == "min_bedrooms" and out["bedrooms"] is None:
                if isinstance(value, dict):
                    iv = value.get("integerValue")
                    if isinstance(iv, (int, float)) and int(iv) >= 0:
                        out["bedrooms"] = int(iv)

            if key == "min_beds" and out["beds"] is None:
                if isinstance(value, dict):
                    iv = value.get("integerValue")
                    if isinstance(iv, (int, float)) and int(iv) >= 0:
                        out["beds"] = int(iv)

            if key == "min_bathrooms" and out["baths"] is None:
                if isinstance(value, dict):
                    iv = value.get("integerValue")
                    if isinstance(iv, (int, float)) and float(iv) >= 0:
                        out["baths"] = float(iv)

            if key == "room_types" and not out["property_type"]:
                if isinstance(value, dict):
                    vals = value.get("stringValues")
                    if isinstance(vals, list):
                        for raw in vals:
                            ptype = _normalize_property_type_from_room_type_value(str(raw or ""))
                            if ptype:
                                out["property_type"] = ptype
                                break

            if key == "refinement_paths" and not out["property_type"]:
                if isinstance(value, dict):
                    vals = value.get("stringValues")
                    if isinstance(vals, list):
                        normalized_paths = {str(v or "").strip().lower() for v in vals}
                        if "/homes" in normalized_paths:
                            out["property_type"] = "entire_home"
                        elif "/rooms" in normalized_paths:
                            out["property_type"] = "private_room"

    # guests.adults.searchParams.params[*].key == "adults"
    if out["accommodates"] is None:
        adults_params = _get_nested(
            data,
            [
                "data",
                "presentation",
                "staysSearch",
                "results",
                "searchInput",
                "staysSearchInput",
                "guests",
                "adults",
                "searchParams",
                "params",
            ],
        )
        if isinstance(adults_params, list):
            for p in adults_params:
                if not isinstance(p, dict):
                    continue
                if str(p.get("key") or "").strip() != "adults":
                    continue
                v = p.get("value")
                if isinstance(v, dict):
                    sv = v.get("stringValue")
                    if isinstance(sv, str) and sv.strip().isdigit():
                        out["accommodates"] = int(sv.strip())
                        break

    # Rooms and beds panel stepper params (Bedrooms/Beds/Bathrooms).
    panel_sections = _get_nested(
        data,
        [
            "data",
            "presentation",
            "staysSearch",
            "results",
            "filters",
            "filterPanel",
            "filterPanelSections",
            "sections",
        ],
    )
    if isinstance(panel_sections, list):
        for sec in panel_sections:
            if not isinstance(sec, dict):
                continue
            sec_id = str(sec.get("sectionId") or "").upper()
            if sec_id != "FILTER_SECTION_CONTAINER:ROOMS_AND_BEDS_WITH_SUBCATEGORY":
                continue
            sec_data = sec.get("sectionData")
            if not isinstance(sec_data, dict):
                continue
            items = sec_data.get("discreteFilterItems")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                params = _get_nested(item, ["searchParams", "params"])
                if not isinstance(params, list):
                    continue
                for p in params:
                    if not isinstance(p, dict):
                        continue
                    key = str(p.get("key") or "").strip()
                    v = p.get("value")
                    sv: Optional[str] = None
                    if isinstance(v, dict):
                        raw = v.get("stringValue")
                        if isinstance(raw, str):
                            sv = raw.strip()
                    if not sv:
                        continue
                    if key == "min_bathrooms" and out["baths"] is None and sv.replace(".", "", 1).isdigit():
                        out["baths"] = float(sv)
                    elif key == "min_bedrooms" and out["bedrooms"] is None and sv.isdigit():
                        out["bedrooms"] = int(sv)
                    elif key == "min_beds" and out["beds"] is None and sv.isdigit():
                        out["beds"] = int(sv)

    return out


def parse_search_listing_context(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Build fallback metadata from StaysSearch response:
    listing_id -> {title, nightly_price, total_price}
    """
    context: Dict[str, Dict[str, Optional[float]]] = {}
    nights_hint = 1

    # Exact extraction from staysSearch.results.searchResults[*]
    search_results = _get_nested(data, ["data", "presentation", "staysSearch", "results", "searchResults"])
    if not isinstance(search_results, list):
        return context

    global_ctx = _extract_search_global_context(data)

    for r in search_results:
        if not isinstance(r, dict):
            continue

        listing_id = _extract_listing_id_from_search_result_item(r)
        if not listing_id:
            continue

        row = context.setdefault(
            listing_id,
            {
                "title": None,
                "nightly_price": None,
                "total_price": None,
                "currency": None,
                "price_nights": 1,
                "location": None,
                "lat": None,
                "lng": None,
                "accommodates": None,
                "bedrooms": None,
                "beds": None,
                "baths": None,
                "property_type": None,
                "rating": None,
                "reviews": None,
                "amenities": [],
                "is_available": True,
                "availability_reason": None,
                "min_nights": None,
            },
        )

        # Title source from search payload:
        # prefer subtitle/nameLocalized over generic title ("Guest suite in ...")
        subtitle = r.get("subtitle")
        name_localized = r.get("nameLocalized", {})
        localized = name_localized.get("localizedStringWithTranslationPreference") if isinstance(name_localized, dict) else None
        if isinstance(localized, str) and localized.strip():
            row["title"] = localized.strip()
        elif isinstance(subtitle, str) and subtitle.strip():
            row["title"] = subtitle.strip()
        elif isinstance(r.get("title"), str) and r.get("title", "").strip():
            row["title"] = r["title"].strip()

        # Availability/min-stay context for filtering and diagnostics.
        avail = _extract_availability_context_from_search_result(r)
        if isinstance(avail.get("is_available"), bool):
            row["is_available"] = bool(avail["is_available"])
        if avail.get("availability_reason"):
            row["availability_reason"] = avail["availability_reason"]
        if isinstance(avail.get("min_nights"), int) and avail["min_nights"] > 0:
            row["min_nights"] = int(avail["min_nights"])

        # Price source from search payload (strict path only):
        # available=true + structuredDisplayPrice.primaryLine.{discountedPrice|price}
        # Always prefer discountedPrice when both are present.
        sdp = r.get("structuredDisplayPrice", {})
        if isinstance(sdp, dict):
            primary = sdp.get("primaryLine", {})
            if isinstance(primary, dict):
                qualifier = str(primary.get("qualifier") or "").lower()
                accessibility_label = str(primary.get("accessibilityLabel") or "")
                price_text = primary.get("discountedPrice")
                if not isinstance(price_text, str) or not price_text.strip():
                    if _mentions_original_price(accessibility_label):
                        price_text = accessibility_label
                    else:
                        price_text = primary.get("price")

                # Exact payload rule:
                # if available=true and primaryLine.price is '$<num> <ccy>',
                # parse it as the primary source.
                if row.get("is_available") and isinstance(price_text, str):
                    strict_val, strict_ccy = _parse_dollar_amount_currency(price_text)
                    if strict_val is not None:
                        if strict_ccy and not row.get("currency"):
                            row["currency"] = strict_ccy
                        # "for N nights" → total price; derive nightly.
                        # Some payloads omit qualifier but include "for N nights" in
                        # accessibilityLabel, so inspect both fields.
                        qualifier_ctx = f"{qualifier} {accessibility_label}".strip()
                        normalized = normalize_raw_price(
                            strict_val,
                            qualifier=qualifier,
                            context_text=qualifier_ctx,
                            stay_nights=nights_hint,
                            source="search",
                            currency=strict_ccy,
                            produce_nightly_from_total=False,
                            round_direct_nightly_to_whole=True,
                        )
                        if normalized is not None:
                            row["nightly_price"] = normalized.nightly_price
                            row["total_price"] = normalized.total_price
                            row["price_nights"] = normalized.price_nights

        # Structural/context fields used for similarity scoring.
        derived = _extract_structural_context_from_search_result(r)
        for key in ("location", "lat", "lng", "accommodates", "bedrooms", "beds", "baths", "property_type", "rating", "reviews"):
            if row.get(key) in (None, "", 0):
                v = derived.get(key)
                if v not in (None, "", 0):
                    row[key] = v
        if not row.get("amenities") and isinstance(derived.get("amenities"), list):
            row["amenities"] = derived["amenities"]

        # Final fallback from search-level context (same request/filter state).
        # Applied only for missing values in skinny listing cards. Search-level
        # location is useful when Airbnb returns SkinnyListingItem rows without
        # any per-card location text; it never overwrites a card-derived value.
        for key in ("location", "accommodates", "bedrooms", "beds", "baths", "property_type"):
            if row.get(key) in (None, "", 0):
                v = global_ctx.get(key)
                if v not in (None, "", 0):
                    row[key] = v

        logger.info(
            "[amenities][search] listing_id=%s amenities=%s",
            listing_id,
            list(row.get("amenities") or []),
        )
        if not str(row.get("location") or "").strip():
            _title = str(row.get("title") or "").strip()
            _raw_title = str(r.get("title") or "").strip()
            _raw_subtitle = str(r.get("subtitle") or "").strip()
            _samples: List[str] = []
            for _txt in _walk_strings(r):
                _s = str(_txt or "").strip()
                if not _s:
                    continue
                if len(_s) > 100:
                    _s = _s[:100] + "...<truncated>"
                if _s in _samples:
                    continue
                _samples.append(_s)
                if len(_samples) >= 20:
                    break
            logger.info(
                "[search_location_missing_raw] listing_id=%s title=%s raw_title=%s raw_subtitle=%s text_samples=%s",
                listing_id,
                _title,
                _raw_title,
                _raw_subtitle,
                _samples,
            )

    return context


def parse_pdp_response(data: Dict[str, Any], listing_id: str, base_url: str) -> Dict[str, Any]:
    """Extract listing title, price fields, and amenities from StaysPdpSections response."""
    result = {
        "listing_id": str(listing_id),
        "title": None,
        "location": None,
        "city": None,
        "state": None,
        "postal_code": None,
        "country": None,
        "country_code": None,
        "accommodates": None,
        "bedrooms": None,
        "beds": None,
        "baths": None,
        "property_type": None,
        "nightly_price": None,
        "total_price": None,
        "currency": None,
        "price_nights": 1,
        "price_kind": "unknown",
        "cleaning_fee": None,
        "service_fee": None,
        "amenities": [],
        "listing_url": f"{base_url}/rooms/{listing_id}",
    }

    generic_titles = {
        "things to know",
        "change dates",
        "choose a cancellation policy",
        "availability",
        "reviews",
        "amenities",
        "where you'll sleep",
    }

    def _extract_overview_v2_context(payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract structural/location context from
        sections.sbuiData.sectionConfiguration.root.sections[*].sectionData
        for OVERVIEW_DEFAULT_V2.
        """
        out: Dict[str, Any] = {
            "location": None,
            "city": None,
            "state": None,
            "country": None,
            "accommodates": None,
            "bedrooms": None,
            "beds": None,
            "baths": None,
            "property_type": None,
        }

        root_sections = _get_nested(
            payload,
            [
                "data",
                "presentation",
                "stayProductDetailPage",
                "sections",
                "sbuiData",
                "sectionConfiguration",
                "root",
                "sections",
            ],
        )
        if not isinstance(root_sections, list):
            root_sections = _get_nested(
                payload,
                [
                    "data",
                    "presentation",
                    "stayproductdetailpage",
                    "sections",
                    "sbuiData",
                    "sectionConfiguration",
                    "root",
                    "sections",
                ],
            )
        if not isinstance(root_sections, list):
            return out

        for entry in root_sections:
            if not isinstance(entry, dict):
                continue
            section_id = str(entry.get("sectionId") or "").strip().upper()
            if section_id != "OVERVIEW_DEFAULT_V2":
                continue
            section_data = entry.get("sectionData")
            if not isinstance(section_data, dict):
                continue

            title = section_data.get("title")
            if isinstance(title, str) and title.strip():
                t = title.strip()
                m = re.search(r"^\s*(.+?)\s+in\s+(.+?)\s*$", t, re.I)
                if m:
                    ptype = m.group(1).strip()
                    loc = m.group(2).strip()
                    if ptype:
                        out["property_type"] = ptype
                    if loc:
                        out["location"] = loc
                        parts = [p.strip() for p in loc.split(",") if p and p.strip()]
                        if len(parts) >= 1:
                            out["city"] = parts[0]
                        if len(parts) >= 2:
                            out["state"] = parts[1]
                        if len(parts) >= 3:
                            out["country"] = parts[-1]

            overview_items = section_data.get("overviewItems")
            if isinstance(overview_items, list):
                for item in overview_items:
                    text = ""
                    if isinstance(item, dict):
                        raw = item.get("title")
                        if isinstance(raw, str):
                            text = raw.strip()
                    elif isinstance(item, str):
                        text = item.strip()
                    if not text:
                        continue

                    if out["accommodates"] is None:
                        m = _GUEST_RE.search(text)
                        if m:
                            try:
                                out["accommodates"] = int(m.group(1))
                            except Exception:
                                pass

                    if out["bedrooms"] is None:
                        m = _BEDROOM_RE.search(text)
                        if m:
                            try:
                                out["bedrooms"] = int(m.group(1))
                            except Exception:
                                pass

                    if out["beds"] is None:
                        m = _BED_RE.search(text)
                        if m:
                            try:
                                out["beds"] = int(m.group(1))
                            except Exception:
                                pass

                    if out["baths"] is None:
                        m = _BATH_RE.search(text)
                        if m:
                            try:
                                out["baths"] = float(m.group(1))
                            except Exception:
                                pass
            break

        return out

    def _extract_metadata_sharing_config(payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract metadata.sharingConfig fields (location/propertyType/personCapacity).
        Airbnb payloads can place metadata under stayProductDetailPage.sections.metadata
        (common) with occasional shape variations.
        """
        out: Dict[str, Any] = {
            "location": None,
            "city": None,
            "state": None,
            "country": None,
            "property_type": None,
            "accommodates": None,
        }

        metadata_obj: Optional[Dict[str, Any]] = None

        # Primary shape (from your payload):
        # data.presentation.stayProductDetailPage.sections.metadata
        sections_container = _get_nested(
            payload,
            ["data", "presentation", "stayProductDetailPage", "sections"],
        )
        if not isinstance(sections_container, dict):
            sections_container = _get_nested(
                payload,
                ["data", "presentation", "stayproductdetailpage", "sections"],
            )

        if isinstance(sections_container, dict):
            md = sections_container.get("metadata")
            if isinstance(md, dict):
                metadata_obj = md

        # Alternate paths seen in some payload variants.
        if not isinstance(metadata_obj, dict):
            metadata_paths = (
                ["data", "presentation", "stayProductDetailPage", "metadata"],
                ["data", "presentation", "stayproductdetailpage", "metadata"],
            )
            for path in metadata_paths:
                node = _get_nested(payload, path)
                if isinstance(node, dict):
                    metadata_obj = node
                    break

        # Last resort: find a dict explicitly marked as StayPDPMetadata under sections.
        if not isinstance(metadata_obj, dict) and isinstance(sections_container, dict):
            for d in _walk_dicts(sections_container):
                if not isinstance(d, dict):
                    continue
                if d.get("__typename") == "StayPDPMetadata":
                    metadata_obj = d
                    break

        if not isinstance(metadata_obj, dict):
            return out

        sharing = metadata_obj.get("sharingConfig")
        if not isinstance(sharing, dict):
            return out

        loc = sharing.get("location")
        if isinstance(loc, str) and loc.strip():
            out["location"] = loc.strip()
            parts = [p.strip() for p in out["location"].split(",") if p and p.strip()]
            if len(parts) >= 1:
                out["city"] = parts[0]
            if len(parts) >= 2:
                out["state"] = parts[1]
            if len(parts) >= 3:
                out["country"] = parts[-1]

        ptype = sharing.get("propertyType")
        if isinstance(ptype, str) and ptype.strip():
            out["property_type"] = ptype.strip()

        cap = sharing.get("personCapacity")
        if isinstance(cap, (int, float)) and int(cap) > 0:
            out["accommodates"] = int(cap)

        return out

    # 1) Title extraction: data.presentation.stayProductDetailPage.sections.sections[*].section.listingTitle
    sections_root = _get_nested(data, ["data", "presentation", "stayProductDetailPage", "sections", "sections"])
    if not isinstance(sections_root, list):
        sections_root = _get_nested(data, ["data", "presentation", "stayproductdetailpage", "sections", "sections"])

    # 0) Primary structural/location extraction from metadata.sharingConfig.
    metadata_ctx = _extract_metadata_sharing_config(data)
    for key in ("location", "city", "state", "country", "accommodates", "property_type"):
        value = metadata_ctx.get(key)
        if value not in (None, "", 0):
            result[key] = value

    # 0b) Structural/location extraction from SBUI overview section.
    # This is where Airbnb commonly exposes "3 guests · 1 bedroom · 2 beds · 1 bath".
    overview_ctx = _extract_overview_v2_context(data)
    for key in ("location", "city", "state", "country", "accommodates", "bedrooms", "beds", "baths", "property_type"):
        if result.get(key) in (None, "", 0):
            value = overview_ctx.get(key)
            if value not in (None, "", 0):
                result[key] = value


    if isinstance(sections_root, list):
        for entry in sections_root:
            if not isinstance(entry, dict):
                continue
            sec = entry.get("section")
            if not isinstance(sec, dict):
                continue
            title_from_path = sec.get("listingTitle")
            if isinstance(title_from_path, str) and title_from_path.strip():
                result["title"] = title_from_path.strip()
                break

    # 1b) Fallback title extraction with filtering.
    for d in _walk_dicts(data):
        if result["title"]:
            break
        for key in ("listingName", "name", "title", "heading", "seoTitle"):
            value = d.get(key)
            if not isinstance(value, str):
                continue
            cleaned = value.strip()
            if len(cleaned) < 8:
                continue
            if cleaned.lower() in generic_titles:
                continue
            if cleaned.startswith("http") or "$" in cleaned:
                continue
            if re.search(r"[A-Za-z]", cleaned):
                result["title"] = cleaned
                break
        if result["title"]:
            break

    # 2) Minimal metadata extraction (postal/country codes only).
    # Core structural/location fields are intentionally sourced ONLY from:
    #   - metadata.sharingConfig
    #   - sbuiData OVERVIEW_DEFAULT_V2
    for d in _walk_dicts(data):
        if not result["postal_code"] and isinstance(d.get("postalCode"), str) and d.get("postalCode").strip():
            result["postal_code"] = d.get("postalCode").strip()
        if not result["country_code"] and isinstance(d.get("countryCode"), str) and d.get("countryCode").strip():
            result["country_code"] = d.get("countryCode").strip().upper()
        if not result["postal_code"] and isinstance(d.get("postalCode"), str) and d.get("postalCode").strip():
            result["postal_code"] = d.get("postalCode").strip()

    # 3) Strict booking section parser only.
    # Primary source: section.structuredDisplayPrice.primaryLine.price
    # Priority: BOOK_IT_FLOATING_FOOTER -> BOOK_IT_SIDEBAR -> any section with price.
    if isinstance(sections_root, list):
        section_priority = ("BOOK_IT_FLOATING_FOOTER", "BOOK_IT_SIDEBAR")
        section_by_id: Dict[str, Dict[str, Any]] = {}
        all_entries: List[Dict[str, Any]] = []
        for entry in sections_root:
            if not isinstance(entry, dict):
                continue
            all_entries.append(entry)
            sid = entry.get("sectionId")
            if isinstance(sid, str):
                section_by_id[sid] = entry

        def _apply_booking_price_from_entry(entry: Dict[str, Any], *, require_available: bool) -> bool:
            sec = entry.get("section")
            if not isinstance(sec, dict):
                return False
            if require_available and sec.get("available") is not True:
                return False
            sdp = sec.get("structuredDisplayPrice")
            if not isinstance(sdp, dict):
                return False

            breakdown = _parse_pdp_booking_breakdown_nightly(sdp)
            if breakdown is not None:
                amount, ccy, price_nights = breakdown
                result["nightly_price"] = round(float(amount), 2)
                result["total_price"] = round(float(amount) * max(1, int(price_nights)), 2)
                result["price_nights"] = max(1, int(price_nights))
                result["price_kind"] = "nightly_from_pdp_breakdown"
                if ccy:
                    result["currency"] = ccy
                return True

            primary = sdp.get("primaryLine")
            if not isinstance(primary, dict):
                return False

            # Airbnb can omit `price` for some PDP contexts while still exposing
            # amount text in `discountedPrice` or `accessibilityLabel`.
            price_candidates: List[str] = []
            accessibility_label = str(primary.get("accessibilityLabel") or "")
            candidate_keys = (
                ("discountedPrice", "accessibilityLabel", "price")
                if _mentions_original_price(accessibility_label)
                else ("discountedPrice", "price", "accessibilityLabel")
            )
            for key in candidate_keys:
                v = primary.get(key)
                if isinstance(v, str) and v.strip():
                    price_candidates.append(v.strip())

            amount: Optional[float] = None
            ccy: Optional[str] = None
            for candidate in price_candidates:
                parsed_amount, parsed_ccy = _parse_dollar_amount_currency(candidate)
                if parsed_amount is None:
                    continue
                amount = parsed_amount
                ccy = parsed_ccy
                break
            if amount is None:
                return False
            qualifier = str(primary.get("qualifier") or "").lower()
            normalized = normalize_raw_price(
                amount,
                qualifier=qualifier,
                context_text=" ".join(price_candidates + [qualifier]),
                stay_nights=1,
                source="pdp",
                currency=ccy,
                produce_nightly_from_total=True,
                round_direct_nightly_to_whole=True,
            )
            if normalized is None:
                return False
            result["nightly_price"] = normalized.nightly_price
            result["total_price"] = normalized.total_price
            result["price_nights"] = normalized.price_nights
            result["price_kind"] = normalized.price_kind
            if ccy:
                result["currency"] = ccy
            return True
        parsed_price = False
        for require_available in (True, False):
            if parsed_price:
                break
            for sid in section_priority:
                entry = section_by_id.get(sid)
                if isinstance(entry, dict) and _apply_booking_price_from_entry(entry, require_available=require_available):
                    parsed_price = True
                    break

        if not parsed_price:
            priority_set = set(section_priority)
            fallback_entries = [
                e for e in all_entries
                if not isinstance(e.get("sectionId"), str) or e.get("sectionId") not in priority_set
            ]
            for require_available in (True, False):
                if parsed_price:
                    break
                for entry in fallback_entries:
                    if _apply_booking_price_from_entry(entry, require_available=require_available):
                        parsed_price = True
                        break

    # 6) Amenities from common shapes (exclude "not included"/unavailable amenities).
    amenity_names = set()
    blocked_amenity_names = set()
    negative_group_markers = (
        "not included",
        "not available",
        "unavailable",
        "not provided",
    )

    def _is_amenity_available(item: Dict[str, Any]) -> bool:
        for key in ("available", "isAvailable", "present", "isPresent", "enabled", "isEnabled"):
            v = item.get(key)
            if isinstance(v, bool):
                return bool(v)
        if item.get("unavailable") is True or item.get("isUnavailable") is True:
            return False
        return True

    def _collect_amenity_items(node: Any, parent_negative: bool = False) -> None:
        if isinstance(node, list):
            for item in node:
                _collect_amenity_items(item, parent_negative=parent_negative)
            return
        if isinstance(node, dict):
            title = node.get("title") or node.get("name") or node.get("label")
            title_text = str(title or "").strip()
            node_negative = parent_negative
            if title_text and any(marker in title_text.lower() for marker in negative_group_markers):
                node_negative = True

            if title_text and not any(isinstance(node.get(k), list) for k in ("amenities", "items", "previewAmenitiesGroups", "seeAllAmenitiesGroups", "amenityGroups")):
                if node_negative or not _is_amenity_available(node):
                    blocked_amenity_names.add(title_text)
                else:
                    amenity_names.add(title_text)

            for key in ("amenities", "items", "previewAmenitiesGroups", "seeAllAmenitiesGroups", "amenityGroups"):
                child = node.get(key)
                if isinstance(child, (list, dict)):
                    _collect_amenity_items(child, parent_negative=node_negative)
            return
        if isinstance(node, str) and node.strip():
            if parent_negative:
                blocked_amenity_names.add(node.strip())
            else:
                amenity_names.add(node.strip())
    amenities_candidates = (
        _find_keys(data, "amenities")
        + _find_keys(data, "previewAmenitiesGroups")
        + _find_keys(data, "amenityGroups")
    )

    for candidate in amenities_candidates:
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, dict):
                    name = item.get("title") or item.get("name") or item.get("label")
                    if isinstance(name, str) and name.strip():
                        clean_name = name.strip()
                        if _is_amenity_available(item):
                            amenity_names.add(clean_name)
                        else:
                            blocked_amenity_names.add(clean_name)
                elif isinstance(item, str) and item.strip():
                    amenity_names.add(item.strip())
        elif isinstance(candidate, dict):
            group_title = str(candidate.get("title") or candidate.get("name") or "").strip().lower()
            group_is_negative = any(marker in group_title for marker in negative_group_markers)
            for k in ("title", "name", "label"):
                v = candidate.get(k)
                if isinstance(v, str) and v.strip():
                    clean_name = v.strip()
                    if group_is_negative:
                        blocked_amenity_names.add(clean_name)
                    else:
                        amenity_names.add(clean_name)

    # Parse amenityGroups with group-level include/exclude semantics.
    for group in _find_keys(data, "amenityGroups"):
        if not isinstance(group, list):
            continue
        for g in group:
            if not isinstance(g, dict):
                continue
            g_title = str(g.get("title") or g.get("name") or "").strip().lower()
            g_negative = any(marker in g_title for marker in negative_group_markers)
            for key in ("amenities", "items"):
                items = g.get(key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict):
                        nm = item.get("title") or item.get("name") or item.get("label")
                        if isinstance(nm, str) and nm.strip():
                            clean_name = nm.strip()
                            if g_negative or not _is_amenity_available(item):
                                blocked_amenity_names.add(clean_name)
                            else:
                                amenity_names.add(clean_name)
                    elif isinstance(item, str) and item.strip():
                        if g_negative:
                            blocked_amenity_names.add(item.strip())
                        else:
                            amenity_names.add(item.strip())

    # Explicitly parse AMENITIES_* sections from section payloads.
    if isinstance(sections_root, list):
        for entry in sections_root:
            if not isinstance(entry, dict):
                continue
            sid = str(entry.get("sectionId") or "").upper()
            if "AMENITIES" not in sid:
                continue
            sec = entry.get("section")
            if not isinstance(sec, dict):
                continue
            for key in ("previewAmenitiesGroups", "seeAllAmenitiesGroups", "amenityGroups", "amenities", "items"):
                if key in sec:
                    _collect_amenity_items(sec.get(key))

    # Some payload shapes keep amenity groups outside `sections_root` in SBUI objects.
    for key in ("previewAmenitiesGroups", "seeAllAmenitiesGroups", "amenityGroups", "amenities"):
        for candidate in _find_keys(data, key):
            _collect_amenity_items(candidate)

    for candidate in _find_keys(data, "brandAccessibilityLabel") + _find_keys(data, "textAccessibilityLabel"):
        if isinstance(candidate, str) and re.search(r"guest favou?rite", candidate, re.I):
            amenity_names.add("Guest favorite")
            break

    # Fallback for payloads that omit AMENITIES_* sections.
    # Parse safety/property bullets and media-tour stop labels that represent amenities.
    policies_sections = _find_keys(data, "safetyAndPropertiesSections")
    for groups in policies_sections:
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for item in group.get("items") or []:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                clean_title = re.sub(r"\s+installed$", "", title, flags=re.I).strip()
                if clean_title:
                    amenity_names.add(clean_title)

    for preview in _find_keys(data, "previewSafetyAndProperties"):
        if not isinstance(preview, list):
            continue
        for item in preview:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if title:
                amenity_names.add(title)

    for groups in _find_keys(data, "houseRulesSections"):
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for item in group.get("items") or []:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip().lower()
                if "self check-in" in title or "smart lock" in title:
                    amenity_names.add("Self check-in")

    media_tour_stops = _get_nested(
        data,
        ["data", "node", "pdpPresentation", "mediaTour", "stops"],
    )
    if isinstance(media_tour_stops, list):
        for stop in media_tour_stops:
            if not isinstance(stop, dict):
                continue
            stop_name = str(stop.get("name") or "").strip().lower()
            if not stop_name:
                continue
            if "kitchen" in stop_name:
                amenity_names.add("Kitchen")
            if "workspace" in stop_name:
                amenity_names.add("Dedicated workspace")
            if "backyard" in stop_name:
                amenity_names.add("Backyard")
            if "gym" in stop_name:
                amenity_names.add("Gym")
            if "pool" in stop_name:
                amenity_names.add("Pool")
            if "hot tub" in stop_name:
                amenity_names.add("Hot tub")
            if "parking" in stop_name:
                amenity_names.add("Free parking")
            if "washer" in stop_name:
                amenity_names.add("Washer")
            if "dryer" in stop_name:
                amenity_names.add("Dryer")

    result["amenities"] = sorted(a for a in amenity_names if a not in blocked_amenity_names)
    logger.info(
        "[amenities][pdp] listing_id=%s amenities=%s",
        listing_id,
        result["amenities"],
    )

    if not result["title"]:
        result["title"] = f"Listing {listing_id}"

    return result


def parse_pdp_baths_property_type_fast(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fast-path PDP extractor for compset enrichment.

    Reads only:
      - metadata.sharingConfig.propertyType
      - sbuiData.sectionConfiguration.root.sections[*] OVERVIEW_DEFAULT_V2.overviewItems[*].title (bath)

    Returns:
      {"baths": Optional[float], "property_type": Optional[str]}
    """
    out: Dict[str, Any] = {"baths": None, "property_type": None}

    # 1) Property type from metadata.sharingConfig (preferred).
    sharing = _get_nested(
        data,
        [
            "data",
            "presentation",
            "stayProductDetailPage",
            "sections",
            "metadata",
            "sharingConfig",
        ],
    )
    if not isinstance(sharing, dict):
        sharing = _get_nested(
            data,
            [
                "data",
                "presentation",
                "stayproductdetailpage",
                "sections",
                "metadata",
                "sharingConfig",
            ],
        )
    if isinstance(sharing, dict):
        ptype = sharing.get("propertyType")
        if isinstance(ptype, str) and ptype.strip():
            out["property_type"] = ptype.strip()

    # 2) Bath count from OVERVIEW_DEFAULT_V2.overviewItems[*].title.
    root_sections = _get_nested(
        data,
        [
            "data",
            "presentation",
            "stayProductDetailPage",
            "sections",
            "sbuiData",
            "sectionConfiguration",
            "root",
            "sections",
        ],
    )
    if not isinstance(root_sections, list):
        root_sections = _get_nested(
            data,
            [
                "data",
                "presentation",
                "stayproductdetailpage",
                "sections",
                "sbuiData",
                "sectionConfiguration",
                "root",
                "sections",
            ],
        )

    if isinstance(root_sections, list):
        for entry in root_sections:
            if not isinstance(entry, dict):
                continue
            section_id = str(entry.get("sectionId") or "").strip().upper()
            if section_id != "OVERVIEW_DEFAULT_V2":
                continue
            section_data = entry.get("sectionData")
            if not isinstance(section_data, dict):
                break
            items = section_data.get("overviewItems")
            if not isinstance(items, list):
                break
            for item in items:
                text = ""
                if isinstance(item, dict):
                    t = item.get("title")
                    if isinstance(t, str):
                        text = t.strip()
                elif isinstance(item, str):
                    text = item.strip()
                if not text:
                    continue
                m = _BATH_RE.search(text)
                if m:
                    try:
                        out["baths"] = float(m.group(1))
                    except Exception:
                        pass
                    break
            break

    return out
