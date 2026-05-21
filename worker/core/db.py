"""
Supabase client helpers for the worker.

All DB access uses the service role key, which bypasses RLS.
This key must NEVER be exposed to the frontend.
"""

from __future__ import annotations

import importlib
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("worker.core.db")


def _load_supabase_client_symbols() -> tuple[Any, Any]:
    """Avoid the repo's local /supabase folder shadowing the installed package."""
    repo_root = Path(__file__).resolve().parents[2]
    local_supabase_dir = repo_root / "supabase"

    try:
        supabase_module = importlib.import_module("supabase")
    except ModuleNotFoundError:
        raise

    if hasattr(supabase_module, "create_client") and hasattr(supabase_module, "Client"):
        return supabase_module.create_client, supabase_module.Client

    if local_supabase_dir.is_dir():
        original_sys_path = list(sys.path)
        try:
            sys.path = [
                entry
                for entry in sys.path
                if Path(entry or ".").resolve() != repo_root
            ]
            sys.modules.pop("supabase", None)
            supabase_module = importlib.import_module("supabase")
            if hasattr(supabase_module, "create_client") and hasattr(supabase_module, "Client"):
                return supabase_module.create_client, supabase_module.Client
        finally:
            sys.path = original_sys_path

    raise ImportError("Installed Python package 'supabase' with Client/create_client was not found.")


import sys

create_client, Client = _load_supabase_client_symbols()


