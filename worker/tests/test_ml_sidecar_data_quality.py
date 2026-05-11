from __future__ import annotations

import pandas as pd

from ml_sidecar.data import TARGET_COLUMN_NAME
from ml_sidecar.data_quality import (
    QualityIssue,
    _validate_feature_matrix,
    _validate_raw_observations,
    _validate_report_ingestion,
    _validate_training_frame,
)


def test_report_ingestion_detects_missing_calendar_date() -> None:
    issues: list[QualityIssue] = []
    metrics = _validate_report_ingestion(
        reports=[
            {
                "id": "report-1",
                "result_calendar": [
                    {"date": "2026-05-09", "baseDailyPrice": 120},
                    {"date": "2026-05-10", "baseDailyPrice": 130},
                ],
            }
        ],
        observations=[
            {
                "pricing_report_id": "report-1",
                "stay_date": "2026-05-09",
                "base_daily_price": 120,
            }
        ],
        min_new_observations=1,
        price_tolerance=0.01,
        issues=issues,
    )

    assert metrics["missing_observation_date_count"] == 1
    assert any(issue.code == "missing_observation_dates" for issue in issues)


def test_report_ingestion_detects_price_mismatch() -> None:
    issues: list[QualityIssue] = []
    metrics = _validate_report_ingestion(
        reports=[
            {
                "id": "report-1",
                "result_calendar": [
                    {"date": "2026-05-09", "baseDailyPrice": 120},
                ],
            }
        ],
        observations=[
            {
                "pricing_report_id": "report-1",
                "stay_date": "2026-05-09",
                "base_daily_price": 121,
            }
        ],
        min_new_observations=1,
        price_tolerance=0.01,
        issues=issues,
    )

    assert metrics["price_mismatch_count"] == 1
    assert any(issue.code == "price_mismatch" for issue in issues)


def test_raw_observation_requires_positive_training_price() -> None:
    issues: list[QualityIssue] = []
    metrics = _validate_raw_observations(
        [
            {
                "pricing_report_id": "report-1",
                "saved_listing_id": "listing-1",
                "observed_at": "2026-05-08T08:00:00Z",
                "stay_date": "2026-05-09",
                "days_until_stay": 1,
                "base_daily_price": None,
                "effective_daily_price_refundable": None,
                "target_lat": 25.0,
                "target_lng": 121.0,
                "comps_used": 3,
            }
        ],
        issues=issues,
    )

    assert metrics["raw_no_positive_price_count"] == 1
    assert any(issue.code == "missing_training_target_price" for issue in issues)


def test_training_and_feature_matrix_pass_for_complete_rows() -> None:
    issues: list[QualityIssue] = []
    training_df = pd.DataFrame(
        [
            {
                "saved_listing_id": "listing-1",
                "property_type": "entire_home",
                "bedrooms": 2,
                "baths": 1,
                "accommodates": 4,
                "beds": 2,
                "comps_used": 8,
                "lat": 25.0,
                "lng": 121.0,
                "amenities": ["wifi", "kitchen", "ac"],
                "price_date": "2026-05-09",
                "observation_date": "2026-05-08",
                "day_of_week": 5,
                "month": 5,
                "day_of_year": 129,
                "dow_sin": 0.1,
                "dow_cos": 0.2,
                "doy_sin": 0.3,
                "doy_cos": 0.4,
                "lead_time_days": 1,
                "is_weekend": 1,
                "is_holiday": 0,
                TARGET_COLUMN_NAME: 150,
            },
            {
                "saved_listing_id": "listing-1",
                "property_type": "entire_home",
                "bedrooms": 2,
                "baths": 1,
                "accommodates": 4,
                "beds": 2,
                "comps_used": 7,
                "lat": 25.0,
                "lng": 121.0,
                "amenities": ["wifi", "kitchen"],
                "price_date": "2026-05-10",
                "observation_date": "2026-05-08",
                "day_of_week": 6,
                "month": 5,
                "day_of_year": 130,
                "dow_sin": 0.2,
                "dow_cos": 0.3,
                "doy_sin": 0.4,
                "doy_cos": 0.5,
                "lead_time_days": 2,
                "is_weekend": 0,
                "is_holiday": 0,
                TARGET_COLUMN_NAME: 140,
            },
        ]
    )

    _validate_training_frame(
        training_df,
        min_training_rows=2,
        max_missing_feature_rate=0.05,
        issues=issues,
    )
    matrix, feature_metrics = _validate_feature_matrix(training_df, issues=issues)

    assert matrix.shape[0] == 2
    assert feature_metrics["feature_column_count"] > 0
    assert not [issue for issue in issues if issue.severity == "error"]
