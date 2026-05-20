"""
Live end-to-end scraper smoke test for both worker lanes.

Runs one interactive job and one nightly job through the real worker
dispatcher (`process_job`) and exits non-zero if scraper health checks fail.

Usage:
    python -m worker.e2e_scraper_smoke
    python -m worker.e2e_scraper_smoke --listing-url https://www.airbnb.ca/rooms/1408676238256636483
"""

from __future__ import annotations

import argparse
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv

# Keep shell env vars as highest priority.
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)
load_dotenv(override=False)

from worker.core import db as db_helpers
from worker.main import process_job


@dataclass
class LaneOutcome:
    lane: str
    report_id: str
    share_id: str
    status: str
    core_version: str
    comps_used: int
    live_price_status: str
    failures: List[str]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return parsed


def _build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run live scraper smoke tests for both interactive and nightly worker lanes."
    )
    parser.add_argument(
        "--listing-url",
        default=os.getenv("WORKER_E2E_LISTING_URL", "https://www.airbnb.ca/rooms/1408676238256636483"),
        help="Airbnb room URL to scrape (default from WORKER_E2E_LISTING_URL or built-in fallback).",
    )
    parser.add_argument(
        "--input-address",
        default=os.getenv("WORKER_E2E_INPUT_ADDRESS", "Belmont, CA, United States"),
        help="Input address stored on the synthetic report rows.",
    )
    parser.add_argument(
        "--start-offset-days",
        type=int,
        default=int(os.getenv("WORKER_E2E_START_OFFSET_DAYS", "14")),
        help="Start date offset from today (UTC).",
    )
    parser.add_argument(
        "--nights",
        type=_positive_int,
        default=int(os.getenv("WORKER_E2E_NIGHTS", "3")),
        help="Number of nights to scrape per lane.",
    )
    parser.add_argument(
        "--min-comps-used",
        type=int,
        default=int(os.getenv("WORKER_E2E_MIN_COMPS_USED", "1")),
        help="Fail when usedForPricing comps are below this number (default 1).",
    )
    parser.add_argument(
        "--allow-target-only-fallback",
        action="store_true",
        help="Do not fail when target_only_fallback is used.",
    )
    parser.add_argument(
        "--allow-self-only-core-version",
        action="store_true",
        help="Do not fail if core_version indicates self-only fallback.",
    )
    parser.add_argument(
        "--allow-live-price-scrape-failed",
        action="store_true",
        help="Do not fail when summary.livePriceStatus is scrape_failed.",
    )
    parser.add_argument(
        "--keep-reports",
        action="store_true",
        help="Keep generated pricing_reports rows instead of deleting them at the end.",
    )
    parser.add_argument(
        "--claim-timeout-seconds",
        type=_positive_int,
        default=int(os.getenv("WORKER_E2E_CLAIM_TIMEOUT_SECONDS", "20")),
        help="Max seconds to wait for claiming each synthetic job.",
    )
    return parser.parse_args()


def _date_window(start_offset_days: int, nights: int) -> Tuple[str, str]:
    start = datetime.utcnow().date() + timedelta(days=start_offset_days)
    end = start + timedelta(days=nights)
    return start.isoformat(), end.isoformat()


