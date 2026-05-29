import re
from datetime import datetime
from typing import Any, Dict, List, Optional


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(float(value))
    except Exception:
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _extract_money(text: str) -> Optional[float]:
    if not isinstance(text, str) or not text.strip():
        return None
    m = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text.replace("\xa0", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None


def _extract_currency(text: str, fallback: str) -> str:
    if not isinstance(text, str):
        return fallback
    m = re.search(r"\$\s*[0-9][0-9,]*(?:\.[0-9]+)?\s+([A-Za-z]{3})\b", text.replace("\xa0", " "))
    if m:
        return str(m.group(1)).upper()
    return str(fallback or "USD").upper()


def _nights(checkin: str, checkout: str) -> int:
    try:
        d0 = datetime.strptime(checkin, "%Y-%m-%d").date()
        d1 = datetime.strptime(checkout, "%Y-%m-%d").date()
        return max(1, (d1 - d0).days)
    except Exception:
        return 1


def parse_deepbnb_search_to_stayssearch_payload(
    data: Dict[str, Any],
    *,
    checkin: str,
    checkout: str,
    currency: str,
) -> Dict[str, Any]:
    """
    Convert deepbnb-style ExploreSearch/StaysSearch responses into the
    staysSearch.results.searchResults shape expected by existing parsers.
    """
    # Already in native expected shape.
    native_results = (
        (((data or {}).get("data") or {}).get("presentation") or {}).get("staysSearch") or {}
    )
    if isinstance(native_results, dict) and isinstance((native_results.get("results") or {}).get("searchResults"), list):
        return data

    rows: List[Dict[str, Any]] = []
    total_count = 0
    nights = _nights(checkin, checkout)

    explore = (((data or {}).get("data") or {}).get("dora") or {}).get("exploreV3") or {}
    sections = explore.get("sections") or []
    if isinstance(sections, list):
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            if str(sec.get("sectionComponentType") or "") != "listings_ListingsGrid_Explore":
                continue
            for item in sec.get("items") or []:
                if not isinstance(item, dict):
                    continue
                listing = item.get("listing") if isinstance(item.get("listing"), dict) else {}
                pricing = item.get("pricingQuote") if isinstance(item.get("pricingQuote"), dict) else {}
                lid = listing.get("id")
                if lid is None:
                    continue
                lid_s = str(lid)
                title = str(listing.get("name") or f"Listing {lid_s}")
                subtitle = str(listing.get("roomAndPropertyType") or "")
                person_capacity = _to_int(listing.get("personCapacity"))
                bedrooms = _to_int(listing.get("bedrooms"))
                beds = _to_int(listing.get("beds"))
                baths = _to_float(listing.get("bathrooms"))
                lat = _to_float(listing.get("lat"))
                lng = _to_float(listing.get("lng"))

                primary_price = None
                primary_qualifier = "night"
                sdp = pricing.get("structuredStayDisplayPrice") if isinstance(pricing.get("structuredStayDisplayPrice"), dict) else {}
                primary_line = sdp.get("primaryLine") if isinstance(sdp.get("primaryLine"), dict) else {}
                if isinstance(primary_line.get("discountedPrice"), str):
                    primary_price = primary_line.get("discountedPrice")
                    primary_qualifier = str(primary_line.get("qualifier") or "").lower() or "night"
                elif isinstance(primary_line.get("price"), str):
                    primary_price = primary_line.get("price")
                    primary_qualifier = str(primary_line.get("qualifier") or "").lower() or "night"

                if not primary_price and isinstance(pricing.get("rateWithServiceFee"), dict):
                    amount = _to_float((pricing.get("rateWithServiceFee") or {}).get("amount"))
                    if isinstance(amount, (int, float)) and amount > 0:
                        primary_price = f"${float(amount):.2f} {currency}"
                        primary_qualifier = "night"

                if not primary_price and isinstance(pricing.get("price"), dict):
                    amount = _to_float((pricing.get("price") or {}).get("amount"))
                    if isinstance(amount, (int, float)) and amount > 0:
                        # pricing.price.amount in Deepbnb payload is treated as a
                        # stay total; do not multiply by nights here.
                        primary_price = f"${float(amount):.2f} {currency}"
                        primary_qualifier = f"for {nights} nights" if nights > 1 else "total"

                row: Dict[str, Any] = {
                    "__typename": "SkinnyListingItem",
                    "listingId": lid_s,
                    "title": title,
                    "subtitle": subtitle,
                    "personCapacity": person_capacity,
                    "bedrooms": bedrooms,
                    "beds": beds,
                    "bathrooms": baths,
                    "lat": lat,
                    "lng": lng,
                    "reviewCount": _to_int(listing.get("reviewsCount")),
                    "starRating": _to_float(listing.get("starRating") or listing.get("avgRating")),
                }
                if isinstance(primary_price, str):
                    row["structuredDisplayPrice"] = {
                        "primaryLine": {
                            "price": primary_price,
                            "qualifier": primary_qualifier,
                            "accessibilityLabel": primary_price,
                        }
                    }
                rows.append(row)

    # fallback count from metadata if present
    meta = explore.get("metadata") if isinstance(explore.get("metadata"), dict) else {}
    if isinstance(meta.get("listings_count"), (int, float)):
        total_count = int(meta.get("listings_count"))
    if total_count <= 0:
        total_count = len(rows)

    return {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": rows,
                        "filters": {
                            "filterPanel": {
                                "searchButtonText": f"Show {total_count} places",
                            }
                        },
                    }
                }
            }
        }
    }


