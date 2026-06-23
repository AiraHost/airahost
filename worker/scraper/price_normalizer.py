from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class NormalizedPrice:
    nightly_price: Optional[float]
    total_price: Optional[float]
    price_nights: int
    price_kind: str
    currency: Optional[str] = None


_FOR_NIGHTS_RE = re.compile(r"\bfor\s+(\d+)\s+nights?\b", re.I)


def _positive_amount(value: Any) -> Optional[float]:
    if not isinstance(value, (int, float)):
        return None
    amount = float(value)
    return amount if amount > 0 else None


def extract_price_nights(text: str, default: int = 1) -> int:
    fallback = max(1, int(default or 1))
    if not isinstance(text, str) or not text:
        return fallback
    match = _FOR_NIGHTS_RE.search(text)
    if not match:
        return fallback
    try:
        nights = int(match.group(1))
    except Exception:
        return fallback
    return nights if nights > 0 else fallback


def _airbnb_display_whole_dollar(amount: float) -> int:
    return int(math.ceil(float(amount)))


def normalize_raw_price(
    amount: Any,
    *,
    qualifier: str = "",
    context_text: str = "",
    stay_nights: int = 1,
    source: str = "price",
    currency: Optional[str] = None,
    produce_nightly_from_total: bool = True,
    round_direct_nightly_to_whole: bool = False,
) -> Optional[NormalizedPrice]:
    """
    Convert raw scraped price text into one nightly/total interpretation.

    Scrapers should pass raw amount plus nearby labels here instead of deciding
    locally whether a value is nightly or a multi-night trip total.
    """
    raw_amount = _positive_amount(amount)
    if raw_amount is None:
        return None

    try:
        requested_nights = max(1, int(stay_nights or 1))
    except Exception:
        requested_nights = 1

    qualifier_text = str(qualifier or "")
    combined_text = f"{qualifier_text} {context_text or ''}".strip()
    lower = combined_text.lower()
    has_for_nights = _FOR_NIGHTS_RE.search(combined_text) is not None
    explicit_nights = extract_price_nights(combined_text, default=requested_nights)
    price_nights = explicit_nights if has_for_nights else requested_nights

    is_nightly = (
        "night" in qualifier_text.lower()
        or "/night" in lower
        or "per night" in lower
        or (has_for_nights and price_nights == 1 and "total" not in lower)
    )
    is_total = (
        (has_for_nights and price_nights > 1)
        or "total" in lower
        or (not is_nightly and requested_nights > 1)
    )

    if is_total:
        total = (
            _airbnb_display_whole_dollar(raw_amount)
            if round_direct_nightly_to_whole
            else round(raw_amount, 2)
        )
        nightly = round(total / max(1, price_nights), 2) if produce_nightly_from_total else None
        return NormalizedPrice(
            nightly_price=nightly,
            total_price=total,
            price_nights=max(1, price_nights),
            price_kind=f"trip_total_from_{source}",
            currency=currency,
        )

    nightly_amount = (
        _airbnb_display_whole_dollar(raw_amount)
        if round_direct_nightly_to_whole
        else round(raw_amount, 2)
    )
    return NormalizedPrice(
        nightly_price=nightly_amount,
        total_price=nightly_amount,
        price_nights=1,
        price_kind=f"nightly_from_{source}",
        currency=currency,
    )


def nightly_price_from_parsed_pdp(
    parsed: Dict[str, Any],
    *,
    stay_nights: int,
    source: str = "pdp",
) -> Optional[float]:
    """
    Return the canonical nightly price from parse_pdp_response().

    parse_pdp_response already normalizes Airbnb PDP totals and prefers booking
    breakdown rows when present. Callers should only derive nightly from total
    when the parser did not provide a usable nightly value.
    """
    nightly = parsed.get("nightly_price") if isinstance(parsed, dict) else None
    if isinstance(nightly, (int, float)) and nightly > 0:
        return float(nightly)

    total = parsed.get("total_price") if isinstance(parsed, dict) else None
    if not isinstance(total, (int, float)) or total <= 0:
        return None

    normalized = normalize_raw_price(
        total,
        qualifier="total",
        stay_nights=stay_nights,
        source=source,
        produce_nightly_from_total=True,
    )
    if normalized is None or not isinstance(normalized.nightly_price, (int, float)):
        return None
    return float(normalized.nightly_price)


def select_price_from_dom_candidates(
    candidates: List[Dict[str, Any]],
) -> Optional[Tuple[float, str]]:
    if not candidates:
        return None

    non_strikethrough = [c for c in candidates if not c.get("strikethrough")]
    if not non_strikethrough:
        return None

    has_strikethrough = any(c.get("strikethrough") for c in candidates)
    best = sorted(non_strikethrough, key=lambda c: c.get("domIndex", 0))[-1]
    try:
        trip_nights = max(1, int(best.get("tripNights") or 1))
    except Exception:
        trip_nights = 1

    normalized = normalize_raw_price(
        best.get("value"),
        context_text=f"for {trip_nights} nights",
        stay_nights=trip_nights,
        source="dom",
        produce_nightly_from_total=True,
    )
    if normalized is None or not isinstance(normalized.nightly_price, (int, float)):
        return None
    if not (10 <= float(normalized.nightly_price) <= 10000):
        return None

    price_kind = "nightly_discounted" if has_strikethrough else "nightly_standard"
    return float(normalized.nightly_price), price_kind