def get_client() -> Client:
    """Create a Supabase client using the service role key."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY for worker. "
            "Refusing to start without service-role credentials."
        )
    return create_client(url, key)


def claim_job(client: Client, worker_token: uuid.UUID, stale_minutes: int, target_env: str, job_lane: str = "interactive") -> Optional[Dict[str, Any]]:
    """
    Atomically claim one queued (or stale-running) job matching target_env and job_lane.
    Returns the full row dict, or None if no work available.
    """
    try:
        # New signature (migration 012): lane + env aware.
        result = client.rpc(
            "claim_pricing_report",
            {
                "p_worker_token": str(worker_token),
                "p_stale_minutes": stale_minutes,
                "p_job_lane": job_lane,
                "p_target_env": target_env,
            },
        ).execute()
    except Exception as exc:
        # Backward compatibility with older DBs that only have:
        # claim_pricing_report(p_worker_token, p_stale_minutes)
        msg = str(exc)
        is_signature_mismatch = (
            "PGRST202" in msg
            and "claim_pricing_report" in msg
            and ("p_job_lane" in msg or "p_target_env" in msg)
        )
        if not is_signature_mismatch:
            raise

        logger.warning(
            "claim_pricing_report lane/env signature not found; "
            "falling back to legacy 2-arg RPC. Apply migration 012 to enable lane/env isolation."
        )
        result = client.rpc(
            "claim_pricing_report",
            {
                "p_worker_token": str(worker_token),
                "p_stale_minutes": stale_minutes,
            },
        ).execute()

    rows = result.data
    if rows and len(rows) > 0:
        return rows[0]
    return None


def list_queued_nightly_jobs(client: Client, target_env: str, limit: int = 500) -> List[Dict[str, Any]]:
    """
    Return queued nightly pricing_reports for the given target_env (oldest first).
    """
    result = (
        client.table("pricing_reports")
        .select("id,input_date_start,created_at,worker_claimed_at,status,job_lane,target_env")
        .eq("status", "queued")
        .eq("job_lane", "nightly")
        .eq("target_env", target_env)
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )
    return list(result.data or [])


def utc_date_from_iso(raw_ts: Optional[str]) -> Optional[str]:
    if not raw_ts:
        return None
    try:
        text = str(raw_ts).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()
    except Exception:
        return None


def heartbeat(client: Client, report_id: str, worker_token: uuid.UUID) -> bool:
    """Update heartbeat timestamp. Returns True if the token still owns the job."""
    result = client.rpc(
        "heartbeat_pricing_report",
        {"p_report_id": report_id, "p_worker_token": str(worker_token)},
    ).execute()
    return result.data is True


def complete_job(
    client: Client,
    report_id: str,
    worker_token: uuid.UUID,
    *,
    summary: Dict[str, Any],
    calendar: list,
    core_version: str,
    debug: Optional[Dict[str, Any]] = None,
    input_attributes: Optional[Dict[str, Any]] = None,
    input_address: Optional[str] = None,
    input_listing_url: Optional[str] = None,
    write_input_listing_url: bool = False,
    discount_policy: Optional[Dict[str, Any]] = None,
    cache_key: Optional[str] = None,
    source_market_captured_at: Optional[str] = None,
    queue_ml_forecast: bool = False,
    ml_training_scope: str = "global",
    ml_force_retrain: bool = True,
) -> None:
    """Mark a job as ready with results. Idempotent — overwrites existing results.

    Sets completed_at to now.  market_captured_at is set to now for fresh
    scrapes (live_analysis).  For forecast_snapshot reports, pass the source
    live_analysis report's market_captured_at as source_market_captured_at so
    freshness reflects when the underlying market data was actually captured.

    For nightly jobs that reload saved listing data at execution time, pass
    input_address, input_listing_url (with write_input_listing_url=True),
    discount_policy, and cache_key so the completed report row fully reflects
    actual execution inputs rather than the stale queued snapshot.

    write_input_listing_url=True: write input_listing_url unconditionally,
    including clearing to NULL when listing_url is None.  This separates
    "parameter not provided" (False, skip update) from "explicitly no URL" (True+None).
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    update: Dict[str, Any] = {
        "status": "ready",
        "core_version": core_version,
        "result_summary": summary,
        "result_calendar": calendar,
        "error_message": None,
        # Explicit freshness timestamps (migration 010)
        "completed_at": now,
        # For forecast_snapshot: inherit market_captured_at from source live_analysis.
        # For live_analysis (fresh scrape): capture time == completion time.
        "market_captured_at": source_market_captured_at if source_market_captured_at is not None else now,
    }
    if debug:
        update["result_core_debug"] = debug
    if input_attributes is not None:
        update["input_attributes"] = input_attributes
    if input_address is not None:
        update["input_address"] = input_address
    # write_input_listing_url=True writes the value unconditionally (including None → clears to NULL).
    # Default False preserves existing behaviour: skip update when value is None.
    if write_input_listing_url:
        update["input_listing_url"] = input_listing_url
    elif input_listing_url is not None:
        update["input_listing_url"] = input_listing_url
    if discount_policy is not None:
        update["discount_policy"] = discount_policy
    if cache_key is not None:
        update["cache_key"] = cache_key

    client.table("pricing_reports").update(update).eq("id", report_id).eq(
        "worker_claim_token", str(worker_token)
    ).execute()

    try:
        client.rpc(
            "ingest_market_price_observations",
            {"p_report_id": report_id},
        ).execute()
    except Exception as exc:
        logger.warning(
            f"[{report_id}] market observation ingestion failed after report completion: {exc}"
        )

    if queue_ml_forecast:
        try:
            import uuid

            queued_at = datetime.now(timezone.utc).isoformat()
            result = (
                client.table("pricing_reports")
                .select("result_summary")
                .eq("id", report_id)
                .limit(1)
                .execute()
            )
            rows = result.data or []
            current_summary = rows[0].get("result_summary") if rows else summary
            if not isinstance(current_summary, dict):
                current_summary = dict(summary or {})

            existing_ml = current_summary.get("mlForecast")
            if not isinstance(existing_ml, dict) or existing_ml.get("status") not in ("queued", "running"):
                job_id = str(uuid.uuid4())
                current_summary["mlForecast"] = {
                    "id": job_id,
                    "jobId": job_id,
                    "reportId": report_id,
                    "status": "queued",
                    "trainingScope": ml_training_scope,
                    "modelMode": None,
                    "nSamples": None,
                    "generatedAt": None,
                    "createdAt": queued_at,
                    "completedAt": None,
                    "errorMessage": None,
                    "metrics": None,
                    "explanation": None,
                    "predictions": [],
                    "forceRetrain": bool(ml_force_retrain),
                    "queuedBy": "nightly_worker",
                }
                client.table("pricing_reports").update(
                    {"result_summary": current_summary}
                ).eq("id", report_id).execute()
                logger.info(
                    "[%s] queued ML forecast after nightly observation ingest (scope=%s force_retrain=%s)",
                    report_id,
                    ml_training_scope,
                    ml_force_retrain,
                )
        except Exception as exc:
            logger.warning(f"[{report_id}] ML forecast queueing failed after nightly completion: {exc}")


