from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_REPORTS_DIR = PROJECT_ROOT / "ml_sidecar" / "reports"
DEFAULT_OUTPUT = DEFAULT_REPORTS_DIR / "nightly_quality_report.html"
SNAPSHOT_PREFIX = "nightly_quality_snapshot_"
OBSERVATION_COLUMNS = (
    "saved_listing_id",
    "observed_at",
    "stay_date",
    "days_until_stay",
    "input_mode",
    "input_address",
    "input_listing_url",
    "listing_property_type",
    "listing_bedrooms",
    "listing_baths",
    "listing_accommodates",
    "listing_beds",
    "country_code",
    "listing_timezone",
    "target_lat",
    "target_lng",
    "amenities",
    "base_price",
    "base_daily_price",
    "price_after_time_adjustment",
    "effective_daily_price_refundable",
    "effective_daily_price_non_refundable",
    "comps_used",
    "is_weekend",
    "flags",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _read_csv_rows(path: Path, *, limit: int | None = None) -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append({str(k): "" if v is None else str(v) for k, v in row.items() if k is not None})
            if limit is not None and len(rows) >= limit:
                break
    return rows


def _csv_header(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            return [str(value) for value in next(reader)]
        except StopIteration:
            return []


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _has_value(value: Any) -> bool:
    if value in (None, ""):
        return False
    if isinstance(value, list) and not value:
        return False
    if isinstance(value, dict) and not value:
        return False
    return True


def _is_positive_price(row: dict[str, Any]) -> bool:
    for column in (
        "effective_daily_price_refundable",
        "effective_daily_price_non_refundable",
        "base_daily_price",
        "base_price",
    ):
        value = _safe_float(row.get(column))
        if value is not None and value > 0:
            return True
    return False


def _batched(values: list[str], size: int = 50):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _format_dt(value: Any) -> str:
    if not value:
        return "n/a"
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_delta(current: int | float | None, previous: int | float | None) -> str:
    if current is None or previous is None:
        return "n/a"
    delta = current - previous
    if isinstance(current, float) or isinstance(previous, float):
        return f"{delta:+.4g}"
    return f"{int(delta):+d}"


def _json_leaf_keys(value: Any, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            keys.update(_json_leaf_keys(child, child_prefix))
    elif isinstance(value, list):
        keys.add(prefix)
    elif prefix:
        keys.add(prefix)
    return keys


def _profile_training_matrix(path: Path) -> dict[str, Any]:
    header = _csv_header(path)
    if not header:
        return {
            "path": str(path),
            "exists": path.exists(),
            "row_count": 0,
            "columns": [],
            "column_count": 0,
            "missing_counts": {},
            "zero_counts": {},
            "numeric_min": {},
            "numeric_max": {},
        }

    missing_counts = Counter({column: 0 for column in header})
    zero_counts = Counter({column: 0 for column in header})
    numeric_min: dict[str, float] = {}
    numeric_max: dict[str, float] = {}
    row_count = 0

    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_count += 1
            for column in header:
                raw = row.get(column)
                if raw in (None, ""):
                    missing_counts[column] += 1
                    continue
                numeric = _safe_float(raw)
                if numeric is not None:
                    if numeric == 0:
                        zero_counts[column] += 1
                    numeric_min[column] = numeric if column not in numeric_min else min(numeric_min[column], numeric)
                    numeric_max[column] = numeric if column not in numeric_max else max(numeric_max[column], numeric)

    return {
        "path": str(path),
        "exists": True,
        "row_count": row_count,
        "columns": header,
        "column_count": len(header),
        "missing_counts": dict(missing_counts),
        "zero_counts": dict(zero_counts),
        "numeric_min": numeric_min,
        "numeric_max": numeric_max,
    }


def _profile_training_by_listing(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}

    by_listing: dict[str, dict[str, Any]] = {}
    watched_columns = ("lat", "lng", "comps_used", "observed_market_price")

    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if "debug_saved_listing_id" not in (reader.fieldnames or []):
            return {}

        for row in reader:
            listing_id = row.get("debug_saved_listing_id") or "unknown"
            stats = by_listing.setdefault(
                listing_id,
                {
                    "training_row_count": 0,
                    "zero_counts": {column: 0 for column in watched_columns},
                    "missing_counts": {column: 0 for column in watched_columns},
                    "min_price": None,
                    "max_price": None,
                    "status": "pass",
                },
            )
            stats["training_row_count"] += 1
            for column in watched_columns:
                value = row.get(column)
                if value in (None, ""):
                    stats["missing_counts"][column] += 1
                    continue
                numeric = _safe_float(value)
                if numeric is not None and numeric == 0:
                    stats["zero_counts"][column] += 1
                if column == "observed_market_price" and numeric is not None:
                    current_min = stats["min_price"]
                    current_max = stats["max_price"]
                    stats["min_price"] = numeric if current_min is None else min(current_min, numeric)
                    stats["max_price"] = numeric if current_max is None else max(current_max, numeric)

    for stats in by_listing.values():
        rows = int(stats["training_row_count"] or 0)
        if not rows:
            stats["status"] = "error"
            continue
        lat_bad = stats["missing_counts"]["lat"] + stats["zero_counts"]["lat"]
        lng_bad = stats["missing_counts"]["lng"] + stats["zero_counts"]["lng"]
        comps_bad = stats["missing_counts"]["comps_used"] + stats["zero_counts"]["comps_used"]
        price_bad = stats["missing_counts"]["observed_market_price"] + stats["zero_counts"]["observed_market_price"]
        if price_bad:
            stats["status"] = "error"
        elif lat_bad == rows or lng_bad == rows or comps_bad == rows:
            stats["status"] = "warning"

    return by_listing


def _manifest_summary(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    if not data:
        return {}
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    predictions = data.get("predictions") if isinstance(data.get("predictions"), list) else []
    return {
        "file": path.name,
        "mtime": path.stat().st_mtime,
        "generated_at": data.get("generated_at"),
        "listing_id": data.get("listing_id"),
        "listing_name": data.get("listing_name"),
        "training_scope": data.get("training_scope"),
        "trained_now": data.get("trained_now"),
        "model_mode": data.get("model_mode"),
        "n_samples": data.get("n_samples"),
        "horizon": data.get("horizon"),
        "prediction_count": len(predictions),
        "mae": metrics.get("mae"),
        "mape": metrics.get("mape"),
        "r2": metrics.get("r2"),
        "confidence": metrics.get("model_confidence_band"),
    }


def _latest_manifests(reports_dir: Path, *, limit: int = 8) -> list[dict[str, Any]]:
    paths = sorted(
        reports_dir.glob("manifest_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return [summary for path in paths[:limit] if (summary := _manifest_summary(path))]


def _build_snapshot(reports_dir: Path) -> dict[str, Any]:
    dq_path = reports_dir / "data_quality_latest.json"
    issues_path = reports_dir / "data_quality_issues_latest.csv"
    metrics_path = reports_dir / "metrics_latest.csv"
    training_path = reports_dir / "training_matrix.csv"

    data_quality = _read_json(dq_path)
    issues = _read_csv_rows(issues_path)
    metrics_rows = _read_csv_rows(metrics_path, limit=1)
    training_profile = _profile_training_matrix(training_path)
    training_by_listing = _profile_training_by_listing(training_path)
    manifests = _latest_manifests(reports_dir)

    dq_metrics = data_quality.get("metrics") if isinstance(data_quality.get("metrics"), dict) else {}
    return {
        "snapshot_version": 1,
        "generated_at": _utc_now().isoformat(),
        "reports_dir": str(reports_dir),
        "source_files": {
            "data_quality": dq_path.name if dq_path.exists() else None,
            "issues": issues_path.name if issues_path.exists() else None,
            "metrics": metrics_path.name if metrics_path.exists() else None,
            "training_matrix": training_path.name if training_path.exists() else None,
        },
        "data_quality": data_quality,
        "issues": issues,
        "metrics_latest": metrics_rows[0] if metrics_rows else {},
        "training_matrix": training_profile,
        "training_by_listing": training_by_listing,
        "manifests": manifests,
        "field_sets": {
            "data_quality_metric_fields": sorted(_json_leaf_keys(dq_metrics)),
            "training_matrix_columns": training_profile.get("columns", []),
            "metrics_latest_columns": _csv_header(metrics_path),
            "issue_codes": sorted({row.get("code", "") for row in issues if row.get("code")}),
        },
    }


def _extract_airbnb_room_id(value: Any) -> str | None:
    text = str(value or "")
    marker = "/rooms/"
    if marker not in text:
        return None
    tail = text.split(marker, 1)[1]
    digits = []
    for char in tail:
        if char.isdigit():
            digits.append(char)
        else:
            break
    return "".join(digits) or None


def _normalize_report_relation(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, dict) else None


def _fetch_supabase_snapshot(limit: int = 2000) -> dict[str, Any]:
    from ml_sidecar.supabase_client import get_client

    client = get_client()
    generated_at = _utc_now().isoformat()

    listing_rows = (
        client.table("saved_listings")
        .select("id,name,input_address,input_attributes,target_lat,target_lng,listing_timezone,created_at")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )
    listings_by_id = {str(row.get("id")): row for row in listing_rows if row.get("id")}

    link_rows = (
        client.table("listing_reports")
        .select(
            "saved_listing_id,trigger,created_at,"
            "pricing_reports:pricing_report_id("
            "id,listing_id,status,job_lane,target_env,report_type,created_at,completed_at,"
            "market_captured_at,error_message,result_calendar,result_summary,input_listing_url,input_address)"
        )
        .eq("trigger", "scheduled")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )

    latest_any_by_listing: dict[str, dict[str, Any]] = {}
    ready_by_listing: dict[str, list[dict[str, Any]]] = {}
    all_report_ids: list[str] = []
    for link in link_rows:
        listing_id = str(link.get("saved_listing_id") or "")
        report = _normalize_report_relation(link.get("pricing_reports"))
        if not listing_id or not report:
            continue
        report = dict(report)
        report["_link_created_at"] = link.get("created_at")
        report["_saved_listing_id"] = listing_id
        report_id = str(report.get("id") or "")
        if report_id:
            all_report_ids.append(report_id)
        latest_any_by_listing.setdefault(listing_id, report)
        if (
            report.get("status") == "ready"
            and (report.get("report_type") or "live_analysis") != "forecast_snapshot"
        ):
            ready_by_listing.setdefault(listing_id, []).append(report)

    latest_ready_by_listing: dict[str, dict[str, Any]] = {}
    previous_ready_by_listing: dict[str, dict[str, Any]] = {}
    checked_report_ids: list[str] = []
    previous_report_ids: list[str] = []
    for listing_id, reports in ready_by_listing.items():
        reports = sorted(
            reports,
            key=lambda row: str(row.get("completed_at") or row.get("created_at") or ""),
            reverse=True,
        )
        if reports:
            latest_ready_by_listing[listing_id] = reports[0]
            checked_report_ids.append(str(reports[0].get("id")))
        if len(reports) > 1:
            previous_ready_by_listing[listing_id] = reports[1]
            previous_report_ids.append(str(reports[1].get("id")))

    observation_report_ids = sorted(set(checked_report_ids + previous_report_ids))
    observations: list[dict[str, Any]] = []
    if observation_report_ids:
        columns = "pricing_report_id," + ",".join(OBSERVATION_COLUMNS)
        for batch in _batched(observation_report_ids):
            observations.extend(
                client.table("market_price_observations")
                .select(columns)
                .in_("pricing_report_id", batch)
                .execute()
                .data
                or []
            )

    obs_by_report: dict[str, list[dict[str, Any]]] = {}
    for row in observations:
        report_id = str(row.get("pricing_report_id") or "")
        obs_by_report.setdefault(report_id, []).append(row)

    def observation_profile(rows: list[dict[str, Any]]) -> dict[str, Any]:
        row_count = len(rows)
        present_counts = {column: 0 for column in OBSERVATION_COLUMNS}
        zero_counts = {column: 0 for column in OBSERVATION_COLUMNS}
        for row in rows:
            for column in OBSERVATION_COLUMNS:
                value = row.get(column)
                if _has_value(value):
                    present_counts[column] += 1
                numeric = _safe_float(value)
                if numeric is not None and numeric == 0:
                    zero_counts[column] += 1
        return {
            "row_count": row_count,
            "present_counts": present_counts,
            "zero_counts": zero_counts,
            "available_fields": sorted([column for column, count in present_counts.items() if count > 0]),
        }

    def calendar_dates(report: dict[str, Any]) -> set[str]:
        calendar = report.get("result_calendar") or []
        if not isinstance(calendar, list):
            return set()
        return {
            str(day.get("date"))
            for day in calendar
            if isinstance(day, dict) and day.get("date")
        }

    listing_quality: dict[str, dict[str, Any]] = {}
    issue_rows: list[dict[str, str]] = []
    total_new_observations = 0
    total_missing_dates = 0
    total_price_mismatches = 0
    total_bad_coords = 0
    total_bad_comps = 0
    total_no_price = 0

    eligible_listing_ids: list[str] = []
    for listing_id, listing in listings_by_id.items():
        attrs = listing.get("input_attributes") if isinstance(listing.get("input_attributes"), dict) else {}
        latest = latest_ready_by_listing.get(listing_id)
        previous = previous_ready_by_listing.get(listing_id)
        latest_any = latest_any_by_listing.get(listing_id)
        listing_url = (
            attrs.get("listingUrl")
            or attrs.get("listing_url")
            or (latest_any or {}).get("input_listing_url")
            or (latest or {}).get("input_listing_url")
        )
        is_eligible = bool((isinstance(listing_url, str) and "/rooms/" in listing_url) or latest_any)
        if not is_eligible:
            continue
        eligible_listing_ids.append(listing_id)

        latest_rows = (obs_by_report.get(str(latest.get("id"))) or []) if latest else []
        previous_rows = (obs_by_report.get(str(previous.get("id"))) or []) if previous else []
        latest_profile = observation_profile(latest_rows)
        previous_profile = observation_profile(previous_rows)
        latest_dates = {str(row.get("stay_date")) for row in latest_rows if row.get("stay_date")}
        cal_dates = calendar_dates(latest or {})
        missing_dates = sorted(cal_dates - latest_dates)
        no_price = sum(1 for row in latest_rows if not _is_positive_price(row))
        bad_coords = 0
        bad_comps = 0
        for row in latest_rows:
            lat = _safe_float(row.get("target_lat"))
            lng = _safe_float(row.get("target_lng"))
            if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
                bad_coords += 1
            comps_used = _safe_float(row.get("comps_used"))
            if comps_used is None or comps_used <= 0:
                bad_comps += 1

        disappeared_fields = sorted(
            set(previous_profile.get("available_fields") or [])
            - set(latest_profile.get("available_fields") or [])
        )
        added_fields = sorted(
            set(latest_profile.get("available_fields") or [])
            - set(previous_profile.get("available_fields") or [])
        )

        status = "pass"
        if not latest:
            status = "error" if latest_any and latest_any.get("status") == "error" else "missing"
        elif missing_dates or no_price:
            status = "error"
        elif bad_coords or bad_comps or disappeared_fields:
            status = "warning"

        total_new_observations += len(latest_rows)
        total_missing_dates += len(missing_dates)
        total_bad_coords += bad_coords
        total_bad_comps += bad_comps
        total_no_price += no_price

        listing_quality[listing_id] = {
            "listing_id": listing_id,
            "listing_name": listing.get("name"),
            "airbnb_room_id": _extract_airbnb_room_id(listing_url),
            "listing_url": listing_url,
            "eligible": is_eligible,
            "status": status,
            "latest_report_id": latest.get("id") if latest else None,
            "latest_report_status": latest.get("status") if latest else latest_any.get("status") if latest_any else None,
            "latest_report_completed_at": latest.get("completed_at") if latest else None,
            "latest_report_error": latest_any.get("error_message") if latest_any else None,
            "previous_report_id": previous.get("id") if previous else None,
            "new_observation_count": len(latest_rows),
            "previous_observation_count": len(previous_rows),
            "observation_delta": len(latest_rows) - len(previous_rows) if previous else None,
            "calendar_date_count": len(cal_dates),
            "missing_observation_date_count": len(missing_dates),
            "missing_dates_sample": missing_dates[:20],
            "bad_coordinate_count": bad_coords,
            "bad_comps_count": bad_comps,
            "no_positive_price_count": no_price,
            "available_fields": latest_profile.get("available_fields"),
            "previous_available_fields": previous_profile.get("available_fields"),
            "added_fields": added_fields,
            "disappeared_fields": disappeared_fields,
            "field_present_counts": latest_profile.get("present_counts"),
            "field_zero_counts": latest_profile.get("zero_counts"),
        }

        if status in ("error", "warning", "missing"):
            issue_rows.append(
                {
                    "severity": "error" if status in ("error", "missing") else "warning",
                    "code": f"listing_{status}",
                    "message": f"{listing.get('name') or listing_id} nightly quality is {status}",
                    "context_json": json.dumps(
                        {
                            "listing_id": listing_id,
                            "latest_report_id": listing_quality[listing_id]["latest_report_id"],
                            "latest_report_status": listing_quality[listing_id]["latest_report_status"],
                            "missing_dates": len(missing_dates),
                            "bad_coords": bad_coords,
                            "bad_comps": bad_comps,
                            "disappeared_fields": disappeared_fields,
                        },
                        ensure_ascii=False,
                    ),
                }
            )

    overall_status = "fail" if any(row["severity"] == "error" for row in issue_rows) else "pass"
    metrics = {
        "calendar_date_count": sum(int(row.get("calendar_date_count") or 0) for row in listing_quality.values()),
        "missing_observation_date_count": total_missing_dates,
        "extra_observation_date_count": 0,
        "price_mismatch_count": total_price_mismatches,
        "raw_invalid_coordinate_count": total_bad_coords,
        "raw_nonpositive_comps_count": total_bad_comps,
        "raw_no_positive_price_count": total_no_price,
        "per_listing_supabase_quality": listing_quality,
    }

    return {
        "snapshot_version": 2,
        "source": "supabase",
        "generated_at": generated_at,
        "reports_dir": str(DEFAULT_REPORTS_DIR),
        "data_quality": {
            "generated_at": generated_at,
            "status": overall_status,
            "training_scope": "supabase_nightly",
            "since_hours": None,
            "checked_report_ids": checked_report_ids,
            "listing_ids": sorted(eligible_listing_ids),
            "new_observation_count": total_new_observations,
            "training_row_count": None,
            "feature_row_count": None,
            "feature_column_count": None,
            "baseline_row_count": sum(len(obs_by_report.get(report_id, [])) for report_id in previous_report_ids),
            "issues": issue_rows,
            "metrics": metrics,
        },
        "issues": issue_rows,
        "metrics_latest": {},
        "training_matrix": {"row_count": total_new_observations, "columns": list(OBSERVATION_COLUMNS), "column_count": len(OBSERVATION_COLUMNS)},
        "training_by_listing": {},
        "supabase_listing_quality": listing_quality,
        "manifests": _latest_manifests(DEFAULT_REPORTS_DIR),
        "field_sets": {
            "data_quality_metric_fields": sorted(_json_leaf_keys(metrics)),
            "training_matrix_columns": list(OBSERVATION_COLUMNS),
            "metrics_latest_columns": [],
            "issue_codes": sorted({row.get("code", "") for row in issue_rows if row.get("code")}),
        },
    }


def _snapshot_paths(reports_dir: Path) -> list[Path]:
    return sorted(
        reports_dir.glob(f"{SNAPSHOT_PREFIX}*.json"),
        key=lambda path: path.stat().st_mtime,
    )


def _write_snapshot(reports_dir: Path, snapshot: dict[str, Any]) -> Path:
    timestamp = _utc_now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"{SNAPSHOT_PREFIX}{timestamp}.json"
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _load_previous_snapshot(reports_dir: Path, current_path: Path | None = None) -> dict[str, Any]:
    paths = _snapshot_paths(reports_dir)
    if current_path is not None:
        paths = [path for path in paths if path.resolve() != current_path.resolve()]
    if not paths:
        return {}
    return _read_json(paths[-1])


def _diff_field_set(snapshot: dict[str, Any], previous: dict[str, Any], key: str) -> dict[str, list[str]]:
    current_values = set(((snapshot.get("field_sets") or {}).get(key) or []))
    previous_values = set(((previous.get("field_sets") or {}).get(key) or []))
    return {
        "added": sorted(current_values - previous_values),
        "removed": sorted(previous_values - current_values),
        "unchanged": sorted(current_values & previous_values),
    }


def _h(text: Any) -> str:
    return escape("" if text is None else str(text))


def _badge(label: str, tone: str) -> str:
    return f'<span class="badge {tone}">{_h(label)}</span>'


def _render_kpi(label: str, value: Any, delta: str = "n/a") -> str:
    return (
        '<div class="kpi">'
        f'<div class="kpi-label">{_h(label)}</div>'
        f'<div class="kpi-value">{_h(value)}</div>'
        f'<div class="kpi-delta">vs previous: {_h(delta)}</div>'
        '</div>'
    )


def _render_issue_table(issues: list[dict[str, str]]) -> str:
    if not issues:
        return '<p class="empty">No data quality issues were reported.</p>'
    rows = []
    for issue in issues:
        severity = issue.get("severity", "")
        tone = "bad" if severity == "error" else "warn" if severity == "warning" else "neutral"
        rows.append(
            "<tr>"
            f"<td>{_badge(severity or 'unknown', tone)}</td>"
            f"<td><code>{_h(issue.get('code'))}</code></td>"
            f"<td>{_h(issue.get('message'))}</td>"
            f"<td><code>{_h(issue.get('context_json'))}</code></td>"
            "</tr>"
        )
    return (
        '<table><thead><tr><th>Severity</th><th>Code</th><th>Message</th><th>Context</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_problem_listings(snapshot: dict[str, Any]) -> str:
    supabase_quality = snapshot.get("supabase_listing_quality")
    if isinstance(supabase_quality, dict) and supabase_quality:
        rows = []
        for listing_id, row in sorted(supabase_quality.items(), key=lambda item: str((item[1] or {}).get("listing_name") or item[0])):
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "unknown")
            if status == "pass":
                continue
            tone = "bad" if status in ("error", "missing") else "warn" if status == "warning" else "good"
            reasons: list[str] = []
            causes: list[str] = []
            impacts: list[str] = []
            if not row.get("latest_report_id"):
                reasons.append("no ready nightly report found")
                causes.append("scheduler did not create a ready report, or worker failed before ready")
                impacts.append("dashboard has no fresh nightly data for this listing")
            if row.get("latest_report_status") == "error":
                reasons.append("latest scheduled nightly ended in error")
                causes.append(str(row.get("latest_report_error") or "see pricing_reports.error_message"))
                impacts.append("new observations were not generated")
            if int(row.get("missing_observation_date_count") or 0):
                reasons.append(f"{row.get('missing_observation_date_count')} calendar dates are missing observations")
                causes.append("result_calendar did not fully ingest into market_price_observations")
                impacts.append("nightly coverage is incomplete")
            if int(row.get("bad_coordinate_count") or 0):
                reasons.append(f"{row.get('bad_coordinate_count')} observations have missing or invalid coordinates")
                causes.append("saved listing target_lat/target_lng or observation target_lat/target_lng is missing")
                impacts.append("model location signal is weak")
            if int(row.get("bad_comps_count") or 0):
                reasons.append(f"{row.get('bad_comps_count')} observations have comps_used missing or <= 0")
                causes.append("calendar day compsUsed did not reach market_price_observations")
                impacts.append("model loses comparable support signal")
            if int(row.get("no_positive_price_count") or 0):
                reasons.append(f"{row.get('no_positive_price_count')} observations have no positive price")
                causes.append("price source fields are empty or non-positive")
                impacts.append("training target is invalid for those rows")
            disappeared = row.get("disappeared_fields") if isinstance(row.get("disappeared_fields"), list) else []
            if disappeared:
                reasons.append("fields disappeared vs previous nightly: " + ", ".join(map(str, disappeared[:8])))
                causes.append("latest observation rows stopped populating fields that existed in the previous nightly")
                impacts.append("feature consistency changed between nightly runs")
            if not reasons:
                reasons.append("no listing-specific issue detected")
                causes.append("n/a")
                impacts.append("n/a")

            rows.append(
                "<tr>"
                f"<td>{_badge(status, tone)}</td>"
                f"<td>{_h(row.get('listing_name') or '')}<br><code>{_h(listing_id)}</code><br><span class=\"muted\">room {_h(row.get('airbnb_room_id') or 'n/a')}</span></td>"
                f"<td><code>{_h(row.get('latest_report_id') or 'n/a')}</code><br><span class=\"muted\">{_h(_format_dt(row.get('latest_report_completed_at')))}</span></td>"
                f"<td><code>{_h(row.get('previous_report_id') or 'n/a')}</code></td>"
                f"<td>{_h(row.get('new_observation_count'))}</td>"
                f"<td>{_h(row.get('previous_observation_count'))}</td>"
                f"<td>{_h(row.get('observation_delta', 'n/a'))}</td>"
                f"<td>{'<br>'.join(_h(reason) for reason in reasons)}</td>"
                f"<td>{'<br>'.join(_h(cause) for cause in dict.fromkeys(causes))}</td>"
                f"<td>{'<br>'.join(_h(impact) for impact in dict.fromkeys(impacts))}</td>"
                f"<td>{_h(row.get('bad_coordinate_count'))}</td>"
                f"<td>{_h(row.get('bad_comps_count'))}</td>"
                f"<td>{_h(row.get('missing_observation_date_count'))}</td>"
                f"<td>{_h(', '.join(map(str, disappeared[:10])) if disappeared else 'none')}</td>"
                "</tr>"
            )

        if not rows:
            return '<p class="empty">No problem listings detected.</p>'

        return (
            '<table><thead><tr><th>Status</th><th>Listing</th><th>Latest nightly</th><th>Previous nightly</th>'
            '<th>New obs</th><th>Prev obs</th><th>Delta</th><th>Why flagged</th><th>Likely cause</th><th>Impact</th>'
            '<th>Bad coords</th><th>Bad comps</th><th>Missing dates</th><th>Disappeared fields</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    dq = snapshot.get("data_quality") if isinstance(snapshot.get("data_quality"), dict) else {}
    metrics = dq.get("metrics") if isinstance(dq.get("metrics"), dict) else {}
    per_listing = metrics.get("per_listing_observation_quality")
    if not isinstance(per_listing, dict):
        per_listing = {}
    per_report = metrics.get("per_report_ingestion_quality")
    if not isinstance(per_report, dict):
        per_report = {}
    training_by_listing = snapshot.get("training_by_listing")
    if not isinstance(training_by_listing, dict):
        training_by_listing = {}

    report_stats_by_listing: dict[str, dict[str, int]] = {}
    for report_id, report in per_report.items():
        if not isinstance(report, dict):
            continue
        listing_id = str(report.get("listing_id") or "unknown")
        stats = report_stats_by_listing.setdefault(
            listing_id,
            {
                "reports": 0,
                "missing_dates": 0,
                "extra_dates": 0,
                "price_mismatches": 0,
            },
        )
        stats["reports"] += 1
        stats["missing_dates"] += int(report.get("missing_observation_date_count") or 0)
        stats["extra_dates"] += int(report.get("extra_observation_date_count") or 0)
        stats["price_mismatches"] += int(report.get("price_mismatch_count") or 0)

    scoped_listing_ids = {
        str(listing_id)
        for listing_id in (dq.get("listing_ids") or [])
        if str(listing_id)
    }
    training_listing_ids = set(training_by_listing)
    if not per_listing and scoped_listing_ids:
        training_listing_ids = training_listing_ids & scoped_listing_ids

    listing_ids = sorted(set(per_listing) | training_listing_ids | set(report_stats_by_listing))
    if not listing_ids:
        return (
            '<p class="empty">No per-listing quality data is available yet. '
            'Run data quality again after this update to populate per-listing issue breakdowns.</p>'
        )

    rows = []
    for listing_id in listing_ids:
        obs = per_listing.get(listing_id) if isinstance(per_listing.get(listing_id), dict) else {}
        train = training_by_listing.get(listing_id) if isinstance(training_by_listing.get(listing_id), dict) else {}
        report = report_stats_by_listing.get(listing_id, {})
        obs_count = int(obs.get("observation_count") or 0)
        train_count = int(train.get("training_row_count") or 0)
        zero_counts = train.get("zero_counts") if isinstance(train.get("zero_counts"), dict) else {}
        missing_counts = train.get("missing_counts") if isinstance(train.get("missing_counts"), dict) else {}
        invalid_coords = int(obs.get("invalid_coordinate_count") or 0)
        nonpositive_comps = int(obs.get("nonpositive_comps_count") or 0)
        no_price = int(obs.get("no_positive_price_count") or 0)
        missing_required = obs.get("missing_required") if isinstance(obs.get("missing_required"), dict) else {}
        missing_required_total = sum(int(value or 0) for value in missing_required.values())
        lat_zero = int(zero_counts.get("lat") or 0)
        lng_zero = int(zero_counts.get("lng") or 0)
        comps_zero = int(zero_counts.get("comps_used") or 0)
        price_missing = int(missing_counts.get("observed_market_price") or 0)
        price_zero = int(zero_counts.get("observed_market_price") or 0)
        missing_dates = int(report.get("missing_dates") or 0)
        price_mismatches = int(report.get("price_mismatches") or 0)
        lat_lng_all_zero = train_count > 0 and lat_zero == train_count and lng_zero == train_count
        comps_all_zero = train_count > 0 and comps_zero == train_count

        reasons: list[str] = []
        causes: list[str] = []
        impacts: list[str] = []

        if invalid_coords:
            reasons.append(f"{invalid_coords}/{obs_count} new observations have invalid or missing coordinates")
            causes.append("market_price_observations.target_lat/target_lng were not populated")
            impacts.append("location signal is weaker for this listing")
        elif lat_lng_all_zero:
            reasons.append("all training rows use lat/lng = 0")
            causes.append("coordinates were missing before the training matrix was built")
            impacts.append("model cannot learn this listing's geography")

        if nonpositive_comps:
            reasons.append(f"{nonpositive_comps}/{obs_count} new observations have comps_used missing or <= 0")
            causes.append("comparable count was not written during observation ingest")
            impacts.append("model loses a data-quality/support-count signal")
        elif comps_all_zero:
            reasons.append("all training rows have comps_used = 0")
            causes.append("comps_used is absent from the training source rows")
            impacts.append("model treats comparable support as unknown")

        if no_price:
            reasons.append(f"{no_price}/{obs_count} new observations have no positive price")
            causes.append("price source columns were empty or non-positive")
            impacts.append("rows cannot provide a reliable supervised target")

        if missing_required_total:
            reasons.append(f"{missing_required_total} required raw observation fields are missing")
            causes.append("raw ingestion omitted required metadata")
            impacts.append("rows may be dropped or misinterpreted")

        if missing_dates:
            reasons.append(f"{missing_dates} calendar dates were not ingested")
            causes.append("result_calendar to market_price_observations ingestion missed dates")
            impacts.append("nightly coverage is incomplete")

        if price_mismatches:
            reasons.append(f"{price_mismatches} calendar prices differ from observations")
            causes.append("calendar and observation price fields diverged")
            impacts.append("training target may not match the report")

        if price_missing or price_zero:
            reasons.append("training target has missing or zero prices")
            causes.append("observed_market_price was not built from a positive price source")
            impacts.append("model target quality is invalid")

        if not reasons:
            reasons.append("no listing-specific issue detected")
            causes.append("n/a")
            impacts.append("n/a")

        if no_price or missing_required_total or missing_dates or price_mismatches or price_missing or price_zero:
            tone = "bad"
            status = "error"
        elif invalid_coords or nonpositive_comps or lat_zero == train_count or lng_zero == train_count or comps_zero == train_count:
            tone = "warn"
            status = "warning"
        else:
            tone = "good"
            status = "pass"

        rows.append(
            "<tr>"
            f"<td>{_badge(status, tone)}</td>"
            f"<td><code>{_h(listing_id)}</code></td>"
            f"<td>{_h(obs_count)}</td>"
            f"<td>{_h(train_count)}</td>"
            f"<td>{'<br>'.join(_h(reason) for reason in reasons)}</td>"
            f"<td>{'<br>'.join(_h(cause) for cause in dict.fromkeys(causes))}</td>"
            f"<td>{'<br>'.join(_h(impact) for impact in dict.fromkeys(impacts))}</td>"
            f"<td>{_h(invalid_coords)}</td>"
            f"<td>{_h(nonpositive_comps)}</td>"
            f"<td>{_h(no_price)}</td>"
            f"<td>{_h(missing_required_total)}</td>"
            f"<td>{_h(missing_dates)}</td>"
            f"<td>{_h(price_mismatches)}</td>"
            f"<td>{_h(lat_zero)}/{_h(lng_zero)}</td>"
            f"<td>{_h(comps_zero)}</td>"
            f"<td>{_h(train.get('min_price', 'n/a'))} - {_h(train.get('max_price', 'n/a'))}</td>"
            "</tr>"
        )

    return (
        '<table><thead><tr><th>Status</th><th>Listing ID</th><th>New obs</th><th>Training rows</th>'
        '<th>Why flagged</th><th>Likely cause</th><th>Impact</th>'
        '<th>Bad coords</th><th>Bad comps</th><th>No price</th><th>Missing required</th>'
        '<th>Missing dates</th><th>Price mismatches</th><th>Lat/Lng zero</th><th>Comps zero</th><th>Price range</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_field_diff(title: str, diff: dict[str, list[str]]) -> str:
    def pills(values: list[str], tone: str) -> str:
        if not values:
            return '<span class="muted">None</span>'
        return "".join(f'<span class="pill {tone}">{_h(value)}</span>' for value in values)

    return (
        '<div class="field-diff">'
        f"<h3>{_h(title)}</h3>"
        '<div class="field-row"><strong>Added</strong><div>' + pills(diff["added"], "good") + "</div></div>"
        '<div class="field-row"><strong>Removed</strong><div>' + pills(diff["removed"], "bad") + "</div></div>"
        '<div class="field-row"><strong>Unchanged</strong><div>' + pills(diff["unchanged"][:80], "neutral") + "</div></div>"
        "</div>"
    )


def _render_column_health(training: dict[str, Any]) -> str:
    columns = list(training.get("columns") or [])
    if not columns:
        return '<p class="empty">No training_matrix.csv columns were found.</p>'
    row_count = int(training.get("row_count") or 0)
    missing_counts = training.get("missing_counts") or {}
    zero_counts = training.get("zero_counts") or {}
    numeric_min = training.get("numeric_min") or {}
    numeric_max = training.get("numeric_max") or {}

    rows = []
    for column in columns:
        missing = int(missing_counts.get(column) or 0)
        zeros = int(zero_counts.get(column) or 0)
        missing_rate = (missing / row_count) if row_count else 0
        zero_rate = (zeros / row_count) if row_count else 0
        tone = "bad" if missing_rate > 0.05 else "warn" if zero_rate > 0.95 and column != "observed_market_price" else "neutral"
        rows.append(
            "<tr>"
            f"<td><code>{_h(column)}</code></td>"
            f"<td>{_h(missing)}</td>"
            f"<td>{missing_rate:.1%}</td>"
            f"<td>{_h(zeros)}</td>"
            f"<td>{zero_rate:.1%}</td>"
            f"<td>{_h(numeric_min.get(column, 'n/a'))}</td>"
            f"<td>{_h(numeric_max.get(column, 'n/a'))}</td>"
            f"<td>{_badge(tone, tone)}</td>"
            "</tr>"
        )
    return (
        '<table><thead><tr><th>Column</th><th>Missing</th><th>Missing rate</th>'
        '<th>Zero</th><th>Zero rate</th><th>Min</th><th>Max</th><th>Flag</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_manifest_table(manifests: list[dict[str, Any]]) -> str:
    if not manifests:
        return '<p class="empty">No manifest_*.json files were found.</p>'
    rows = []
    for manifest in manifests:
        rows.append(
            "<tr>"
            f"<td><code>{_h(manifest.get('file'))}</code></td>"
            f"<td>{_h(_format_dt(manifest.get('generated_at')))}</td>"
            f"<td>{_h(manifest.get('listing_name') or manifest.get('listing_id'))}</td>"
            f"<td>{_h(manifest.get('training_scope'))}</td>"
            f"<td>{_h(manifest.get('n_samples'))}</td>"
            f"<td>{_h(manifest.get('prediction_count'))}</td>"
            f"<td>{_h(manifest.get('mae'))}</td>"
            f"<td>{_h(manifest.get('mape'))}</td>"
            f"<td>{_h(manifest.get('r2'))}</td>"
            f"<td>{_h(manifest.get('confidence'))}</td>"
            "</tr>"
        )
    return (
        '<table><thead><tr><th>Manifest</th><th>Generated</th><th>Listing</th><th>Scope</th>'
        '<th>Samples</th><th>Predictions</th><th>MAE</th><th>MAPE</th><th>R2</th><th>Confidence</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_html(snapshot: dict[str, Any], previous: dict[str, Any], snapshot_path: Path | None) -> str:
    dq = snapshot.get("data_quality") or {}
    prev_dq = previous.get("data_quality") or {}
    metrics = dq.get("metrics") if isinstance(dq.get("metrics"), dict) else {}
    prev_metrics = prev_dq.get("metrics") if isinstance(prev_dq.get("metrics"), dict) else {}
    training = snapshot.get("training_matrix") or {}
    prev_training = previous.get("training_matrix") or {}
    issues = snapshot.get("issues") or []
    manifests = snapshot.get("manifests") or []

    status = str(dq.get("status") or "missing")
    status_tone = "good" if status == "pass" else "bad" if status == "fail" else "neutral"
    issue_counts = Counter(str(issue.get("severity") or "unknown") for issue in issues)
    checked_report_ids = dq.get("checked_report_ids") if isinstance(dq.get("checked_report_ids"), list) else []
    listing_ids = dq.get("listing_ids") if isinstance(dq.get("listing_ids"), list) else []

    field_diffs = [
        _render_field_diff(
            "Training Matrix Columns",
            _diff_field_set(snapshot, previous, "training_matrix_columns"),
        ),
        _render_field_diff(
            "Data Quality Metric Fields",
            _diff_field_set(snapshot, previous, "data_quality_metric_fields"),
        ),
        _render_field_diff(
            "Issue Codes",
            _diff_field_set(snapshot, previous, "issue_codes"),
        ),
    ]

    kpis = [
        _render_kpi(
            "DQ Status",
            status,
            "changed" if prev_dq.get("status") and prev_dq.get("status") != status else "same",
        ),
        _render_kpi(
            "New Observations",
            dq.get("new_observation_count", "n/a"),
            _format_delta(dq.get("new_observation_count"), prev_dq.get("new_observation_count")),
        ),
        _render_kpi(
            "Calendar Dates",
            metrics.get("calendar_date_count", "n/a"),
            _format_delta(metrics.get("calendar_date_count"), prev_metrics.get("calendar_date_count")),
        ),
        _render_kpi(
            "Missing Dates",
            metrics.get("missing_observation_date_count", "n/a"),
            _format_delta(metrics.get("missing_observation_date_count"), prev_metrics.get("missing_observation_date_count")),
        ),
        _render_kpi(
            "Price Mismatches",
            metrics.get("price_mismatch_count", "n/a"),
            _format_delta(metrics.get("price_mismatch_count"), prev_metrics.get("price_mismatch_count")),
        ),
        _render_kpi(
            "Training Rows",
            training.get("row_count", dq.get("training_row_count", "n/a")),
            _format_delta(training.get("row_count"), prev_training.get("row_count")),
        ),
        _render_kpi(
            "Feature Columns",
            training.get("column_count", dq.get("feature_column_count", "n/a")),
            _format_delta(training.get("column_count"), prev_training.get("column_count")),
        ),
        _render_kpi(
            "Issues",
            len(issues),
            _format_delta(len(issues), len(previous.get("issues") or []) if previous else None),
        ),
    ]

    issue_summary = "".join(
        f'<span class="summary-chip">{_h(severity)}: {_h(count)}</span>'
        for severity, count in sorted(issue_counts.items())
    ) or '<span class="summary-chip">none</span>'

    source = str(snapshot.get("source") or "artifacts")
    if source == "supabase":
        coverage_note = (
            "This report queries Supabase directly and compares each eligible listing's latest ready scheduled nightly with its previous ready scheduled nightly."
        )
        if not checked_report_ids:
            coverage_note = (
                "No ready scheduled nightly reports were found in Supabase for eligible listings. Check the Problem Listings table for missing, running, or failed jobs."
            )
    else:
        coverage_note = (
            "This report reflects the data_quality scope. If nightly was forced for one listing, it is not a full portfolio coverage check yet."
            if checked_report_ids
            else "No checked_report_ids were found. Run data quality after nightly to populate this report."
        )

    css = """
    :root { color-scheme: light; --bg:#f6f7f9; --ink:#17202a; --muted:#64748b; --line:#dbe3ee; --panel:#ffffff; --good:#087f5b; --warn:#b7791f; --bad:#c92a2a; }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    header { padding: 28px 32px 18px; background: #111827; color: white; }
    header h1 { margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }
    header p { margin: 4px 0; color: #cbd5e1; }
    main { padding: 24px 32px 40px; max-width: 1480px; margin: 0 auto; }
    section { margin: 0 0 22px; padding: 18px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 1px 2px rgba(15,23,42,.04); }
    h2 { margin: 0 0 14px; font-size: 18px; }
    h3 { margin: 0 0 10px; font-size: 14px; color: #334155; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
    .kpi { padding: 14px; border: 1px solid var(--line); border-radius: 8px; background: #fbfdff; min-height: 100px; }
    .kpi-label { color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 700; }
    .kpi-value { margin-top: 8px; font-size: 28px; font-weight: 750; }
    .kpi-delta { margin-top: 6px; color: var(--muted); font-size: 12px; }
    .badge, .pill, .summary-chip { display: inline-flex; align-items: center; gap: 4px; border-radius: 999px; padding: 2px 8px; font-size: 12px; font-weight: 700; }
    .badge.good, .pill.good { color: var(--good); background: #e6fcf5; }
    .badge.warn, .pill.warn { color: var(--warn); background: #fff4d6; }
    .badge.bad, .pill.bad { color: var(--bad); background: #ffe3e3; }
    .badge.neutral, .pill.neutral { color: #475569; background: #eef2f7; }
    .summary-chip { background: #eef2f7; color: #334155; margin-right: 8px; }
    .meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; color: #334155; }
    .meta div { padding: 10px 12px; background: #f8fafc; border-radius: 6px; border: 1px solid var(--line); }
    table { width: 100%; border-collapse: collapse; table-layout: auto; }
    th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: #475569; font-size: 12px; text-transform: uppercase; background: #f8fafc; }
    code { font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; word-break: break-all; }
    .field-diff { margin-bottom: 14px; }
    .field-row { display: grid; grid-template-columns: 110px 1fr; gap: 10px; margin: 8px 0; align-items: start; }
    .pill { margin: 2px 4px 2px 0; font-weight: 600; }
    .muted, .empty { color: var(--muted); }
    .scroll { overflow-x: auto; }
    """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AiraHost Nightly Data Quality</title>
  <style>{css}</style>
</head>
<body>
  <header>
    <h1>AiraHost Nightly Data Quality</h1>
    <p>Generated {_h(_format_dt(snapshot.get("generated_at")))} from <code>{_h(source)}</code> source</p>
    <p>{_badge(status, status_tone)} <span class="summary-chip">reports: {_h(len(checked_report_ids))}</span><span class="summary-chip">listings: {_h(len(listing_ids))}</span>{issue_summary}</p>
  </header>
  <main>
    <section>
      <h2>Run Scope</h2>
      <div class="meta">
        <div><strong>Data quality generated</strong><br>{_h(_format_dt(dq.get("generated_at")))}</div>
        <div><strong>Training scope</strong><br>{_h(dq.get("training_scope", "n/a"))}</div>
        <div><strong>Report source</strong><br>{_h(source)}</div>
        <div><strong>Since hours</strong><br>{_h(dq.get("since_hours", "n/a"))}</div>
        <div><strong>Snapshot</strong><br><code>{_h(snapshot_path.name if snapshot_path else "not written")}</code></div>
      </div>
      <p class="muted">{_h(coverage_note)}</p>
      <p><strong>Checked report ids:</strong> <code>{_h(", ".join(map(str, checked_report_ids)) or "none")}</code></p>
      <p><strong>Listing ids:</strong> <code>{_h(", ".join(map(str, listing_ids)) or "none")}</code></p>
    </section>
    <section>
      <h2>Latest vs Previous Snapshot</h2>
      <div class="grid">{''.join(kpis)}</div>
    </section>
    <section>
      <h2>Issue Summary</h2>
      <div class="scroll">{_render_issue_table(issues)}</div>
    </section>
    <section>
      <h2>Problem Listings</h2>
      <p class="muted">Rows are grouped by saved listing ID. The first columns explain why the listing is flagged, the likely source of the issue, and the expected model impact. Numeric columns remain visible for debugging.</p>
      <div class="scroll">{_render_problem_listings(snapshot)}</div>
    </section>
    <section>
      <h2>Field Changes</h2>
      {''.join(field_diffs)}
    </section>
    <section>
      <h2>Training Matrix Column Health</h2>
      <div class="scroll">{_render_column_health(training)}</div>
    </section>
    <section>
      <h2>Recent ML Manifests</h2>
      <div class="scroll">{_render_manifest_table(manifests)}</div>
    </section>
  </main>
</body>
</html>
"""


def generate_report(
    *,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    output_path: Path = DEFAULT_OUTPUT,
    write_snapshot: bool = True,
    source: str = "artifacts",
) -> tuple[Path, Path | None]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    snapshot = _fetch_supabase_snapshot() if source == "supabase" else _build_snapshot(reports_dir)
    snapshot["source"] = source
    snapshot_path = _write_snapshot(reports_dir, snapshot) if write_snapshot else None
    previous = _load_previous_snapshot(reports_dir, snapshot_path)
    html = _render_html(snapshot, previous, snapshot_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path, snapshot_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a one-page HTML summary for nightly data quality and ML report artifacts."
    )
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument(
        "--source",
        choices=["artifacts", "supabase"],
        default="artifacts",
        help="Use local report artifacts or query Supabase nightly tables directly.",
    )
    parser.add_argument("--no-snapshot", action="store_true", help="Do not write a historical snapshot JSON.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output, snapshot = generate_report(
        reports_dir=Path(args.reports_dir),
        output_path=Path(args.output),
        write_snapshot=not args.no_snapshot,
        source=args.source,
    )
    print(f"Wrote HTML report: {output}")
    if snapshot:
        print(f"Wrote snapshot: {snapshot}")


if __name__ == "__main__":
    main()
