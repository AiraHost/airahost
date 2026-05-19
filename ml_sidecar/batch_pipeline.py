from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from xgboost import XGBRegressor

from ml_sidecar.data import (
    TARGET_COLUMN_NAME,
    extract_listing_features,
    fetch_saved_listing_by_id,
    fetch_training_dataset,
    get_default_training_scope,
)
from ml_sidecar.model import (
    AMENITIES_LIST,
    AMENITY_FEATURE_WEIGHTS,
    build_default_numeric_features,
    build_amenity_feature_map,
    build_feature_description_df,
    build_feature_matrix_df,
    forecast_prices,
    train_model,
)
from ml_sidecar.supabase_client import get_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_ROOT / "ml_sidecar" / "reports"


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def resolve_repo_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def repo_relative_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except Exception:
        return str(path.resolve()).replace("\\", "/")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the AIRAHOST ML sidecar forecast from Supabase raw market observations."
    )
    parser.add_argument(
        "--saved-listing-id",
        required=True,
        help="UUID of the target saved listing.",
    )
    parser.add_argument("--retrain", action="store_true", help="Force retraining even if a model exists.")
    parser.add_argument("--reuse-model", action="store_true", help="Reuse the saved model when feature columns still match.")
    parser.add_argument("--horizon", type=int, default=30, help="Forecast horizon in days.")
    parser.add_argument("--start-date", help="Forecast start date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--limit", type=int, default=5000, help="Maximum raw training observations to load.")
    parser.add_argument(
        "--training-scope",
        choices=["global", "listing_local"],
        default=get_default_training_scope(),
        help="Training dataset scope.",
    )
    parser.add_argument(
        "--manifest-output",
        default=str(REPORTS_DIR / "latest_manifest.json"),
        help="Manifest JSON output path.",
    )
    parser.add_argument(
        "--predictions-output",
        default=str(REPORTS_DIR / "predictions.csv"),
        help="Prediction CSV output path.",
    )
    parser.add_argument(
        "--model-path",
        default=str(REPORTS_DIR / "saved_model.json"),
        help="Serialized XGBoost model path.",
    )
    return parser.parse_args()


def _build_target_features(saved_listing: Dict[str, Any], training_df: pd.DataFrame) -> Dict[str, float]:
    base = extract_listing_features(saved_listing)
    defaults = build_default_numeric_features(training_df)

    target_features: Dict[str, float] = {
        "property_type": str(base.get("property_type") or "unknown"),
        "bedrooms": float(base.get("bedrooms") or 0.0),
        "baths": float(base.get("baths") or 0.0),
        "accommodates": float(base.get("accommodates") or 0.0),
        "beds": float(base.get("beds") or 0.0),
        "lat": float(base.get("lat") or 0.0),
        "lng": float(base.get("lng") or 0.0),
        "comps_used": defaults.get("comps_used", 0.0),
    }

    amenities = base.get("amenities") or []
    target_features.update(build_amenity_feature_map(amenities))

    for key, value in defaults.items():
        target_features.setdefault(key, value)

    return target_features


def _build_metric_row(
    metrics: Optional[Dict[str, Any]],
    *,
    train_duration: float,
    n_samples: int,
    trained_now: bool,
    model_mode: str,
) -> Dict[str, Any]:
    metrics_row = {
        "timestamp": datetime.now().isoformat(),
        "mae": round(float(metrics["mae"]), 4) if metrics else None,
        "mae_std": round(float(metrics.get("mae_std", 0.0)), 4) if metrics else None,
        "mape": round(float(metrics["mape"]), 6) if metrics else None,
        "q2": round(float(metrics["q2"]), 6) if metrics else None,
        "r2": round(float(metrics["r2"]), 6) if metrics else None,
        "r2_std": round(float(metrics.get("r2_std", 0.0)), 6) if metrics else None,
        "ae_p50": round(float(metrics.get("ae_p50", 0.0)), 4) if metrics else None,
        "ae_p80": round(float(metrics.get("ae_p80", 0.0)), 4) if metrics else None,
        "ae_p95": round(float(metrics.get("ae_p95", 0.0)), 4) if metrics else None,
        "ape_p50": round(float(metrics.get("ape_p50", 0.0)), 6) if metrics else None,
        "ape_p80": round(float(metrics.get("ape_p80", 0.0)), 6) if metrics else None,
        "ape_p95": round(float(metrics.get("ape_p95", 0.0)), 6) if metrics else None,
        "training_time_seconds": round(float(train_duration), 4),
        "n_samples": int(n_samples),
        "trained_now": bool(trained_now),
        "model_mode": model_mode,
        "cv_strategy": metrics.get("cv_strategy") if metrics else None,
    }
    metrics_row.update(_build_model_confidence(metrics_row))
    return metrics_row


