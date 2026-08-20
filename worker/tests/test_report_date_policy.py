"""Past-date jobs must be stopped before any Airbnb request.

The 2026-08-12 production run queried 2026-08-03 → 2026-08-10, a window that
had already passed. Airbnb does not reject past-date filters — it renders an
ordinary search page with the dates normalized away — so the scrape returned
listings with no date-specific prices, which read downstream as a sparse
market and was first misdiagnosed as a challenge.

Policy is reject, decided in an explicit business timezone rather than the
worker host's local one.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from worker.core.errors import ReportInputError, StaleReportJobError
from worker.core.report_dates import (
    PAST_DATES_MESSAGE,
    business_today,
    validate_report_dates,
)


def _utc(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ── The incident window (required test 29) ──────────────────────────────────

def test_the_production_incident_window_is_rejected_before_any_scrape(monkeypatch):
    monkeypatch.setenv("REPORT_BUSINESS_TIMEZONE", "UTC")

    with pytest.raises(StaleReportJobError) as ctx:
        validate_report_dates("2026-08-03", "2026-08-10", now=_utc(2026, 8, 12))

    assert str(ctx.value) == PAST_DATES_MESSAGE
    assert ctx.value.reason_code == "report_dates_in_past"
    assert ctx.value.debug["staleByDays"] == 9
    assert ctx.value.debug["requestedCheckin"] == "2026-08-03"
    assert ctx.value.debug["businessToday"] == "2026-08-12"
    assert ctx.value.debug["policy"] == "reject_past_dates"


def test_stale_job_error_is_a_report_input_error_so_the_report_fails_visibly():
    # ReportInputError messages are written straight to the report, which is
    # what makes this actionable instead of a silent wrong-window report.
    assert issubclass(StaleReportJobError, ReportInputError)
    assert issubclass(StaleReportJobError, ValueError)


def test_today_and_future_check_ins_pass(monkeypatch):
    monkeypatch.setenv("REPORT_BUSINESS_TIMEZONE", "UTC")
    assert validate_report_dates("2026-08-12", "2026-08-19", now=_utc(2026, 8, 12))[0] == "2026-08-12"
    assert validate_report_dates("2026-09-01", "2026-09-08", now=_utc(2026, 8, 12))[0] == "2026-09-01"


def test_inverted_and_unparseable_ranges_are_rejected_with_their_own_reason(monkeypatch):
    monkeypatch.setenv("REPORT_BUSINESS_TIMEZONE", "UTC")

    with pytest.raises(StaleReportJobError) as inverted:
        validate_report_dates("2026-09-10", "2026-09-03", now=_utc(2026, 8, 12))
    assert inverted.value.reason_code == "report_dates_inverted"

    with pytest.raises(StaleReportJobError) as bad:
        validate_report_dates("not-a-date", "2026-09-03", now=_utc(2026, 8, 12))
    assert bad.value.reason_code == "report_dates_unparseable"

    with pytest.raises(StaleReportJobError) as same_day:
        validate_report_dates("2026-09-03", "2026-09-03", now=_utc(2026, 8, 12))
    assert same_day.value.reason_code == "report_dates_inverted"


def test_no_dates_policy_decision_is_ever_silent():
    # Rejection carries the full decision context so an operator can tell a
    # stale queue from a timezone misconfiguration.
    with pytest.raises(StaleReportJobError) as ctx:
        validate_report_dates("2020-01-01", "2020-01-05", now=_utc(2026, 8, 12))
    for key in ("requestedCheckin", "requestedCheckout", "businessTimezone", "businessToday"):
        assert key in ctx.value.debug


# ── Timezone determinism (required test 30) ─────────────────────────────────

def test_business_date_comes_from_the_configured_timezone_not_the_host(monkeypatch):
    # 2026-08-12 01:00 UTC is still 2026-08-11 in Toronto (UTC-4).
    instant = _utc(2026, 8, 12, 1, 0)

    monkeypatch.setenv("REPORT_BUSINESS_TIMEZONE", "UTC")
    assert business_today(instant).isoformat() == "2026-08-12"

    monkeypatch.setenv("REPORT_BUSINESS_TIMEZONE", "America/Toronto")
    assert business_today(instant).isoformat() == "2026-08-11"


def test_timezone_boundary_changes_the_verdict_deterministically(monkeypatch):
    instant = _utc(2026, 8, 12, 1, 0)  # 2026-08-11 21:00 in Toronto

    # In UTC the business day has already rolled over, so an 08-11 check-in is stale.
    monkeypatch.setenv("REPORT_BUSINESS_TIMEZONE", "UTC")
    with pytest.raises(StaleReportJobError):
        validate_report_dates("2026-08-11", "2026-08-14", now=instant)

    # In Toronto it is still 08-11, so the same job is valid.
    monkeypatch.setenv("REPORT_BUSINESS_TIMEZONE", "America/Toronto")
    assert validate_report_dates("2026-08-11", "2026-08-14", now=instant)[0] == "2026-08-11"


def test_naive_now_is_treated_as_utc_not_as_host_local_time(monkeypatch):
    monkeypatch.setenv("REPORT_BUSINESS_TIMEZONE", "UTC")
    naive = datetime(2026, 8, 12, 3, 0)
    assert business_today(naive).isoformat() == "2026-08-12"


def test_unknown_timezone_falls_back_to_utc_rather_than_the_host(monkeypatch):
    monkeypatch.setenv("REPORT_BUSINESS_TIMEZONE", "Mars/Olympus_Mons")
    assert business_today(_utc(2026, 8, 12)).isoformat() == "2026-08-12"


def test_default_timezone_is_utc_when_unset(monkeypatch):
    monkeypatch.delenv("REPORT_BUSINESS_TIMEZONE", raising=False)
    assert business_today(_utc(2026, 8, 12, 23, 30)).isoformat() == "2026-08-12"


def test_grace_days_are_explicit_and_default_to_zero(monkeypatch):
    monkeypatch.setenv("REPORT_BUSINESS_TIMEZONE", "UTC")
    monkeypatch.delenv("REPORT_STALE_GRACE_DAYS", raising=False)

    # Default: yesterday is stale.
    with pytest.raises(StaleReportJobError):
        validate_report_dates("2026-08-11", "2026-08-14", now=_utc(2026, 8, 12))

    # A deployment serving markets west of its business timezone can allow one
    # day; the incident's 9-day-stale window still fails.
    monkeypatch.setenv("REPORT_STALE_GRACE_DAYS", "1")
    assert validate_report_dates("2026-08-11", "2026-08-14", now=_utc(2026, 8, 12))[0] == "2026-08-11"
    with pytest.raises(StaleReportJobError):
        validate_report_dates("2026-08-03", "2026-08-10", now=_utc(2026, 8, 12))
