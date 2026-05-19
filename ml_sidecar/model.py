from __future__ import annotations

import datetime as dt
import math
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error, r2_score
from sklearn.model_selection import KFold, TimeSeriesSplit
from xgboost import XGBRegressor

from ml_sidecar.data import TARGET_COLUMN_NAME, _compute_date_features

# XGBoost's sklearn wrapper may miss this marker with some sklearn/xgboost
# combinations, which breaks save_model(). We only use the regressor wrapper here.
XGBRegressor._estimator_type = "regressor"

AMENITIES_LIST = [
    "wifi",
    "kitchen",
    "washer",
    "dryer",
    "ac",
    "heating",
    "pool",
    "hot_tub",
    "free_parking",
    "pets_allowed",
    "waterfront",
    "guest_favorite",
    "ev_charger",
    "gym",
    "bbq",
]

# These weights let us give extra ML signal to the amenities the product cares
# about most when forecasting price. The strongest emphasis is on A/C,
# kitchen, and in-unit laundry coverage.
AMENITY_FEATURE_WEIGHTS: Dict[str, float] = {
    "wifi": 1.0,
    "kitchen": 1.35,
    "washer": 1.25,
    "dryer": 1.25,
    "ac": 1.45,
    "heating": 1.0,
    "pool": 1.0,
    "hot_tub": 1.0,
    "free_parking": 1.0,
    "pets_allowed": 1.0,
    "waterfront": 1.0,
    "guest_favorite": 1.0,
    "ev_charger": 1.0,
    "gym": 1.0,
    "bbq": 1.0,
}
AMENITY_SIGNAL_ALIASES: Dict[str, Tuple[str, ...]] = {
    # Airbnb exposes several water-adjacent badges. Group them into one
    # waterfront signal so older saved attrs still activate the model feature.
    "waterfront": ("waterfront", "lake_access", "beach_access"),
}
PRIORITY_AMENITY_FEATURES = ("ac", "kitchen", "washer", "dryer")
PRIORITY_AMENITY_SCORE_MAX = float(
    sum(AMENITY_FEATURE_WEIGHTS[name] for name in PRIORITY_AMENITY_FEATURES)
)
AMENITY_DERIVED_FEATURES = [
    "amenity_weighted_score",
    "priority_amenity_score",
    "priority_amenity_ratio",
    "laundry_amenity_count",
]

NUMERIC_FEATURES = [
    "bedrooms",
    "baths",
    "accommodates",
    "beds",
    "comps_used",
    "is_weekend",
    "is_holiday",
    "lat",
    "lng",
    "day_of_week",
    "lead_time_days",
    "day_of_year",
    "dow_sin",
    "dow_cos",
    "doy_sin",
    "doy_cos",
    "month",
] + [f"has_{amenity}" for amenity in AMENITIES_LIST] + AMENITY_DERIVED_FEATURES

CATEGORICAL_FEATURE = "property_type"

FEATURE_DESCRIPTIONS: Dict[str, str] = {
    "bedrooms": "Listing bedroom count from saved_listings or raw market observation metadata.",
    "baths": "Listing bathroom count from saved_listings or raw market observation metadata.",
    "accommodates": "Listing guest capacity from saved_listings or raw market observation metadata.",
    "beds": "Listing bed count from saved_listings or raw market observation metadata.",
    "comps_used": "Number of comparable listings captured for the source market observation.",
    "is_weekend": "Binary flag for Friday/Saturday stays.",
    "is_holiday": "Binary holiday flag derived from the configured holiday calendar.",
    "lat": "Target listing latitude.",
    "lng": "Target listing longitude.",
    "day_of_week": "Stay-date weekday index (0=Mon ... 6=Sun).",
    "lead_time_days": "Days between observation date and stay date.",
    "day_of_year": "Stay-date day of year.",
    "dow_sin": "Cyclical sine encoding of the weekday.",
    "dow_cos": "Cyclical cosine encoding of the weekday.",
    "doy_sin": "Cyclical sine encoding of the day of year.",
    "doy_cos": "Cyclical cosine encoding of the day of year.",
    "month": "Stay-date month index.",
    "amenity_weighted_score": (
        "Hand-weighted amenity score that emphasizes kitchen, A/C, washer, and dryer."
    ),
    "priority_amenity_score": (
        "Focused weighted score for the priority amenities: A/C, kitchen, washer, and dryer."
    ),
    "priority_amenity_ratio": (
        "Normalized 0-1 coverage ratio for the priority amenities: A/C, kitchen, washer, and dryer."
    ),
    "laundry_amenity_count": "Count of laundry amenities present (washer + dryer).",
    "property_type": "Saved listing property type, one-hot encoded.",
}