def _load_metrics_row(metrics_path: Path) -> Optional[Dict[str, Any]]:
    if not metrics_path.exists():
        return None
    try:
        metrics_df = pd.read_csv(metrics_path)
    except Exception:
        return None
    if metrics_df.empty:
        return None
    return metrics_df.iloc[0].to_dict()


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _confidence_band(score: float) -> str:
    if score >= 80:
        return "high"
    if score >= 60:
        return "medium"
    return "low"


def _build_model_confidence(metrics_row: Dict[str, Any]) -> Dict[str, Any]:
    q2 = float(metrics_row.get("q2") or 0.0)
    mape = float(metrics_row.get("mape") or 0.5)
    r2_std = float(metrics_row.get("r2_std") or 0.25)
    n_samples = max(0, int(metrics_row.get("n_samples") or 0))

    q2_score = _clamp01((q2 - 0.05) / 0.60)
    mape_score = _clamp01(1.0 - (mape / 0.35))
    stability_score = _clamp01(1.0 - (r2_std / 0.25))
    sample_score = _clamp01(math.log1p(n_samples) / math.log1p(1500))

    confidence_score = round(
        100
        * (
            (0.40 * q2_score)
            + (0.25 * mape_score)
            + (0.20 * stability_score)
            + (0.15 * sample_score)
        )
    )

    reasons: list[str] = []
    if q2_score >= 0.7:
        reasons.append("strong cross-validation accuracy")
    elif q2_score < 0.35:
        reasons.append("weak cross-validation accuracy")

    if mape_score >= 0.7:
        reasons.append("prediction error stayed within a tight range")
    elif mape_score < 0.35:
        reasons.append("historical prediction error is still fairly wide")

    if sample_score >= 0.7:
        reasons.append("training dataset has good market coverage")
    elif sample_score < 0.35:
        reasons.append("training dataset is still relatively small")

    if stability_score < 0.35:
        reasons.append("cross-validation stability is still noisy")

    return {
        "model_confidence_score": int(confidence_score),
        "model_confidence_band": _confidence_band(confidence_score),
        "model_confidence_reasons": reasons[:3],
    }


def _pick_support_rows(
    training_df: pd.DataFrame,
    *,
    property_type: str,
    is_weekend: float,
    is_holiday: float,
    lead_time_days: float,
) -> pd.DataFrame:
    data = training_df.copy()
    if data.empty:
        return data

    data["_property_type"] = data.get(
        "property_type",
        pd.Series("unknown", index=data.index),
    ).fillna("unknown").astype(str)
    data["_is_weekend"] = pd.to_numeric(
        data.get("is_weekend", pd.Series(0.0, index=data.index)),
        errors="coerce",
    ).fillna(0.0)
    data["_is_holiday"] = pd.to_numeric(
        data.get("is_holiday", pd.Series(0.0, index=data.index)),
        errors="coerce",
    ).fillna(0.0)
    data["_lead_time_days"] = pd.to_numeric(
        data.get("lead_time_days", pd.Series(0.0, index=data.index)),
        errors="coerce",
    ).fillna(0.0)

    exact = data[
        (data["_property_type"] == property_type)
        & (data["_is_weekend"] == float(is_weekend))
        & (data["_is_holiday"] == float(is_holiday))
        & ((data["_lead_time_days"] - float(lead_time_days)).abs() <= 14)
    ]
    if len(exact) >= 12:
        return exact

    relaxed = data[
        (data["_property_type"] == property_type)
        & (data["_is_weekend"] == float(is_weekend))
        & ((data["_lead_time_days"] - float(lead_time_days)).abs() <= 21)
    ]
    if len(relaxed) >= 12:
        return relaxed

    broad = data[
        (data["_property_type"] == property_type)
        & (data["_is_weekend"] == float(is_weekend))
    ]
    if len(broad) >= 8:
        return broad

    weekend_only = data[
        (data["_is_weekend"] == float(is_weekend))
        & ((data["_lead_time_days"] - float(lead_time_days)).abs() <= 21)
    ]
    if len(weekend_only) >= 8:
        return weekend_only

    if not broad.empty:
        return broad
    if not weekend_only.empty:
        return weekend_only
    return data


