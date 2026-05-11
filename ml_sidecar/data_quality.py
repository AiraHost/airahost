from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from ml_sidecar.data import (
    TARGET_COLUMN_NAME,
    fetch_training_dataset,
    get_default_training_scope,
    normalize_training_scope,
)
from ml_sidecar.model import CATEGORICAL_FEATURE, NUMERIC_FEATURES, build_feature_matrix_df
from ml_sidecar.supabase_client import get_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_ROOT / "ml_sidecar" / "reports"

PRICE_SOURCE_COLUMNS = (
    "effective_daily_price_refundable",
    "effective_daily_price_non_refundable",
    "base_daily_price",
    "base_price",
)

CALENDAR_TO_OBSERVATION_PRICE_FIELDS = (
    ("baseDailyPrice", "base_daily_price"),
    ("basePrice", "base_price"),
    ("effectiveDailyPriceRefundable", "effective_daily_price_refundable"),
    ("effectiveDailyPriceNonRefundable", "effective_daily_price_non_refundable"),
)

RAW_REQUIRED_COLUMNS = (
    "saved_listing_id",
    "observed_at",
    "stay_date",
    "days_until_stay",
)

CRITICAL_TRAINING_COLUMNS = (
    "saved_listing_id",
    "property_type",
    "bedrooms",
    "baths",
    "accommodates",
    "beds",
    "lat",
    "lng",
    "price_date",
    "observation_date",
    TARGET_COLUMN_NAME,
)


@dataclass
class QualityIssue:
    severity: str
    code: str
    message: str
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QualityReport:
    generated_at: str
    status: str
    training_scope: str
    since_hours: int
    checked_report_ids: List[str]
    listing_ids: List[str]
    new_observation_count: int
    training_row_count: int
    feature_row_count: int
    feature_column_count: int
    baseline_row_count: int
    issues: List[QualityIssue] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["issues"] = [asdict(issue) for issue in self.issues]
        return payload


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _calendar_price(day: Dict[str, Any], key: str) -> Optional[float]:
    value = _safe_float(day.get(key))
    if value is not None and value > 0:
        return value
    return None


def _observation_price(row: Dict[str, Any], key: str) -> Optional[float]:
    value = _safe_float(row.get(key))
    if value is not None and value > 0:
        return value
    return None


def _add_issue(
    issues: List[QualityIssue],
    severity: str,
    code: str,
    message: str,
    **context: Any,
) -> None:
    issues.append(QualityIssue(severity=severity, code=code, message=message, context=context))


