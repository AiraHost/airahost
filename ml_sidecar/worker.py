from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
import sklearn
import xgboost
from xgboost import XGBRegressor

from ml_sidecar.batch_pipeline import execute_batch_workflow
from ml_sidecar.data import normalize_training_scope
from ml_sidecar.supabase_client import get_client

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
load_dotenv(ROOT / ".env.local", override=False)
load_dotenv(ROOT / "ml_sidecar" / ".env", override=False)

POLL_SECONDS = int(os.getenv("ML_SIDECAR_POLL_SECONDS", "5"))
STALE_MINUTES = int(os.getenv("ML_SIDECAR_STALE_MINUTES", "15"))
MAX_ATTEMPTS = int(os.getenv("ML_SIDECAR_MAX_ATTEMPTS", "3"))
DEFAULT_HORIZON = int(os.getenv("ML_SIDECAR_DEFAULT_HORIZON", "30"))
WORKER_VERSION = os.getenv("ML_SIDECAR_WORKER_VERSION", "ml-sidecar-worker-0.1.1")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

_log_dir = Path(__file__).resolve().parent / "logs"
_log_dir.mkdir(exist_ok=True)

_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
if not _root_logger.handlers:
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    _root_logger.addHandler(console)

file_handler = logging.handlers.RotatingFileHandler(
    filename=str(_log_dir / "ml_sidecar_worker.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
_root_logger.addHandler(file_handler)

logger = logging.getLogger("ml_sidecar.worker")
_shutdown_event = threading.Event()


def _signal_handler(sig: int, _frame: Any) -> None:
    logger.info("Received signal %s, shutting down gracefully...", sig)
    _shutdown_event.set()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _build_ml_forecast_payload(
    *,
    job: Dict[str, Any],
    status: str,
    created_at: Optional[str] = None,
    completed_at: Optional[str] = None,
    manifest: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> Dict[str, Any]:
    manifest = manifest or {}
    metrics = manifest.get("metrics") if isinstance(manifest.get("metrics"), dict) else None
    explanation = (
        manifest.get("explanation") if isinstance(manifest.get("explanation"), dict) else None
    )
    predictions = manifest.get("predictions") if isinstance(manifest.get("predictions"), list) else []

    return {
        "id": job["id"],
        "jobId": job["id"],
        "reportId": job["source_report_id"],
        "status": status,
        "trainingScope": manifest.get("training_scope") or job.get("training_scope"),
        "modelMode": manifest.get("model_mode"),
        "nSamples": manifest.get("n_samples"),
        "generatedAt": manifest.get("generated_at"),
        "createdAt": created_at or job.get("created_at"),
        "completedAt": completed_at,
        "errorMessage": error_message,
        "metrics": metrics,
        "explanation": explanation,
        "predictions": predictions,
        "workerVersion": WORKER_VERSION,
    }


def _merge_report_ml_forecast(
    client: Any,
    *,
    source_report_id: str,
    payload: Dict[str, Any],
) -> None:
    result = (
        client.table("pricing_reports")
        .select("result_summary")
        .eq("id", source_report_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    summary = _as_dict(rows[0].get("result_summary") if rows else {})
    summary["mlForecast"] = payload
    client.table("pricing_reports").update({"result_summary": summary}).eq(
        "id", source_report_id
    ).execute()


def _normalize_report_job(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    summary = _as_dict(row.get("result_summary"))
    ml_forecast = _as_dict(summary.get("mlForecast"))
    if ml_forecast.get("status") != "queued":
        return None

    listing_id = row.get("listing_id")
    report_id = row.get("id")
    if not listing_id or not report_id:
        return None

    return {
        "id": str(ml_forecast.get("jobId") or ml_forecast.get("id") or report_id),
        "listing_id": str(listing_id),
        "source_report_id": str(report_id),
        "training_scope": str(ml_forecast.get("trainingScope") or "global"),
        "horizon": DEFAULT_HORIZON,
        "force_retrain": (
            ml_forecast.get("forceRetrain") is True
            or os.getenv("ML_SIDECAR_FORCE_RETRAIN", "").strip() == "1"
        ),
        "created_at": ml_forecast.get("createdAt") or row.get("created_at"),
        "report_summary": summary,
    }


def _claim_job(client: Any) -> Optional[Dict[str, Any]]:
    result = (
        client.table("pricing_reports")
        .select("id,listing_id,created_at,result_summary")
        .eq("status", "ready")
        .contains("result_summary", {"mlForecast": {"status": "queued"}})
        .order("created_at", desc=False)
        .limit(10)
        .execute()
    )

    for row in result.data or []:
        job = _normalize_report_job(row)
        if not job:
            continue

        running_payload = _build_ml_forecast_payload(
            job=job,
            status="running",
            created_at=job.get("created_at"),
            completed_at=None,
        )
        summary = _as_dict(row.get("result_summary"))
        summary["mlForecast"] = running_payload
        client.table("pricing_reports").update({"result_summary": summary}).eq(
            "id", job["source_report_id"]
        ).execute()
        return job

    return None


def _complete_job(
    client: Any,
    *,
    job: Dict[str, Any],
    manifest: Dict[str, Any],
) -> None:
    completed_at = _now_iso()
    payload = _build_ml_forecast_payload(
        job=job,
        status="ready",
        created_at=job.get("created_at"),
        completed_at=completed_at,
        manifest=manifest,
    )

    _merge_report_ml_forecast(
        client,
        source_report_id=job["source_report_id"],
        payload=payload,
    )


def _fail_job(
    client: Any,
    *,
    job: Dict[str, Any],
    error_message: str,
) -> None:
    completed_at = _now_iso()
    clipped = error_message[:1000]
    payload = _build_ml_forecast_payload(
        job=job,
        status="error",
        created_at=job.get("created_at"),
        completed_at=completed_at,
        error_message=clipped,
    )

    _merge_report_ml_forecast(
        client,
        source_report_id=job["source_report_id"],
        payload=payload,
    )


def _process_job(client: Any, job: Dict[str, Any]) -> None:
    job_id = str(job["id"])

    training_scope = normalize_training_scope(str(job.get("training_scope") or "global"))
    horizon = int(job.get("horizon") or DEFAULT_HORIZON)
    force_retrain = bool(job.get("force_retrain"))
    logger.info(
        "[%s] running ML forecast listing=%s report=%s scope=%s horizon=%s force_retrain=%s",
        job_id,
        job.get("listing_id"),
        job.get("source_report_id"),
        training_scope,
        horizon,
        force_retrain,
    )

    running_payload = _build_ml_forecast_payload(
        job=job,
        status="running",
        created_at=job.get("created_at"),
        completed_at=None,
    )
    _merge_report_ml_forecast(
        client,
        source_report_id=job["source_report_id"],
        payload=running_payload,
    )

    manifest = execute_batch_workflow(
        saved_listing_id=str(job["listing_id"]),
        training_scope=training_scope,
        force_train=force_retrain,
        reuse_model=not force_retrain,
        predictions_output_path=Path("ml_sidecar") / "reports" / f"predictions_{job_id}.csv",
        manifest_output_path=Path("ml_sidecar") / "reports" / f"manifest_{job_id}.json",
        model_path=Path("ml_sidecar") / "reports" / "saved_model.json",
        horizon=horizon,
        start_date=date.today(),
    )
    _complete_job(client, job=job, manifest=manifest)
    logger.info("[%s] completed ML forecast", job_id)


def main() -> None:
    logger.info(
        "ML sidecar worker starting version=%s poll=%ss queue=pricing_reports.result_summary.mlForecast",
        WORKER_VERSION,
        POLL_SECONDS,
    )
    logger.info(
        "ML runtime sklearn=%s xgboost=%s xgb_regressor_type=%s",
        sklearn.__version__,
        xgboost.__version__,
        getattr(XGBRegressor, "_estimator_type", None),
    )
    client = get_client()

    while not _shutdown_event.is_set():
        try:
            job = _claim_job(client)
        except Exception as exc:
            logger.error("ML forecast claim failed: %s", exc)
            _shutdown_event.wait(POLL_SECONDS)
            continue

        if not job:
            _shutdown_event.wait(POLL_SECONDS)
            continue

        try:
            _process_job(client, job)
        except Exception as exc:
            logger.exception("[%s] ML forecast failed", job.get("id"))
            try:
                _fail_job(
                    client,
                    job=job,
                    error_message=str(exc),
                )
            except Exception:
                logger.exception("[%s] failed to persist ML error", job.get("id"))

    logger.info("ML sidecar worker stopped")


if __name__ == "__main__":
    main()