def _enrich_predictions_with_confidence(
    *,
    predictions: List[Dict[str, Any]],
    training_df: pd.DataFrame,
    target_features: Dict[str, float],
    metrics_row: Dict[str, Any],
    start_date: date,
) -> List[Dict[str, Any]]:
    model_confidence = _build_model_confidence(metrics_row)
    model_confidence_norm = float(model_confidence["model_confidence_score"]) / 100.0

    base_ape80 = float(
        metrics_row.get("ape_p80")
        or max(float(metrics_row.get("mape") or 0.12) * 1.25, 0.08)
    )
    base_ape95 = float(
        metrics_row.get("ape_p95")
        or max(base_ape80 * 1.35, float(metrics_row.get("mape") or 0.12) * 1.8, 0.12)
    )

    today_ts = pd.Timestamp(datetime.utcnow().date())
    enriched: list[Dict[str, Any]] = []

    for prediction in predictions:
        stay_date = date.fromisoformat(str(prediction["date"]))
        date_features = {
            "lead_time_days": float(max(0, (stay_date - start_date).days)),
            "is_weekend": 1.0 if bool(prediction.get("is_weekend")) else 0.0,
            "is_holiday": 1.0 if bool(prediction.get("is_holiday")) else 0.0,
        }

        support_rows = _pick_support_rows(
            training_df,
            property_type=str(target_features.get("property_type") or "unknown"),
            is_weekend=date_features["is_weekend"],
            is_holiday=date_features["is_holiday"],
            lead_time_days=date_features["lead_time_days"],
        )

        support_count = int(len(support_rows))
        support_score = _clamp01(support_count / 60.0)

        local_prices = pd.to_numeric(
            support_rows.get(TARGET_COLUMN_NAME, pd.Series(dtype=float)),
            errors="coerce",
        ).dropna()
        if local_prices.empty:
            stability_score = 0.35
        else:
            median_price = float(local_prices.median())
            iqr = float(local_prices.quantile(0.75) - local_prices.quantile(0.25))
            dispersion_ratio = iqr / max(median_price, 1.0)
            stability_score = _clamp01(1.0 - (dispersion_ratio / 0.80))

        observation_dates = pd.to_datetime(
            support_rows.get("observation_date", pd.Series(dtype=object)),
            errors="coerce",
        ).dropna()
        if observation_dates.empty:
            recency_score = 0.35
        else:
            median_days_old = float((today_ts - observation_dates).dt.days.median())
            recency_score = _clamp01(1.0 - (median_days_old / 180.0))

        guardrail_penalty = 0.10 if bool(prediction.get("guardrail_applied")) else 0.0
        prediction_confidence_norm = _clamp01(
            (0.50 * model_confidence_norm)
            + (0.25 * support_score)
            + (0.15 * stability_score)
            + (0.10 * recency_score)
            - guardrail_penalty
        )
        prediction_confidence_score = int(round(100 * prediction_confidence_norm))

        width_multiplier = 1.0 + ((1.0 - prediction_confidence_norm) * 0.60)
        interval80_pct = min(0.95, max(base_ape80, 0.05) * width_multiplier)
        interval95_pct = min(1.25, max(base_ape95, interval80_pct + 0.05) * width_multiplier)

        predicted_price = float(prediction.get("predicted_price") or 0.0)
        reasons: list[str] = []
        if support_count >= 40:
            reasons.append("good local sample support")
        elif support_count < 12:
            reasons.append("thin local sample support")

        if recency_score >= 0.70:
            reasons.append("recent market observations")
        elif recency_score < 0.35:
            reasons.append("older comparable observations")

        if stability_score < 0.35:
            reasons.append("similar dates remain price-volatile")

        if bool(prediction.get("guardrail_applied")):
            reasons.append("guardrail adjusted the raw forecast")

        if not reasons:
            reasons.append("signal quality looks balanced for this forecast date")

        enriched.append(
            {
                **prediction,
                "prediction_confidence_score": prediction_confidence_score,
                "prediction_confidence_band": _confidence_band(prediction_confidence_score),
                "support_count": support_count,
                "interval80_low": round(max(0.0, predicted_price * (1.0 - interval80_pct)), 2),
                "interval80_high": round(predicted_price * (1.0 + interval80_pct), 2),
                "interval95_low": round(max(0.0, predicted_price * (1.0 - interval95_pct)), 2),
                "interval95_high": round(predicted_price * (1.0 + interval95_pct), 2),
                "confidence_reasons": reasons[:3],
            }
        )

    return enriched