def _batched(values: Sequence[str], size: int = 50) -> Iterable[List[str]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def _fetch_nightly_reports(
    client: Any,
    *,
    report_ids: Sequence[str],
    listing_ids: Sequence[str],
    since: datetime,
    limit: int,
) -> List[Dict[str, Any]]:
    columns = (
        "id,listing_id,status,report_type,job_lane,completed_at,market_captured_at,"
        "result_calendar,result_core_debug"
    )

    if report_ids:
        rows: List[Dict[str, Any]] = []
        for batch in _batched(list(report_ids)):
            result = client.table("pricing_reports").select(columns).in_("id", batch).execute()
            rows.extend(result.data or [])
        return rows

    query = (
        client.table("pricing_reports")
        .select(columns)
        .eq("status", "ready")
        .eq("report_type", "live_analysis")
        .order("completed_at", desc=True)
        .limit(limit)
    )
    if listing_ids:
        query = query.in_("listing_id", list(listing_ids))
    else:
        query = query.gte("completed_at", since.isoformat())

    rows = query.execute().data or []
    return [
        row
        for row in rows
        if row.get("job_lane") == "nightly"
        or bool((row.get("result_core_debug") or {}).get("nightly"))
    ]


def _fetch_observations_for_reports(client: Any, report_ids: Sequence[str]) -> List[Dict[str, Any]]:
    if not report_ids:
        return []

    columns = (
        "pricing_report_id,saved_listing_id,observed_at,stay_date,days_until_stay,"
        "listing_property_type,listing_bedrooms,listing_baths,listing_accommodates,"
        "listing_beds,target_lat,target_lng,amenities,base_price,base_daily_price,"
        "effective_daily_price_refundable,effective_daily_price_non_refundable,"
        "comps_used,is_weekend,created_at"
    )
    rows: List[Dict[str, Any]] = []
    for batch in _batched(list(report_ids)):
        result = (
            client.table("market_price_observations")
            .select(columns)
            .in_("pricing_report_id", batch)
            .execute()
        )
        rows.extend(result.data or [])
    return rows


def _fetch_report_statuses(client: Any, report_ids: Sequence[str]) -> Dict[str, str]:
    if not report_ids:
        return {}

    statuses: Dict[str, str] = {}
    for batch in _batched(list(report_ids)):
        result = (
            client.table("pricing_reports")
            .select("id,status")
            .in_("id", batch)
            .execute()
        )
        for row in result.data or []:
            report_id = str(row.get("id") or "")
            status = str(row.get("status") or "")
            if report_id:
                statuses[report_id] = status
    return statuses


def _wait_for_reports_ready(
    client: Any,
    report_ids: Sequence[str],
    *,
    timeout_seconds: int,
    poll_seconds: int,
) -> None:
    if not report_ids:
        return

    deadline = time.time() + max(1, timeout_seconds)
    poll_interval = max(1, poll_seconds)
    pending = set(report_ids)

    while time.time() < deadline:
        statuses = _fetch_report_statuses(client, report_ids)
        error_reports = [
            report_id
            for report_id, status in statuses.items()
            if status == "error"
        ]
        if error_reports:
            raise RuntimeError(
                "Nightly report(s) failed before data quality checks: "
                + ", ".join(error_reports)
            )

        pending = {
            report_id
            for report_id in report_ids
            if statuses.get(report_id) != "ready"
        }
        if not pending:
            return

        print(
            "[ML Data Quality] Waiting for report(s) to become ready: "
            + ", ".join(sorted(pending)),
            flush=True,
        )
        time.sleep(poll_interval)

    raise TimeoutError(
        "Timed out waiting for nightly report(s) to become ready: "
        + ", ".join(sorted(pending))
    )


def _fetch_baseline_observation_count(
    client: Any,
    *,
    listing_ids: Sequence[str],
    since: datetime,
    limit: int,
) -> int:
    query = (
        client.table("market_price_observations")
        .select("id")
        .lt("observed_at", since.isoformat())
        .limit(limit)
    )
    if listing_ids:
        query = query.in_("saved_listing_id", list(listing_ids))
    rows = query.execute().data or []
    return len(rows)


def _index_observations_by_report_date(
    rows: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    indexed: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        report_id = str(row.get("pricing_report_id") or "")
        stay_date = str(row.get("stay_date") or "")
        if not report_id or not stay_date:
            continue
        indexed.setdefault(report_id, {})[stay_date] = row
    return indexed


def _validate_report_ingestion(
    *,
    reports: Sequence[Dict[str, Any]],
    observations: Sequence[Dict[str, Any]],
    min_new_observations: int,
    price_tolerance: float,
    issues: List[QualityIssue],
) -> Dict[str, Any]:
    observations_by_report_date = _index_observations_by_report_date(observations)
    missing_dates_total = 0
    extra_dates_total = 0
    price_mismatch_total = 0
    calendar_date_total = 0

    if not reports:
        _add_issue(
            issues,
            "error",
            "no_nightly_reports",
            "No completed nightly reports were found in the requested window.",
        )

    if len(observations) < min_new_observations:
        _add_issue(
            issues,
            "error",
            "too_few_new_observations",
            "New market observations are below the minimum required for retraining.",
            observed=len(observations),
            minimum=min_new_observations,
        )

    for report in reports:
        report_id = str(report.get("id") or "")
        calendar = report.get("result_calendar") or []
        if not isinstance(calendar, list) or not calendar:
            _add_issue(
                issues,
                "error",
                "empty_result_calendar",
                "Nightly report has no result_calendar rows.",
                report_id=report_id,
            )
            continue

        calendar_dates = {
            str(day.get("date"))
            for day in calendar
            if isinstance(day, dict) and day.get("date")
        }
        calendar_date_total += len(calendar_dates)
        observed_by_date = observations_by_report_date.get(report_id, {})
        observed_dates = set(observed_by_date.keys())

        missing_dates = sorted(calendar_dates - observed_dates)
        extra_dates = sorted(observed_dates - calendar_dates)
        missing_dates_total += len(missing_dates)
        extra_dates_total += len(extra_dates)

        if missing_dates:
            _add_issue(
                issues,
                "error",
                "missing_observation_dates",
                "Some result_calendar dates were not ingested into market_price_observations.",
                report_id=report_id,
                missing_dates=missing_dates[:20],
                missing_count=len(missing_dates),
            )
        if extra_dates:
            _add_issue(
                issues,
                "warning",
                "extra_observation_dates",
                "market_price_observations contains dates that are not in the report calendar.",
                report_id=report_id,
                extra_dates=extra_dates[:20],
                extra_count=len(extra_dates),
            )

        calendar_by_date = {
            str(day.get("date")): day
            for day in calendar
            if isinstance(day, dict) and day.get("date")
        }
        for stay_date in sorted(calendar_dates & observed_dates):
            day = calendar_by_date[stay_date]
            row = observed_by_date[stay_date]
            for calendar_key, observation_key in CALENDAR_TO_OBSERVATION_PRICE_FIELDS:
                expected = _calendar_price(day, calendar_key)
                actual = _observation_price(row, observation_key)
                if expected is None and actual is None:
                    continue
                if expected is None or actual is None or abs(expected - actual) > price_tolerance:
                    price_mismatch_total += 1
                    _add_issue(
                        issues,
                        "error",
                        "price_mismatch",
                        "Calendar price and ingested observation price differ.",
                        report_id=report_id,
                        stay_date=stay_date,
                        calendar_field=calendar_key,
                        observation_field=observation_key,
                        expected=expected,
                        actual=actual,
                    )

    return {
        "calendar_date_count": calendar_date_total,
        "missing_observation_date_count": missing_dates_total,
        "extra_observation_date_count": extra_dates_total,
        "price_mismatch_count": price_mismatch_total,
    }


def _validate_raw_observations(
    observations: Sequence[Dict[str, Any]],
    *,
    issues: List[QualityIssue],
) -> Dict[str, Any]:
    missing_required = {column: 0 for column in RAW_REQUIRED_COLUMNS}
    no_positive_price = 0
    invalid_coordinates = 0
    nonpositive_comps = 0

    for row in observations:
        for column in RAW_REQUIRED_COLUMNS:
            if row.get(column) in (None, ""):
                missing_required[column] += 1

        if not any(_observation_price(row, column) is not None for column in PRICE_SOURCE_COLUMNS):
            no_positive_price += 1

        lat = _safe_float(row.get("target_lat"))
        lng = _safe_float(row.get("target_lng"))
        if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            invalid_coordinates += 1

        comps_used = _safe_float(row.get("comps_used"))
        if comps_used is None or comps_used <= 0:
            nonpositive_comps += 1

    for column, count in missing_required.items():
        if count:
            _add_issue(
                issues,
                "error",
                "missing_raw_required_column",
                "Raw observation rows are missing a required ML ingestion field.",
                column=column,
                count=count,
            )

    if no_positive_price:
        _add_issue(
            issues,
            "error",
            "missing_training_target_price",
            "Raw observation rows have no positive price source for the ML target.",
            count=no_positive_price,
        )
    if invalid_coordinates:
        _add_issue(
            issues,
            "warning",
            "invalid_or_missing_coordinates",
            "Raw observation rows have missing or invalid coordinates; training will fall back to saved listing coordinates when possible.",
            count=invalid_coordinates,
        )
    if nonpositive_comps:
        _add_issue(
            issues,
            "warning",
            "missing_comparable_count",
            "Raw observation rows have missing or non-positive comps_used.",
            count=nonpositive_comps,
        )

    return {
        "raw_missing_required": missing_required,
        "raw_no_positive_price_count": no_positive_price,
        "raw_invalid_coordinate_count": invalid_coordinates,
        "raw_nonpositive_comps_count": nonpositive_comps,
    }


def _null_rate(df: pd.DataFrame, column: str) -> float:
    if df.empty or column not in df.columns:
        return 1.0
    return float(df[column].isna().mean())


def _validate_training_frame(
    training_df: pd.DataFrame,
    *,
    min_training_rows: int,
    max_missing_feature_rate: float,
    issues: List[QualityIssue],
) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {
        "training_row_count": int(len(training_df)),
        "critical_column_null_rates": {},
    }

    if len(training_df) < min_training_rows:
        _add_issue(
            issues,
            "error",
            "too_few_training_rows",
            "Training dataset is too small for a reliable retrain.",
            observed=int(len(training_df)),
            minimum=min_training_rows,
        )

    for column in CRITICAL_TRAINING_COLUMNS:
        rate = _null_rate(training_df, column)
        metrics["critical_column_null_rates"][column] = round(rate, 6)
        if column not in training_df.columns:
            _add_issue(
                issues,
                "error",
                "missing_training_column",
                "Training dataset is missing a critical column.",
                column=column,
            )
        elif rate > max_missing_feature_rate:
            _add_issue(
                issues,
                "error",
                "training_column_missing_rate_too_high",
                "A critical training column has too many missing values.",
                column=column,
                missing_rate=rate,
                max_missing_rate=max_missing_feature_rate,
            )

    if TARGET_COLUMN_NAME in training_df.columns:
        target = pd.to_numeric(training_df[TARGET_COLUMN_NAME], errors="coerce")
        nonpositive = int((target <= 0).sum())
        metrics["target_price_min"] = float(target.min()) if target.notna().any() else None
        metrics["target_price_median"] = float(target.median()) if target.notna().any() else None
        metrics["target_price_max"] = float(target.max()) if target.notna().any() else None
        if nonpositive:
            _add_issue(
                issues,
                "error",
                "nonpositive_training_target",
                "Training rows include non-positive target prices.",
                count=nonpositive,
            )

    for column in ("bedrooms", "baths", "accommodates", "beds"):
        if column in training_df.columns:
            values = pd.to_numeric(training_df[column], errors="coerce").fillna(0.0)
            zero_rate = float((values <= 0).mean())
            metrics[f"{column}_zero_rate"] = round(zero_rate, 6)
            if column in {"accommodates", "bedrooms"} and zero_rate > max_missing_feature_rate:
                _add_issue(
                    issues,
                    "warning",
                    "listing_size_feature_zero_rate_high",
                    "A listing size feature is frequently zero; check saved listing input_attributes.",
                    column=column,
                    zero_rate=zero_rate,
                    max_rate=max_missing_feature_rate,
                )

    return metrics


def _validate_feature_matrix(
    training_df: pd.DataFrame,
    *,
    issues: List[QualityIssue],
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    metrics: Dict[str, Any] = {}
    try:
        matrix = build_feature_matrix_df(training_df)
    except Exception as exc:
        _add_issue(
            issues,
            "error",
            "feature_matrix_build_failed",
            "ML feature matrix could not be built from the current training dataset.",
            error=str(exc),
        )
        return pd.DataFrame(), metrics

    feature_columns = [
        column
        for column in matrix.columns
        if column != TARGET_COLUMN_NAME and not column.startswith("debug_")
    ]
    metrics["feature_row_count"] = int(len(matrix))
    metrics["feature_column_count"] = int(len(feature_columns))
    metrics["numeric_feature_count"] = len(NUMERIC_FEATURES)
    metrics["categorical_feature_prefix"] = f"{CATEGORICAL_FEATURE}_"

    missing_numeric_features = [column for column in NUMERIC_FEATURES if column not in matrix.columns]
    if missing_numeric_features:
        _add_issue(
            issues,
            "error",
            "missing_numeric_features",
            "The feature matrix is missing required numeric model features.",
            missing_features=missing_numeric_features,
        )

    numeric_matrix = matrix[feature_columns].apply(pd.to_numeric, errors="coerce")
    nan_columns = [
        column
        for column in feature_columns
        if bool(numeric_matrix[column].isna().any())
    ]
    if nan_columns:
        _add_issue(
            issues,
            "error",
            "feature_matrix_contains_nan",
            "The feature matrix contains NaN values.",
            columns=nan_columns[:30],
            count=len(nan_columns),
        )

    finite_mask = numeric_matrix.map(
        lambda value: math.isfinite(float(value)) if pd.notna(value) else False
    )
    nonfinite_columns = [
        column
        for column in feature_columns
        if not bool(finite_mask[column].all())
    ]
    if nonfinite_columns:
        _add_issue(
            issues,
            "error",
            "feature_matrix_contains_nonfinite",
            "The feature matrix contains non-finite values.",
            columns=nonfinite_columns[:30],
            count=len(nonfinite_columns),
        )

    property_type_columns = [
        column for column in feature_columns if column.startswith(f"{CATEGORICAL_FEATURE}_")
    ]
    metrics["property_type_feature_count"] = len(property_type_columns)
    if not property_type_columns:
        _add_issue(
            issues,
            "warning",
            "no_property_type_feature",
            "No property_type one-hot feature was produced; all rows may be missing property type.",
        )

    return matrix, metrics


def run_quality_checks(
    *,
    report_ids: Sequence[str],
    listing_ids: Sequence[str],
    since_hours: int,
    limit: int,
    training_scope: str,
    min_new_observations: int,
    min_training_rows: int,
    max_missing_feature_rate: float,
    price_tolerance: float,
    wait_ready: bool = False,
    wait_timeout_seconds: int = 3600,
    poll_seconds: int = 15,
) -> QualityReport:
    training_scope = normalize_training_scope(training_scope)
    since = _utc_now() - timedelta(hours=since_hours)
    client = get_client()
    issues: List[QualityIssue] = []

    if wait_ready and report_ids:
        _wait_for_reports_ready(
            client,
            report_ids,
            timeout_seconds=wait_timeout_seconds,
            poll_seconds=poll_seconds,
        )

    reports = _fetch_nightly_reports(
        client,
        report_ids=report_ids,
        listing_ids=listing_ids,
        since=since,
        limit=limit,
    )
    checked_report_ids = [str(report.get("id")) for report in reports if report.get("id")]
    report_listing_ids = [
        str(report.get("listing_id"))
        for report in reports
        if report.get("listing_id")
    ]
    scoped_listing_ids = sorted(set(list(listing_ids) + report_listing_ids))

    observations = _fetch_observations_for_reports(client, checked_report_ids)
    baseline_count = _fetch_baseline_observation_count(
        client,
        listing_ids=scoped_listing_ids,
        since=since,
        limit=limit,
    )

    metrics: Dict[str, Any] = {}
    metrics.update(
        _validate_report_ingestion(
            reports=reports,
            observations=observations,
            min_new_observations=min_new_observations,
            price_tolerance=price_tolerance,
            issues=issues,
        )
    )
    metrics.update(_validate_raw_observations(observations, issues=issues))

    training_df = pd.DataFrame()
    if scoped_listing_ids:
        frames: List[pd.DataFrame] = []
        ids_for_training = scoped_listing_ids if training_scope == "listing_local" else [scoped_listing_ids[0]]
        for listing_id in ids_for_training:
            frames.append(
                fetch_training_dataset(
                    client,
                    saved_listing_id=listing_id,
                    limit=limit,
                    training_scope=training_scope,
                )
            )
        training_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    else:
        _add_issue(
            issues,
            "error",
            "no_listing_scope",
            "No listing id could be resolved for training dataset validation.",
        )

    metrics.update(
        _validate_training_frame(
            training_df,
            min_training_rows=min_training_rows,
            max_missing_feature_rate=max_missing_feature_rate,
            issues=issues,
        )
    )
    feature_matrix, feature_metrics = _validate_feature_matrix(training_df, issues=issues)
    metrics.update(feature_metrics)

    status = "fail" if any(issue.severity == "error" for issue in issues) else "pass"
    return QualityReport(
        generated_at=_utc_now().isoformat(),
        status=status,
        training_scope=training_scope,
        since_hours=int(since_hours),
        checked_report_ids=checked_report_ids,
        listing_ids=scoped_listing_ids,
        new_observation_count=int(len(observations)),
        training_row_count=int(len(training_df)),
        feature_row_count=int(len(feature_matrix)),
        feature_column_count=int(metrics.get("feature_column_count") or 0),
        baseline_row_count=int(baseline_count),
        issues=issues,
        metrics=metrics,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate newly ingested nightly observations and the ML training "
            "feature matrix before retraining."
        )
    )
    parser.add_argument("--report-id", action="append", default=[], help="Pricing report id to check.")
    parser.add_argument("--listing-id", action="append", default=[], help="Saved listing id scope.")
    parser.add_argument("--since-hours", type=int, default=30, help="Lookback window for completed nightly reports.")
    parser.add_argument("--limit", type=int, default=5000, help="Maximum rows to read per validation query.")
    parser.add_argument(
        "--training-scope",
        choices=["global", "listing_local"],
        default=get_default_training_scope(),
        help="Training dataset scope to validate.",
    )
    parser.add_argument("--min-new-observations", type=int, default=1)
    parser.add_argument("--min-training-rows", type=int, default=2)
    parser.add_argument("--max-missing-feature-rate", type=float, default=0.05)
    parser.add_argument("--price-tolerance", type=float, default=0.01)
    parser.add_argument("--wait-ready", action="store_true", help="Wait until explicit --report-id reports are ready before checking.")
    parser.add_argument("--wait-timeout-seconds", type=int, default=3600)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument(
        "--output",
        default=str(REPORTS_DIR / "data_quality_latest.json"),
        help="JSON report output path.",
    )
    parser.add_argument(
        "--issues-csv",
        default=str(REPORTS_DIR / "data_quality_issues_latest.csv"),
        help="CSV output path for quality issues.",
    )
    return parser.parse_args()


def _write_issues_csv(path: Path, issues: Sequence[QualityIssue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["severity", "code", "message", "context_json"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for issue in issues:
            writer.writerow(
                {
                    "severity": issue.severity,
                    "code": issue.code,
                    "message": issue.message,
                    "context_json": json.dumps(issue.context, ensure_ascii=False),
                }
            )


def main() -> None:
    args = parse_args()
    report = run_quality_checks(
        report_ids=args.report_id,
        listing_ids=args.listing_id,
        since_hours=args.since_hours,
        limit=args.limit,
        training_scope=args.training_scope,
        min_new_observations=args.min_new_observations,
        min_training_rows=args.min_training_rows,
        max_missing_feature_rate=args.max_missing_feature_rate,
        price_tolerance=args.price_tolerance,
        wait_ready=args.wait_ready,
        wait_timeout_seconds=args.wait_timeout_seconds,
        poll_seconds=args.poll_seconds,
    )

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    issues_csv_path = Path(args.issues_csv)
    if not issues_csv_path.is_absolute():
        issues_csv_path = PROJECT_ROOT / issues_csv_path
    _write_issues_csv(issues_csv_path, report.issues)

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[ML Data Quality] Wrote JSON report to {output_path}")
    print(f"[ML Data Quality] Wrote issues CSV to {issues_csv_path}")
    if report.status != "pass":
        sys.exit(1)


if __name__ == "__main__":
    main()
