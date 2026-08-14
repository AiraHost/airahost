"""Exception types shared between the worker loop and the scrape pipeline."""


class ReportInputError(ValueError):
    """A report failed because of its own inputs, not because a scrape failed.

    The message is written straight to pricing_reports.error_message, so it must
    read as user-facing guidance. Internal failures must NOT raise this type —
    they fall through to the generic handler in main.py, which shows a safe
    message and keeps the raw detail in result_core_debug.

    Subclasses ValueError so existing callers that catch ValueError still work.
    """


class InputListingNotFound(ReportInputError):
    """The user's own listing URL does not resolve to a live Airbnb listing.

    Terminal and non-retryable: once a 404 is confirmed for the *input* listing,
    every downstream stage (target extraction, comp search, daily retry,
    target-only fallback, live-price capture, pool seeding, cache write) must be
    skipped. A 404 on a *comparable* listing is an ordinary rejected candidate
    and must never raise this.

    The public message is fixed so the report failure path is stable; room id /
    status / detection source belong in structured debug metadata instead.
    """

    PUBLIC_MESSAGE = "input listing not found"
    ERROR_CODE = "input_listing_not_found"

    def __init__(self, *, room_id: str = "", detail: str = ""):
        super().__init__(self.PUBLIC_MESSAGE)
        self.room_id = str(room_id or "")
        self.detail = str(detail or "")


class StaleReportJobError(ReportInputError):
    """The job asks for a check-in date that already passed in business time.

    Airbnb quietly normalizes or ignores past-date filters and still renders an
    ordinary search page, so a stale job produces listings with no date-specific
    prices rather than an error. Stopping at the job boundary keeps that
    ambiguity out of the scrape entirely.
    """

    ERROR_CODE = "report_dates_in_past"

    def __init__(self, message: str, *, reason_code: str = ERROR_CODE, debug=None):
        super().__init__(message)
        self.reason_code = str(reason_code or self.ERROR_CODE)
        self.debug = dict(debug or {})