def _feature_driver_label(feature_name: str) -> str:
    if feature_name in {
        "day_of_week",
        "lead_time_days",
        "day_of_year",
        "dow_sin",
        "dow_cos",
        "doy_sin",
        "doy_cos",
        "month",
        "is_weekend",
        "is_holiday",
    }:
        return "Stay-date timing"
    if feature_name in {"bedrooms", "baths", "accommodates", "beds"}:
        return "Listing size and capacity"
    if feature_name in {"lat", "lng"}:
        return "Location"
    if feature_name == "comps_used":
        return "Nearby comparable density"
    if feature_name.startswith("property_type_"):
        return "Property type"
    if feature_name.startswith("has_"):
        return "Amenity mix"
    return "Other signal"


def _build_explanation_payload(
    *,
    importances: pd.Series,
    target_features: Dict[str, float],
    metrics_row: Dict[str, Any],
    horizon: int,
) -> Dict[str, Any]:
    grouped: Dict[str, float] = {}
    for feature_name, importance in importances.items():
        label = _feature_driver_label(str(feature_name))
        grouped[label] = grouped.get(label, 0.0) + float(importance)

    top_driver_labels = [
        label
        for label, weight in sorted(grouped.items(), key=lambda item: item[1], reverse=True)
        if weight > 0
    ][:3]

    property_type = str(target_features.get("property_type") or "unknown").replace("_", " ")
    bedrooms = int(round(float(target_features.get("bedrooms") or 0.0)))
    baths = round(float(target_features.get("baths") or 0.0), 1)
    accommodates = int(round(float(target_features.get("accommodates") or 0.0)))
    amenity_rank = {name: index for index, name in enumerate(AMENITIES_LIST)}
    active_amenities = [
        amenity.replace("_", " ")
        for amenity in sorted(
            (
                feature_name.removeprefix("has_")
                for feature_name, value in target_features.items()
                if feature_name.startswith("has_") and float(value or 0.0) > 0.5
            ),
            key=lambda amenity: (
                -float(AMENITY_FEATURE_WEIGHTS.get(amenity, 1.0)),
                amenity_rank.get(amenity, len(AMENITIES_LIST)),
                amenity,
            ),
        )
    ][:4]

    summary_parts: list[str] = []
    if top_driver_labels:
        if len(top_driver_labels) == 1:
            driver_text = top_driver_labels[0].lower()
        elif len(top_driver_labels) == 2:
            driver_text = f"{top_driver_labels[0].lower()} and {top_driver_labels[1].lower()}"
        else:
            driver_text = (
                f"{top_driver_labels[0].lower()}, "
                f"{top_driver_labels[1].lower()}, and "
                f"{top_driver_labels[2].lower()}"
            )
        summary_parts.append(
            f"This forecast is driven mostly by {driver_text} learned from historical market observations."
        )

    summary_parts.append(
        f"For this listing, the model is pricing a {property_type} with {bedrooms} bedroom(s), "
        f"{baths} bath(s), and room for {accommodates} guest(s)."
    )

    if active_amenities:
        summary_parts.append(
            f"It also uses amenity signals such as {', '.join(active_amenities)} when those features mattered in similar listings."
        )

    model_band = metrics_row.get("model_confidence_band")
    model_score = metrics_row.get("model_confidence_score")
    if model_band and model_score is not None:
        summary_parts.append(
            f"Overall model confidence is {int(model_score)}/100 ({model_band})."
        )

    feature_highlights = [
        f"Listing profile: {property_type}, {bedrooms} bd, {baths} ba, sleeps {accommodates}.",
        "Date effects matter: weekday vs weekend, holidays, and booking lead time can shift each forecast day.",
        "Nearby market density helps when more comparable observations are available.",
    ]
    if active_amenities:
        feature_highlights.append(
            f"Amenity signal included: {', '.join(active_amenities)}."
        )

    return {
        "summary": " ".join(summary_parts),
        "top_drivers": top_driver_labels,
        "feature_highlights": feature_highlights[:4],
        "horizon_days": int(horizon),
        "display_note": (
            f"The model computes a {int(horizon)}-day forecast. "
            "The card starts with 7 days for readability, and you can switch to the full horizon in the UI."
        ),
    }