def sync_linked_listing_attributes(
    client: Client,
    report_id: str,
    input_attributes: Dict[str, Any],
) -> None:
    """Update linked saved listing attributes from finalized report attributes."""
    link_result = (
        client.table("listing_reports")
        .select("saved_listing_id")
        .eq("pricing_report_id", report_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = link_result.data or []
    if not rows:
        return

    listing_id = rows[0].get("saved_listing_id")
    if not listing_id:
        return

    client.table("saved_listings").update(
        {"input_attributes": input_attributes}
    ).eq("id", listing_id).execute()


def update_progress(
    client: Client,
    report_id: str,
    worker_token: uuid.UUID,
    *,
    pct: int,
    stage: str,
    message: str,
    est_seconds_remaining: Optional[int] = None,
) -> bool:
    """Update heartbeat + progress metadata atomically.

    Returns True if the token still owns the job (row was updated).
    Safe to call from both the heartbeat thread and the main thread.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    meta: Dict[str, Any] = {
        "pct": max(0, min(100, pct)),
        "stage": stage,
        "message": message,
        "updated_at": now,
    }
    if est_seconds_remaining is not None:
        meta["est_seconds_remaining"] = max(0, est_seconds_remaining)
    result = (
        client.table("pricing_reports")
        .update({
            "worker_heartbeat_at": now,
            "progress_meta": meta,
        })
        .eq("id", report_id)
        .eq("worker_claim_token", str(worker_token))
        .execute()
    )
    return bool(result.data)


def fail_job(
    client: Client,
    report_id: str,
    worker_token: uuid.UUID,
    *,
    error_message: str,
    debug: Optional[Dict[str, Any]] = None,
) -> None:
    """Mark a job as error with a user-friendly message."""
    update: Dict[str, Any] = {
        "status": "error",
        "error_message": error_message,
    }
    if debug:
        update["result_core_debug"] = debug

    client.table("pricing_reports").update(update).eq("id", report_id).eq(
        "worker_claim_token", str(worker_token)
    ).execute()


def claim_price_update_job(
    client: Client, worker_token: uuid.UUID, stale_minutes: int
) -> Optional[Dict[str, Any]]:
    """
    Atomically claim one queued (or stale-running) price update job.
    Returns the full row dict, or None if no work is available.
    """
    result = client.rpc(
        "claim_price_update_job",
        {
            "p_worker_token": str(worker_token),
            "p_stale_minutes": stale_minutes,
        },
    ).execute()

    rows = result.data
    if rows and len(rows) > 0:
        return rows[0]
    return None


def complete_price_update_job(
    client: Client,
    job_id: str,
    worker_token: uuid.UUID,
    *,
    result_payload: Dict[str, Any],
) -> None:
    """Mark a claimed price update job as ready with result payload."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    client.table("price_update_jobs").update(
        {
            "status": "ready",
            "result": result_payload,
            "error_message": None,
            "completed_at": now,
            "updated_at": now,
            "worker_heartbeat_at": now,
        }
    ).eq("id", job_id).eq("worker_claim_token", str(worker_token)).execute()


def fail_price_update_job(
    client: Client,
    job_id: str,
    worker_token: uuid.UUID,
    *,
    error_message: str,
    result_payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Mark a claimed price update job as error."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    update: Dict[str, Any] = {
        "status": "error",
        "error_message": error_message,
        "completed_at": now,
        "updated_at": now,
        "worker_heartbeat_at": now,
    }
    if result_payload is not None:
        update["result"] = result_payload

    client.table("price_update_jobs").update(update).eq("id", job_id).eq(
        "worker_claim_token", str(worker_token)
    ).execute()