def _make_report_payload(
    *,
    lane: str,
    policy: str,
    listing_url: str,
    input_address: str,
    start_date: str,
    end_date: str,
    target_env: str,
) -> Dict[str, Any]:
    report_id = str(uuid.uuid4())
    share_id = f"e2e-{lane}-{uuid.uuid4().hex[:20]}"
    input_attributes = {
        "inputMode": "url",
        "listingUrl": listing_url,
        "lastMinuteStrategy": {
            "mode": "auto",
            "aggressiveness": 50,
            "floor": 0.65,
            "cap": 1.05,
        },
    }
    return {
        "id": report_id,
        "share_id": share_id,
        "report_type": "live_analysis",
        "input_address": input_address,
        "target_env": target_env,
        "job_lane": lane,
        "input_attributes": input_attributes,
        "input_date_start": start_date,
        "input_date_end": end_date,
        "discount_policy": {},
        "input_listing_url": listing_url,
        "cache_key": f"e2e-{lane}-{uuid.uuid4().hex}",
        "status": "queued",
        "core_version": "pending",
        "result_summary": None,
        "result_calendar": None,
        "error_message": None,
        "result_core_debug": {
            "execution_policy": policy,
            "cache_hit": False,
            "force_rerun": True,
            "request_source": "worker.e2e_scraper_smoke",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }


def _claim_exact_job(
    client: Any,
    *,
    report_id: str,
    lane: str,
    target_env: str,
    timeout_seconds: int,
) -> Tuple[Dict[str, Any], uuid.UUID]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        worker_token = uuid.uuid4()
        claimed = db_helpers.claim_job(
            client,
            worker_token=worker_token,
            stale_minutes=15,
            target_env=target_env,
            job_lane=lane,
        )
        if claimed is None:
            time.sleep(0.5)
            continue
        if str(claimed.get("id")) == report_id:
            return claimed, worker_token
        # Unique target_env should prevent this, but fail loudly if it happens.
        raise RuntimeError(
            f"Claimed unexpected report id={claimed.get('id')} while waiting for {report_id}"
        )
    raise TimeoutError(f"Timed out claiming report {report_id} in lane={lane}")


def _fetch_report_row(client: Any, report_id: str) -> Dict[str, Any]:
    res = (
        client.table("pricing_reports")
        .select("id, share_id, status, core_version, error_message, result_summary, result_core_debug")
        .eq("id", report_id)
        .single()
        .execute()
    )
    if not res.data:
        raise RuntimeError(f"Report row not found after run: {report_id}")
    return res.data


def _extract_comps_used(summary: Dict[str, Any]) -> int:
    comps_summary = summary.get("compsSummary")
    if not isinstance(comps_summary, dict):
        return 0
    raw = comps_summary.get("usedForPricing")
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        return int(raw)
    return 0


def _evaluate_lane(row: Dict[str, Any], lane: str, args: argparse.Namespace) -> LaneOutcome:
    status = str(row.get("status") or "")
    core_version = str(row.get("core_version") or "")
    summary = row.get("result_summary") if isinstance(row.get("result_summary"), dict) else {}
    debug = row.get("result_core_debug") if isinstance(row.get("result_core_debug"), dict) else {}

    failures: List[str] = []
    if status != "ready":
        failures.append(f"status={status} (expected ready), error_message={row.get('error_message')!r}")

    if (not args.allow_self_only_core_version) and "self_only" in core_version:
        failures.append(f"core_version={core_version} indicates self-only fallback")

    if (not args.allow_target_only_fallback) and bool(debug.get("target_only_fallback")):
        failures.append("result_core_debug.target_only_fallback=true")

    if debug.get("error"):
        failures.append(f"result_core_debug.error present: {debug.get('error')!r}")

    comps_used = _extract_comps_used(summary)
    if comps_used < args.min_comps_used:
        failures.append(
            f"compsSummary.usedForPricing={comps_used} is below min {args.min_comps_used}"
        )

    live_price_status = str(summary.get("livePriceStatus") or "")
    if (not args.allow_live_price_scrape_failed) and live_price_status == "scrape_failed":
        failures.append("summary.livePriceStatus=scrape_failed")

    return LaneOutcome(
        lane=lane,
        report_id=str(row.get("id")),
        share_id=str(row.get("share_id") or ""),
        status=status,
        core_version=core_version,
        comps_used=comps_used,
        live_price_status=live_price_status,
        failures=failures,
    )


def _run_lane(
    client: Any,
    *,
    lane: str,
    policy: str,
    listing_url: str,
    input_address: str,
    start_date: str,
    end_date: str,
    target_env: str,
    claim_timeout_seconds: int,
) -> Dict[str, Any]:
    payload = _make_report_payload(
        lane=lane,
        policy=policy,
        listing_url=listing_url,
        input_address=input_address,
        start_date=start_date,
        end_date=end_date,
        target_env=target_env,
    )

    client.table("pricing_reports").insert(payload).execute()
    claimed, worker_token = _claim_exact_job(
        client,
        report_id=payload["id"],
        lane=lane,
        target_env=target_env,
        timeout_seconds=claim_timeout_seconds,
    )

    process_job(claimed, worker_token)
    return _fetch_report_row(client, payload["id"])


def _delete_reports(client: Any, report_ids: List[str]) -> None:
    if not report_ids:
        return
    client.table("pricing_reports").delete().in_("id", report_ids).execute()


def main() -> int:
    args = _build_args()

    if "/rooms/" not in args.listing_url:
        print("ERROR: --listing-url must be an Airbnb room URL containing /rooms/{id}")
        return 2

    supabase_url = str(os.getenv("SUPABASE_URL", "")).strip()
    supabase_key = str(os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")).strip()
    if not supabase_url or not supabase_key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")
        return 2

    start_date, end_date = _date_window(args.start_offset_days, args.nights)
    client = db_helpers.get_client()

    # Isolate claims to this run so no other queued jobs interfere.
    run_target_env = f"e2e-smoke-{uuid.uuid4().hex[:10]}"
    created_report_ids: List[str] = []

    outcomes: List[LaneOutcome] = []
    lane_plan = [
        ("interactive", "interactive_live_report"),
        ("nightly", "nightly_board_refresh"),
    ]

    try:
        for lane, policy in lane_plan:
            print(f"\n=== Running {lane} lane ===")
            row = _run_lane(
                client,
                lane=lane,
                policy=policy,
                listing_url=args.listing_url,
                input_address=args.input_address,
                start_date=start_date,
                end_date=end_date,
                target_env=run_target_env,
                claim_timeout_seconds=args.claim_timeout_seconds,
            )
            created_report_ids.append(str(row["id"]))
            outcome = _evaluate_lane(row, lane, args)
            outcomes.append(outcome)

            print(
                f"{lane}: status={outcome.status} core_version={outcome.core_version} "
                f"comps_used={outcome.comps_used} live_price_status={outcome.live_price_status} "
                f"report_id={outcome.report_id} share_id={outcome.share_id}"
            )
            if outcome.failures:
                for failure in outcome.failures:
                    print(f"  FAIL: {failure}")
            else:
                print("  PASS")
    finally:
        if created_report_ids and not args.keep_reports:
            _delete_reports(client, created_report_ids)
            print(f"\nDeleted generated reports: {', '.join(created_report_ids)}")

    failed_outcomes = [o for o in outcomes if o.failures]
    if failed_outcomes:
        print("\nScraper E2E smoke test failed.")
        return 1

    print("\nScraper E2E smoke test passed for both lanes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