def _load_or_train_model(
    training_df: pd.DataFrame,
    *,
    model_path: Path,
    reuse_model: bool,
    force_train: bool,
    metrics_path: Path,
) -> Tuple[XGBRegressor, List[str], pd.Series, Optional[Dict[str, Any]], float, bool, str]:
    if reuse_model and force_train:
        raise ValueError("--reuse-model and --retrain cannot be used together.")

    if reuse_model and model_path.exists() and not force_train:
        feature_matrix_df = build_feature_matrix_df(training_df)
        feature_columns = [
            column
            for column in feature_matrix_df.columns
            if column != TARGET_COLUMN_NAME and not column.startswith("debug_")
        ]
        model = XGBRegressor()
        model._estimator_type = "regressor"
        try:
            model.load_model(str(model_path))
            booster_feature_names = list(model.get_booster().feature_names or [])
            if booster_feature_names and booster_feature_names != feature_columns:
                raise ValueError("Saved model feature columns do not match the current training matrix.")
            importances = pd.Series(model.feature_importances_, index=feature_columns).sort_values(ascending=False)
            return (
                model,
                feature_columns,
                importances,
                _load_metrics_row(metrics_path),
                0.0,
                False,
                "reused_saved_model",
            )
        except Exception as exc:
            print(f"[ML Sidecar] Saved model could not be reused ({exc}). Retraining instead.")

    start = time.time()
    model, feature_columns, importances, metrics = train_model(training_df)
    train_duration = time.time() - start
    model._estimator_type = "regressor"
    model.save_model(str(model_path))
    return model, feature_columns, importances, metrics, train_duration, True, "trained_fresh"


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def execute_batch_workflow(
    *,
    saved_listing_id: str,
    training_scope: str = "global",
    force_train: bool = False,
    reuse_model: bool = False,
    predictions_output_path: str | Path = REPORTS_DIR / "predictions.csv",
    manifest_output_path: str | Path = REPORTS_DIR / "latest_manifest.json",
    model_path: str | Path = REPORTS_DIR / "saved_model.json",
    horizon: int = 30,
    start_date: Optional[date] = None,
    limit: int = 5000,
) -> Dict[str, Any]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    resolved_predictions_output = resolve_repo_path(predictions_output_path)
    resolved_manifest_output = resolve_repo_path(manifest_output_path)
    resolved_model_path = resolve_repo_path(model_path)
    for path in (resolved_predictions_output, resolved_manifest_output, resolved_model_path):
        path.parent.mkdir(parents=True, exist_ok=True)

    metrics_path = REPORTS_DIR / "metrics_latest.csv"
    training_matrix_path = REPORTS_DIR / "training_matrix.csv"
    feature_descriptions_path = REPORTS_DIR / "feature_descriptions.csv"

    client = get_client()
    saved_listing = fetch_saved_listing_by_id(client, saved_listing_id)
    if not saved_listing:
        raise ValueError(f"Saved listing {saved_listing_id} was not found.")

    training_df = fetch_training_dataset(
        client,
        saved_listing_id=saved_listing_id,
        limit=limit,
        training_scope=training_scope,
    )
    if training_df.empty:
        raise RuntimeError(
            "No market observations were returned for ML training. "
            "Run the nightly worker first so market_price_observations has data."
        )

    training_matrix_df = build_feature_matrix_df(training_df)
    training_matrix_df.to_csv(training_matrix_path, index=False)

    feature_description_df = build_feature_description_df(list(training_matrix_df.columns))
    feature_description_df.to_csv(feature_descriptions_path, index=False)

    model, feature_columns, importances, metrics, train_duration, trained_now, model_mode = _load_or_train_model(
        training_df,
        model_path=resolved_model_path,
        reuse_model=reuse_model,
        force_train=force_train,
        metrics_path=metrics_path,
    )

    metrics_row = _build_metric_row(
        metrics if isinstance(metrics, dict) else None,
        train_duration=train_duration,
        n_samples=len(training_df),
        trained_now=trained_now,
        model_mode=model_mode,
    )
    pd.DataFrame([metrics_row]).to_csv(metrics_path, index=False)

    forecast_start_date = start_date or date.today()
    target_features = _build_target_features(saved_listing, training_df)
    predictions = forecast_prices(
        model,
        feature_columns,
        target_features,
        start_date=forecast_start_date,
        horizon=horizon,
    )
    predictions = _enrich_predictions_with_confidence(
        predictions=predictions,
        training_df=training_df,
        target_features=target_features,
        metrics_row=metrics_row,
        start_date=forecast_start_date,
    )
    explanation = _build_explanation_payload(
        importances=importances,
        target_features=target_features,
        metrics_row=metrics_row,
        horizon=horizon,
    )

    predictions_df = pd.DataFrame(
        [
            {
                "date": item["date"],
                "predicted_price": round(float(item["predicted_price"]), 2),
                "predicted_price_raw": round(float(item["predicted_price_raw"]), 2),
                "guardrail_applied": bool(item["guardrail_applied"]),
                "is_weekend": bool(item["is_weekend"]),
                "is_holiday": bool(item["is_holiday"]),
                "prediction_confidence_score": int(item["prediction_confidence_score"]),
                "prediction_confidence_band": item["prediction_confidence_band"],
                "support_count": int(item["support_count"]),
                "interval80_low": round(float(item["interval80_low"]), 2),
                "interval80_high": round(float(item["interval80_high"]), 2),
                "interval95_low": round(float(item["interval95_low"]), 2),
                "interval95_high": round(float(item["interval95_high"]), 2),
                "confidence_reasons": item["confidence_reasons"],
            }
            for item in predictions
        ]
    )
    predictions_df.to_csv(resolved_predictions_output, index=False)

    manifest = {
        "generated_at": datetime.now().isoformat(),
        "listing_id": saved_listing["id"],
        "listing_name": saved_listing.get("name"),
        "training_scope": training_scope,
        "trained_now": trained_now,
        "model_mode": model_mode,
        "n_samples": int(len(training_df)),
        "start_date": forecast_start_date.isoformat(),
        "horizon": int(horizon),
        "metrics": metrics_row,
        "explanation": explanation,
        "predictions": predictions,
        "artifacts": {
            "manifest": repo_relative_path(resolved_manifest_output),
            "predictions_csv": repo_relative_path(resolved_predictions_output),
            "metrics_csv": repo_relative_path(metrics_path),
            "training_matrix_csv": repo_relative_path(training_matrix_path),
            "feature_descriptions_csv": repo_relative_path(feature_descriptions_path),
            "saved_model": repo_relative_path(resolved_model_path),
        },
    }
    _write_json(resolved_manifest_output, manifest)
    print(f"[ML Sidecar] Wrote manifest to {resolved_manifest_output}")
    return manifest


def main() -> None:
    configure_console_encoding()
    args = parse_args()
    resolved_start_date = date.fromisoformat(args.start_date) if args.start_date else None
    execute_batch_workflow(
        saved_listing_id=args.saved_listing_id,
        training_scope=args.training_scope,
        force_train=args.retrain,
        reuse_model=args.reuse_model,
        predictions_output_path=args.predictions_output,
        manifest_output_path=args.manifest_output,
        model_path=args.model_path,
        horizon=args.horizon,
        start_date=resolved_start_date,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