def _safe_quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(np.quantile(np.array(values, dtype=float), q))


def _normalize_amenity_names(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()

    normalized: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip().casefold()
        if text:
            normalized.add(text)
    return normalized


def _has_amenity_signal(amenity_names: set[str], amenity: str) -> bool:
    return any(
        alias in amenity_names
        for alias in AMENITY_SIGNAL_ALIASES.get(amenity, (amenity,))
    )


def build_amenity_feature_map(value: Any) -> Dict[str, float]:
    amenity_names = _normalize_amenity_names(value)
    feature_map: Dict[str, float] = {
        f"has_{amenity}": 1.0 if _has_amenity_signal(amenity_names, amenity) else 0.0
        for amenity in AMENITIES_LIST
    }

    amenity_weighted_score = sum(
        AMENITY_FEATURE_WEIGHTS[amenity]
        for amenity in AMENITIES_LIST
        if _has_amenity_signal(amenity_names, amenity)
    )
    priority_amenity_score = sum(
        AMENITY_FEATURE_WEIGHTS[amenity]
        for amenity in PRIORITY_AMENITY_FEATURES
        if amenity in amenity_names
    )

    feature_map.update(
        {
            "amenity_weighted_score": float(amenity_weighted_score),
            "priority_amenity_score": float(priority_amenity_score),
            "priority_amenity_ratio": (
                float(priority_amenity_score / PRIORITY_AMENITY_SCORE_MAX)
                if PRIORITY_AMENITY_SCORE_MAX > 0
                else 0.0
            ),
            "laundry_amenity_count": float(
                int("washer" in amenity_names) + int("dryer" in amenity_names)
            ),
        }
    )
    return feature_map


def _clean_training_frame(df: pd.DataFrame) -> pd.DataFrame:
    cleaned = df.copy()
    if TARGET_COLUMN_NAME not in cleaned.columns:
        raise ValueError(f"Training data must include '{TARGET_COLUMN_NAME}'.")

    cleaned = cleaned[cleaned[TARGET_COLUMN_NAME].notna()].copy()
    if cleaned.empty:
        return cleaned

    mean_price = cleaned[TARGET_COLUMN_NAME].mean()
    std_price = cleaned[TARGET_COLUMN_NAME].std()
    if std_price and std_price > 0:
        upper = mean_price + (3 * std_price)
        lower = max(0.0, mean_price - (3 * std_price))
        cleaned = cleaned[
            (cleaned[TARGET_COLUMN_NAME] <= upper)
            & (cleaned[TARGET_COLUMN_NAME] >= lower)
        ].copy()

    if "amenities" in cleaned.columns:
        amenity_features = pd.DataFrame(
            cleaned["amenities"].apply(build_amenity_feature_map).tolist(),
            index=cleaned.index,
        )
        cleaned = pd.concat([cleaned, amenity_features], axis=1)

    for column in NUMERIC_FEATURES:
        if column not in cleaned.columns:
            cleaned[column] = 0.0

    cleaned[NUMERIC_FEATURES] = (
        cleaned[NUMERIC_FEATURES].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    )
    if CATEGORICAL_FEATURE not in cleaned.columns:
        cleaned[CATEGORICAL_FEATURE] = "unknown"
    cleaned[CATEGORICAL_FEATURE] = cleaned[CATEGORICAL_FEATURE].fillna("unknown").astype(str)
    return cleaned


def _feature_matrix(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series]:
    categorical = pd.get_dummies(df[CATEGORICAL_FEATURE], prefix=CATEGORICAL_FEATURE)
    features = pd.concat([df[NUMERIC_FEATURES], categorical], axis=1)
    target = df[TARGET_COLUMN_NAME].astype(float)
    return features, target


def build_default_numeric_features(df: pd.DataFrame) -> Dict[str, float]:
    cleaned = _clean_training_frame(df)
    defaults: Dict[str, float] = {}
    for column in NUMERIC_FEATURES:
        defaults[column] = float(cleaned[column].median()) if not cleaned.empty else 0.0
    return defaults


def build_feature_matrix_df(df: pd.DataFrame) -> pd.DataFrame:
    cleaned = _clean_training_frame(df)
    features, target = _feature_matrix(cleaned)
    matrix = features.copy()
    if "price_date" in cleaned.columns:
        matrix["debug_price_date"] = cleaned["price_date"].values
    if "observation_date" in cleaned.columns:
        matrix["debug_observation_date"] = cleaned["observation_date"].values
    if "saved_listing_id" in cleaned.columns:
        matrix["debug_saved_listing_id"] = cleaned["saved_listing_id"].values
    matrix[TARGET_COLUMN_NAME] = target.values
    return matrix


def build_feature_description_df(feature_columns: List[str]) -> pd.DataFrame:
    rows: list[Dict[str, str]] = []
    prefix = f"{CATEGORICAL_FEATURE}_"

    for column in feature_columns:
        if column == TARGET_COLUMN_NAME:
            description = "Observed market target price used for supervised training."
        elif column in FEATURE_DESCRIPTIONS:
            description = FEATURE_DESCRIPTIONS[column]
        elif column.startswith("has_"):
            description = (
                f"Binary amenity flag for '{column.removeprefix('has_').replace('_', ' ')}'."
            )
        elif column.startswith(prefix):
            description = f"One-hot encoded property type for '{column[len(prefix):]}'."
        else:
            description = "Generated feature column."
        rows.append({"feature": column, "description": description})

    return pd.DataFrame(rows)


def train_model(df: pd.DataFrame) -> Tuple[XGBRegressor, List[str], pd.Series, Dict[str, Any]]:
    cleaned = _clean_training_frame(df)
    if cleaned.empty:
        raise ValueError("No usable training rows were found after cleaning.")
    if len(cleaned) < 2:
        raise ValueError("At least two training rows are required for ML sidecar training.")

    if "price_date" in cleaned.columns:
        cleaned = cleaned.sort_values("price_date").reset_index(drop=True)

    features, target = _feature_matrix(cleaned)
    target_log = np.log1p(target)

    unique_dates = cleaned["price_date"].nunique() if "price_date" in cleaned.columns else 1
    max_time_splits = min(5, len(cleaned) - 1)
    max_kfold_splits = min(5, len(cleaned))

    if unique_dates >= (max_time_splits + 1) and max_time_splits >= 2:
        cv = TimeSeriesSplit(n_splits=max_time_splits)
        cv_label = "TimeSeriesSplit"
    else:
        cv = KFold(n_splits=max(2, max_kfold_splits), shuffle=False)
        cv_label = "KFold-chronological"

    observation_dates = pd.to_datetime(
        cleaned.get("observation_date"),
        errors="coerce",
    ).dt.date
    today = dt.date.today()
    days_old = observation_dates.apply(
        lambda value: (today - value).days if isinstance(value, dt.date) else 0
    ).fillna(0.0)
    recency_weights = np.exp(-0.03 * days_old.to_numpy(dtype=float))

    comp_signal = cleaned.get("comps_used", pd.Series(0.0, index=cleaned.index))
    comp_weights = 1.0 + np.clip(comp_signal.to_numpy(dtype=float), 0.0, 20.0) / 20.0
    priority_signal = cleaned.get(
        "priority_amenity_ratio",
        pd.Series(0.0, index=cleaned.index),
    )
    priority_weights = 1.0 + (
        np.clip(priority_signal.to_numpy(dtype=float), 0.0, 1.0) * 0.20
    )

    weights = recency_weights * comp_weights * priority_weights
    if weights.size > 0:
        weights = np.clip(weights, 0.0, np.mean(weights) * 5.0)
    weights = weights / weights.mean()

    base_params = {
        "objective": "reg:squarederror",
        "n_estimators": 600,
        "max_depth": 5,
        "learning_rate": 0.05,
        "subsample": 0.85,
        "colsample_bytree": 0.85,
        "tree_method": "hist",
        "random_state": 42,
        "n_jobs": -1,
        "verbosity": 0,
    }

    fold_results: list[Dict[str, float]] = []
    absolute_errors: list[float] = []
    absolute_pct_errors: list[float] = []
    for train_index, test_index in cv.split(features):
        x_train = features.iloc[train_index]
        x_test = features.iloc[test_index]
        y_train = target_log.iloc[train_index]
        y_test = target_log.iloc[test_index]
        w_train = weights[train_index]

        fold_model = XGBRegressor(**base_params, early_stopping_rounds=50)
        fold_model.fit(
            x_train,
            y_train,
            sample_weight=w_train,
            eval_set=[(x_test, y_test)],
            verbose=False,
        )

        preds = np.expm1(fold_model.predict(x_test))
        actuals = np.expm1(y_test)
        abs_errors = np.abs(actuals - preds)
        abs_pct_errors = abs_errors / np.maximum(actuals, 1.0)
        absolute_errors.extend(abs_errors.astype(float).tolist())
        absolute_pct_errors.extend(abs_pct_errors.astype(float).tolist())
        fold_results.append(
            {
                "mae": float(mean_absolute_error(actuals, preds)),
                "mape": float(mean_absolute_percentage_error(actuals, preds)),
                "r2": float(r2_score(actuals, preds)),
            }
        )

    final_model = XGBRegressor(**base_params)
    final_model.fit(features, target_log, sample_weight=weights)
    fitted_preds = np.expm1(final_model.predict(features))
    fitted_r2 = float(r2_score(target, fitted_preds))

    importances = pd.Series(
        final_model.feature_importances_,
        index=features.columns,
    ).sort_values(ascending=False)

    metrics = {
        "mae": float(np.mean([result["mae"] for result in fold_results])),
        "mae_std": float(np.std([result["mae"] for result in fold_results])),
        "mape": float(np.mean([result["mape"] for result in fold_results])),
        "q2": float(np.mean([result["r2"] for result in fold_results])),
        "r2": fitted_r2,
        "r2_std": float(np.std([result["r2"] for result in fold_results])),
        "cv_strategy": cv_label,
        "n_samples": int(len(cleaned)),
        "ae_p50": _safe_quantile(absolute_errors, 0.50),
        "ae_p80": _safe_quantile(absolute_errors, 0.80),
        "ae_p95": _safe_quantile(absolute_errors, 0.95),
        "ape_p50": _safe_quantile(absolute_pct_errors, 0.50),
        "ape_p80": _safe_quantile(absolute_pct_errors, 0.80),
        "ape_p95": _safe_quantile(absolute_pct_errors, 0.95),
    }
    return final_model, list(features.columns), importances, metrics


def build_target_row(feature_values: Dict[str, float], feature_columns: List[str]) -> pd.DataFrame:
    row = {column: 0.0 for column in feature_columns}
    for column in NUMERIC_FEATURES:
        row[column] = float(feature_values.get(column, 0.0))

    property_type = str(feature_values.get(CATEGORICAL_FEATURE, "unknown") or "unknown")
    property_column = f"{CATEGORICAL_FEATURE}_{property_type}"
    if property_column in row:
        row[property_column] = 1.0

    return pd.DataFrame([row], columns=feature_columns)


def _apply_forecast_guardrail(
    prices: List[float],
    *,
    max_change_factor: float = 1.30,
    window: int = 7,
) -> List[float]:
    if len(prices) <= 1:
        return list(prices)

    values = np.array(prices, dtype=float)
    bounded = values.copy()
    half_window = window // 2

    for index in range(len(values)):
        lower = max(0, index - half_window)
        upper = min(len(values), index + half_window + 1)
        reference = float(np.median(values[lower:upper]))
        if reference > 0:
            bounded[index] = float(
                np.clip(
                    values[index],
                    reference / max_change_factor,
                    reference * max_change_factor,
                )
            )

    return bounded.tolist()


def forecast_prices(
    model: XGBRegressor,
    feature_columns: List[str],
    target_features: Dict[str, float],
    *,
    start_date: dt.date,
    horizon: int = 30,
) -> List[Dict[str, Any]]:
    results: list[Dict[str, Any]] = []

    for offset in range(horizon):
        stay_date = start_date + dt.timedelta(days=offset)
        row_features = target_features.copy()
        row_features.update(_compute_date_features(stay_date, start_date))
        row = build_target_row(row_features, feature_columns)
        predicted_price = float(np.expm1(model.predict(row)[0]))
        results.append(
            {
                "date": stay_date.isoformat(),
                "predicted_price": predicted_price,
                "is_weekend": bool(row_features["is_weekend"]),
                "is_holiday": bool(row_features["is_holiday"]),
            }
        )

    raw_prices = [result["predicted_price"] for result in results]
    bounded_prices = _apply_forecast_guardrail(raw_prices)

    for result, bounded_price in zip(results, bounded_prices):
        result["predicted_price_raw"] = result["predicted_price"]
        result["predicted_price"] = bounded_price
        result["guardrail_applied"] = abs(bounded_price - result["predicted_price_raw"]) > 0.01

    return results
