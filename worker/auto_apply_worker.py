"""
Auto-apply worker: polls price_update_jobs and writes Airbnb prices.

Usage:
    python -m worker.auto_apply_worker
"""

from __future__ import annotations

import logging
import os
import re
import signal
import threading
import time
import uuid
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from worker.core import db as db_helpers
from worker.core.auto_price_assignment import assign_prices_calendar

# Load .env from worker directory or repo root.
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)
load_dotenv(override=False)

POLL_SECONDS = int(os.getenv("AUTO_APPLY_POLL_SECONDS", "5"))
STALE_MINUTES = int(os.getenv("AUTO_APPLY_STALE_MINUTES", "15"))
MAX_ATTEMPTS = int(os.getenv("AUTO_APPLY_MAX_ATTEMPTS", "3"))
CDP_URL = os.getenv("AUTO_APPLY_CDP_URL", os.getenv("CDP_URL", "http://127.0.0.1:9222"))

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("worker.auto_apply")

_shutdown_event = threading.Event()


def _signal_handler(sig, _frame):
    logger.info("Received signal %s, shutting down...", sig)
    _shutdown_event.set()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _normalize_calendar(raw_calendar: Any) -> Dict[str, int]:
    if not isinstance(raw_calendar, dict):
        raise ValueError("calendar payload is not an object")

    normalized: Dict[str, int] = {}
    for date, price in raw_calendar.items():
        if not isinstance(date, str):
            raise ValueError("calendar date keys must be strings")
        if price is None:
            continue
        normalized[date] = int(round(float(price)))
    return normalized


def _extract_airbnb_listing_id_from_url(listing_url: str | None) -> str | None:
    if not listing_url:
        return None
    m = re.search(r"/rooms/(\d+)", str(listing_url))
    return m.group(1) if m else None


def _resolve_airbnb_listing_id(client, saved_listing_id: str) -> str | None:
    try:
        row = (
            client.table("saved_listings")
            .select("input_attributes")
            .eq("id", saved_listing_id)
            .single()
            .execute()
        )
        data = row.data or {}
        attrs = data.get("input_attributes") or {}
        listing_url = attrs.get("listingUrl") or attrs.get("listing_url")
        return _extract_airbnb_listing_id_from_url(listing_url)
    except Exception:
        return None


def process_job(job: Dict[str, Any], worker_token: uuid.UUID, client) -> None:
    job_id = str(job["id"])
    saved_listing_id = str(job.get("listing_id") or "")
    attempts = int(job.get("worker_attempts") or 0)

    if not saved_listing_id:
        db_helpers.fail_price_update_job(
            client,
            job_id,
            worker_token,
            error_message="Missing listing_id on queued job.",
            result_payload={"ok": False, "error": "missing listing_id"},
        )
        return

    listing_id = _resolve_airbnb_listing_id(client, saved_listing_id)
    if not listing_id:
        db_helpers.fail_price_update_job(
            client,
            job_id,
            worker_token,
            error_message=(
                "Unable to resolve Airbnb listing id from saved listing URL. "
                "Ensure input_attributes.listingUrl is a valid airbnb.com/rooms/{id} URL."
            ),
            result_payload={"ok": False, "error": "missing_airbnb_listing_id"},
        )
        return

    if attempts > MAX_ATTEMPTS:
        db_helpers.fail_price_update_job(
            client,
            job_id,
            worker_token,
            error_message=f"Job exceeded max attempts ({attempts}).",
            result_payload={"ok": False, "error": "max attempts exceeded"},
        )
        return

    try:
        calendar = _normalize_calendar(job.get("calendar"))
        if not calendar:
            db_helpers.fail_price_update_job(
                client,
                job_id,
                worker_token,
                error_message="No calendar prices found in queued job.",
                result_payload={"ok": False, "error": "empty calendar"},
            )
            return

        logger.info(
            "[%s] Applying %s prices for saved_listing=%s (airbnb_listing_id=%s)",
            job_id,
            len(calendar),
            saved_listing_id,
            listing_id,
        )
        result = assign_prices_calendar(
            listing_id=listing_id,
            calendar=calendar,
            cdp_url=CDP_URL,
        )

        if result.get("ok"):
            db_helpers.complete_price_update_job(
                client,
                job_id,
                worker_token,
                result_payload=result,
            )
            logger.info("[%s] Completed successfully", job_id)
            return

        error_message = str(result.get("error") or "Price assignment failed.")
        db_helpers.fail_price_update_job(
            client,
            job_id,
            worker_token,
            error_message=error_message[:500],
            result_payload=result,
        )
        logger.error("[%s] Failed: %s", job_id, error_message)

    except Exception as exc:
        db_helpers.fail_price_update_job(
            client,
            job_id,
            worker_token,
            error_message=f"Worker exception: {str(exc)[:400]}",
            result_payload={"ok": False, "error": str(exc)},
        )
        logger.exception("[%s] Unexpected worker error", job_id)


def main() -> None:
    logger.info(
        "Auto-apply worker started (poll=%ss stale=%sm cdp=%s)",
        POLL_SECONDS,
        STALE_MINUTES,
        CDP_URL,
    )

    client = db_helpers.get_client()

    backoff = POLL_SECONDS
    max_backoff = max(POLL_SECONDS, POLL_SECONDS * 12)

    while not _shutdown_event.is_set():
        try:
            worker_token = uuid.uuid4()
            job: Optional[Dict[str, Any]] = db_helpers.claim_price_update_job(
                client, worker_token, STALE_MINUTES
            )

            if job is None:
                _shutdown_event.wait(backoff)
                backoff = min(int(backoff * 1.5) or 1, max_backoff)
                continue

            backoff = POLL_SECONDS
            logger.info("[%s] Claimed job", job["id"])
            process_job(job, worker_token, client)

        except Exception:
            logger.exception("Worker loop error")
            _shutdown_event.wait(backoff)
            backoff = min(int(backoff * 2) or 1, max_backoff)

    logger.info("Auto-apply worker stopped.")


if __name__ == "__main__":
    main()