def parse_deepbnb_pdp_to_stayspdp_payload(
    data: Dict[str, Any],
    *,
    listing_id: str,
    checkin: str,
    checkout: str,
    currency: str,
) -> Dict[str, Any]:
    """
    Convert deepbnb-style PdpPlatformSections responses into a minimal
    stayProductDetailPage payload expected by existing PDP parsers.
    """
    # Already in expected shape.
    native = (((data or {}).get("data") or {}).get("presentation") or {}).get("stayProductDetailPage")
    if isinstance(native, dict):
        return data

    pdp_sections = (((data or {}).get("data") or {}).get("merlin") or {}).get("pdpSections") or {}
    sections = pdp_sections.get("sections") if isinstance(pdp_sections.get("sections"), list) else []
    metadata = pdp_sections.get("metadata") if isinstance(pdp_sections.get("metadata"), dict) else {}

    overview_items: List[Dict[str, str]] = []
    property_type = ""
    location = ""
    primary_price_text: Optional[str] = None

    # Extract from known sections.
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        sid = str(sec.get("sectionId") or "")
        section_data = sec.get("section") if isinstance(sec.get("section"), dict) else {}

        if sid in ("OVERVIEW_DEFAULT_V2", "OVERVIEW_DEFAULT") and isinstance(section_data.get("overviewItems"), list):
            for it in section_data.get("overviewItems") or []:
                title = str((it or {}).get("title") or "").strip()
                if title:
                    overview_items.append({"title": title})

        if sid in ("BOOK_IT_FLOATING_FOOTER", "BOOK_IT_SIDEBAR", "BOOK_IT_NAV"):
            sdp = section_data.get("structuredDisplayPrice") if isinstance(section_data.get("structuredDisplayPrice"), dict) else {}
            primary = sdp.get("primaryLine") if isinstance(sdp.get("primaryLine"), dict) else {}
            for key in ("discountedPrice", "price", "accessibilityLabel"):
                text = primary.get(key)
                if isinstance(text, str) and _extract_money(text) is not None:
                    primary_price_text = text
                    break

    # Metadata-driven fallback.
    sharing = metadata.get("sharingConfig") if isinstance(metadata.get("sharingConfig"), dict) else {}
    if isinstance(sharing.get("propertyType"), str):
        property_type = sharing.get("propertyType").strip()
    if isinstance(sharing.get("location"), str):
        location = sharing.get("location").strip()

    if not primary_price_text:
        # Last-resort scan for any price-like string.
        candidates: List[str] = []
        def _walk(o: Any):
            if isinstance(o, dict):
                for v in o.values():
                    _walk(v)
            elif isinstance(o, list):
                for v in o:
                    _walk(v)
            elif isinstance(o, str):
                candidates.append(o)
        _walk(data)
        for c in candidates:
            amount = _extract_money(c)
            if isinstance(amount, (int, float)) and amount > 0:
                ccy = _extract_currency(c, currency)
                primary_price_text = f"${float(amount):.2f} {ccy}"
                break

    if not primary_price_text:
        # Fallback for payloads that expose numeric price amounts without formatted strings.
        numeric_candidates: List[float] = []

        def _walk_numeric(o: Any):
            if isinstance(o, dict):
                for k, v in o.items():
                    lk = str(k or "").lower()
                    if isinstance(v, (int, float)):
                        if ("price" in lk or "amount" in lk or "rate" in lk) and 10 <= float(v) <= 200000:
                            numeric_candidates.append(float(v))
                    else:
                        _walk_numeric(v)
            elif isinstance(o, list):
                for v in o:
                    _walk_numeric(v)

        _walk_numeric(data)
        if numeric_candidates:
            primary_price_text = f"${numeric_candidates[0]:.2f} {str(currency or 'USD').upper()}"

    if not overview_items:
        # Populate basic overview fallback from metadata.
        pc = sharing.get("personCapacity")
        if isinstance(pc, (int, float)):
            overview_items.append({"title": f"{int(pc)} guests"})

    return {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_FLOATING_FOOTER",
                                "sectionContentStatus": "COMPLETE",
                                "section": {
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": primary_price_text,
                                            "qualifier": "total" if _nights(checkin, checkout) > 1 else "night",
                                            "accessibilityLabel": primary_price_text,
                                        }
                                    }
                                },
                            }
                        ] if isinstance(primary_price_text, str) else [],
                        "metadata": {
                            "sharingConfig": {
                                "propertyType": property_type or None,
                                "location": location or None,
                            }
                        },
                        "sbuiData": {
                            "sectionConfiguration": {
                                "root": {
                                    "sections": [
                                        {
                                            "sectionId": "OVERVIEW_DEFAULT_V2",
                                            "sectionData": {
                                                "title": f"{property_type} in {location}".strip() or f"Listing {listing_id}",
                                                "overviewItems": overview_items,
                                            },
                                        }
                                    ]
                                }
                            }
                        },
                    }
                }
            }
        }
    }
