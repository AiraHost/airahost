"""Business-timezone validation of report date windows.

The 2026-08-12 incident ran a production job whose window was 2026-08-03 →
2026-08-10 — already in the past. Airbnb does not reject past-date filters; it
renders an ordinary search page with the dates normalized away, so the scrape
came back with listings and no date-specific prices. That is indistinguishable
downstream from a sparse market, which is why the failure was first read as a
challenge.

Policy: reject. A stale window never reaches Airbnb. Rolling the dates forward
would silently change what the user asked for, so it is not done here.

The decision date comes from REPORT_BUSINESS_TIMEZONE (default UTC), never from
the worker host's local timezone — two workers in different regions must reach
the same verdict on the same job.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional, Tuple

from worker.core.errors import StaleReportJobError

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - the worker targets 3.9+
    ZoneInfo = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_BUSINESS_TIMEZONE = "UTC"

# Tolerance, in days, for a check-in earlier than the business date.
# 0 = anything strictly before today's business date is stale.
#
# Nightly jobs are safe at 0: the scheduler derives startDate as *local
# tomorrow* in the listing's timezone, which is never behind any business
# timezone. Interactive same-day jobs from users far west of UTC can land one
# day behind a UTC business date, so a deployment serving such markets should
# either set REPORT_BUSINESS_TIMEZONE to its own market or raise this to 1.
DEFAULT_STALE_GRACE_DAYS = 0


def stale_grace_days() -> int:
    try:
        return max(0, int(os.getenv("REPORT_STALE_GRACE_DAYS", str(DEFAULT_STALE_GRACE_DAYS))))
    except (TypeError, ValueError):
        return DEFAULT_STALE_GRACE_DAYS

PAST_DATES_MESSAGE = (
    "Report dates are in the past. Please re-run with current dates."
)
INVALID_RANGE_MESSAGE = (
    "Invalid date range: check-out must be after check-in."
)


def business_timezone_name() -> str:
    return str(os.getenv("REPORT_BUSINESS_TIMEZONE", DEFAULT_BUSINESS_TIMEZONE) or DEFAULT_BUSINESS_TIMEZONE).strip()


def business_today(now: Optional[datetime] = None) -> date:
    """Today's date in the authoritative business timezone."""
    tz_name = business_timezone_name()
    tz: Any = timezone.utc
    if tz_name.upper() != "UTC" and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            logger.warning(
                "Unknown REPORT_BUSINESS_TIMEZONE=%s; falling back to UTC", tz_name
            )
            tz = timezone.utc
    # `now` is normalized to an aware UTC instant first so a naive host clock
    # can never leak the host's local offset into the comparison.
    if now is None:
        instant = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        instant = now.replace(tzinfo=timezone.utc)
    else:
        instant = now.astimezone(timezone.utc)
    return instant.astimezone(tz).date()


def _parse(value: Any, field: str) -> date:
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d").date()
    except Exception as exc:
        raise StaleReportJobError(
            INVALID_RANGE_MESSAGE,
            reason_code="report_dates_unparseable",
            debug={"field": field},
        ) from exc


def validate_report_dates(
    checkin: Any,
    checkout: Any,
    *,
    now: Optional[datetime] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    """Validate a report window before any Airbnb call.

    Returns (checkin, checkout, debug) on success. Raises StaleReportJobError —
    a ReportInputError, so the report ends failed with a user-facing message —
    when the window is unparseable, inverted, or already in the past.
    """
    d_in = _parse(checkin, "checkin")
    d_out = _parse(checkout, "checkout")
    today = business_today(now)
    tz_name = business_timezone_name()

    debug: Dict[str, Any] = {
        "requestedCheckin": d_in.isoformat(),
        "requestedCheckout": d_out.isoformat(),
        "businessTimezone": tz_name,
        "businessToday": today.isoformat(),
        "policy": "reject_past_dates",
    }

    if d_out <= d_in:
        raise StaleReportJobError(
            INVALID_RANGE_MESSAGE,
            reason_code="report_dates_inverted",
            debug=debug,
        )

    grace = stale_grace_days()
    debug["graceDays"] = grace
    if (today - d_in).days > grace:
        debug["staleByDays"] = (today - d_in).days
        raise StaleReportJobError(
            PAST_DATES_MESSAGE,
            reason_code=StaleReportJobError.ERROR_CODE,
            debug=debug,
        )

    return d_in.isoformat(), d_out.isoformat(), debug
