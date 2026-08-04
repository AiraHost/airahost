"""Exception types shared between the worker loop and the scrape pipeline."""


class ReportInputError(ValueError):
    """A report failed because of its own inputs, not because a scrape failed.

    The message is written straight to pricing_reports.error_message, so it must
    read as user-facing guidance. Internal failures must NOT raise this type —
    they fall through to the generic handler in main.py, which shows a safe
    message and keeps the raw detail in result_core_debug.

    Subclasses ValueError so existing callers that catch ValueError still work.
    """
