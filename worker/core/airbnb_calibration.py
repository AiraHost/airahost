"""Bounded probe for finding a *deployment's* safe request envelope.

There is no published, universal Airbnb rate limit, and this tool does not
discover one. What it does is measure how the current deployment — this host,
this session, these endpoints, right now — behaves at increasing request rates,
and stop the moment Airbnb pushes back.

Safety is the design, not a mode:

  * Starts at one in-flight request and at least one second between starts.
  * Increases only after a full window of healthy responses, one step at a time.
  * Hard caps on total requests, wall-clock duration, concurrency and target
    rate. Every cap is checked before each request, so no cap can be overshot.
  * Aborts into cooldown at the first configured threshold of 429/503/challenge
    responses. It never "pushes through" to find the edge.
  * Disabled by default. Running it requires ``AIRBNB_CALIBRATION_ENABLED=1``
    *and* an explicit acknowledgement flag on the command line.

The report it writes is machine-readable and its recommendation applies a safety
margin below the highest healthy envelope actually observed. Treat it as a
snapshot, not a permanent limit: the runtime admission policy stays adaptive
regardless of what this says.

Unit tests drive it through an injected probe function and make no network call.

Usage::

    AIRBNB_CALIBRATION_ENABLED=1 python -m worker.core.airbnb_calibration \\
        --i-understand-live-traffic --max-requests 60 --max-seconds 120
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from worker.core import scrape_events
from worker.core.admission import (
    OUTCOME_BLOCKED,
    OUTCOME_OVERLOAD,
    OUTCOME_SUCCESS,
    OUTCOME_TRANSPORT_ERROR,
    classify_response_outcome,
)

# Absolute ceilings. Command-line values are clamped to these, so a typo in an
# argument cannot turn a calibration run into a load test.
ABSOLUTE_MAX_REQUESTS = 500
ABSOLUTE_MAX_SECONDS = 900.0
ABSOLUTE_MAX_CONCURRENCY = 8
ABSOLUTE_MAX_RATE_PER_SEC = 5.0

# Result of one probe call, as the injected probe function must return it.
ProbeResult = Dict[str, Any]
ProbeFn = Callable[[int], ProbeResult]


@dataclass
class CalibrationLimits:
    """Every bound the run must respect. Clamped on construction."""

    max_requests: int = 60
    max_seconds: float = 180.0
    max_concurrency: int = 4
    max_rate_per_sec: float = 2.0
    start_concurrency: int = 1
    start_interval_seconds: float = 1.0
    healthy_window: int = 10
    error_threshold: int = 3
    cooldown_seconds: float = 30.0

    def __post_init__(self) -> None:
        self.max_requests = max(1, min(int(self.max_requests), ABSOLUTE_MAX_REQUESTS))
        self.max_seconds = max(1.0, min(float(self.max_seconds), ABSOLUTE_MAX_SECONDS))
        self.max_concurrency = max(1, min(int(self.max_concurrency), ABSOLUTE_MAX_CONCURRENCY))
        self.max_rate_per_sec = max(
            0.05, min(float(self.max_rate_per_sec), ABSOLUTE_MAX_RATE_PER_SEC)
        )
        self.start_concurrency = max(1, min(int(self.start_concurrency), self.max_concurrency))
        # Never start faster than one request per second, whatever was asked for.
        self.start_interval_seconds = max(1.0, float(self.start_interval_seconds))
        self.healthy_window = max(1, min(int(self.healthy_window), 200))
        self.error_threshold = max(1, min(int(self.error_threshold), 50))
        self.cooldown_seconds = max(0.0, min(float(self.cooldown_seconds), 600.0))


@dataclass
class StepRecord:
    """One rate/concurrency step and what happened at it."""

    step: int
    target_rate_per_sec: float
    concurrency: int
    requests: int = 0
    successes: int = 0
    overloads: int = 0
    blocks: int = 0
    transport_errors: int = 0
    latencies_ms: List[float] = field(default_factory=list)
    actual_rate_per_sec: float = 0.0
    status_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.overloads == 0 and self.blocks == 0 and self.successes > 0

    def summary(self) -> Dict[str, Any]:
        lat = sorted(self.latencies_ms)
        return {
            "step": self.step,
            "target_rate_per_sec": round(self.target_rate_per_sec, 4),
            "concurrency": self.concurrency,
            "requests": self.requests,
            "successes": self.successes,
            "overloads": self.overloads,
            "blocks": self.blocks,
            "transport_errors": self.transport_errors,
            "actual_rate_per_sec": round(self.actual_rate_per_sec, 4),
            "latency_p50_ms": round(_percentile(lat, 50), 1) if lat else None,
            "latency_p95_ms": round(_percentile(lat, 95), 1) if lat else None,
            "status_counts": dict(self.status_counts),
            "healthy": self.healthy,
        }


def _percentile(sorted_values: List[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return float(sorted_values[low] * (1 - frac) + sorted_values[high] * frac)


def calibration_enabled() -> bool:
    return str(os.getenv("AIRBNB_CALIBRATION_ENABLED", "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


class CalibrationRun:
    """Executes one bounded ramp and produces a report.

    ``probe`` is called with the 1-based request index and must return a dict
    with ``status`` (int or None), ``elapsed_ms`` (float), and optionally
    ``blocked`` (bool) and ``error`` (str). Injecting it is what keeps unit
    tests off the network.
    """

    def __init__(self, probe: ProbeFn, limits: Optional[CalibrationLimits] = None) -> None:
        self.probe = probe
        self.limits = limits or CalibrationLimits()
        self.steps: List[StepRecord] = []
        self.stop_reason = "not_started"
        self.time_to_first_overload_seconds: Optional[float] = None
        self.recovery_seconds: Optional[float] = None
        self.total_requests = 0
        self._lock = threading.Lock()

    # ── ramp ─────────────────────────────────────────────────────────────────
    def run(self) -> Dict[str, Any]:
        started = time.monotonic()
        rate = min(1.0 / self.limits.start_interval_seconds, self.limits.max_rate_per_sec)
        concurrency = self.limits.start_concurrency
        step_index = 0
        consecutive_errors = 0
        first_overload_at: Optional[float] = None

        self.stop_reason = "completed_ramp"
        while True:
            if self.total_requests >= self.limits.max_requests:
                self.stop_reason = "max_requests_reached"
                break
            if time.monotonic() - started >= self.limits.max_seconds:
                self.stop_reason = "max_duration_reached"
                break

            step_index += 1
            record = StepRecord(
                step=step_index, target_rate_per_sec=rate, concurrency=concurrency
            )
            self.steps.append(record)
            step_started = time.monotonic()
            interval = 1.0 / max(rate, 0.001)

            for _ in range(self.limits.healthy_window):
                if self.total_requests >= self.limits.max_requests:
                    self.stop_reason = "max_requests_reached"
                    break
                if time.monotonic() - started >= self.limits.max_seconds:
                    self.stop_reason = "max_duration_reached"
                    break

                self.total_requests += 1
                outcome, status, elapsed_ms = self._one_request(self.total_requests)
                record.requests += 1
                record.latencies_ms.append(elapsed_ms)
                key = str(status) if status is not None else "none"
                record.status_counts[key] = record.status_counts.get(key, 0) + 1

                if outcome == OUTCOME_SUCCESS:
                    record.successes += 1
                    consecutive_errors = 0
                elif outcome == OUTCOME_OVERLOAD:
                    record.overloads += 1
                    consecutive_errors += 1
                    if first_overload_at is None:
                        first_overload_at = time.monotonic()
                        self.time_to_first_overload_seconds = round(first_overload_at - started, 3)
                elif outcome == OUTCOME_BLOCKED:
                    record.blocks += 1
                    consecutive_errors += 1
                else:
                    record.transport_errors += 1
                    consecutive_errors += 1

                if consecutive_errors >= self.limits.error_threshold:
                    self.stop_reason = "error_threshold_reached"
                    break
                time.sleep(interval)

            elapsed_step = max(1e-6, time.monotonic() - step_started)
            record.actual_rate_per_sec = record.requests / elapsed_step

            if self.stop_reason != "completed_ramp":
                break
            if not record.healthy:
                # Never push past a step that showed pressure.
                self.stop_reason = "unhealthy_step"
                break
            if rate >= self.limits.max_rate_per_sec and concurrency >= self.limits.max_concurrency:
                self.stop_reason = "reached_configured_ceiling"
                break

            # One step at a time: rate first, then concurrency.
            if rate < self.limits.max_rate_per_sec:
                rate = min(self.limits.max_rate_per_sec, rate * 1.5)
            else:
                concurrency = min(self.limits.max_concurrency, concurrency + 1)

        if self.stop_reason in ("error_threshold_reached", "unhealthy_step"):
            self._cooldown(started)
        return self.report(total_seconds=time.monotonic() - started)

    def _one_request(self, index: int) -> tuple:
        started = time.perf_counter()
        try:
            result = self.probe(index) or {}
        except Exception:  # noqa: BLE001 - a failing probe is data, not a crash
            return OUTCOME_TRANSPORT_ERROR, None, (time.perf_counter() - started) * 1000.0
        status = result.get("status")
        elapsed_ms = float(result.get("elapsed_ms", (time.perf_counter() - started) * 1000.0))
        outcome = classify_response_outcome(
            status,
            exception=RuntimeError(result["error"]) if result.get("error") else None,
            auth_blocked=bool(result.get("blocked")),
        )
        return outcome, status, elapsed_ms

    def _cooldown(self, started: float) -> None:
        """Stop sending and wait. Never a probe loop hunting for recovery."""
        if self.limits.cooldown_seconds <= 0:
            return
        scrape_events.emit(
            scrape_events.CALIBRATION_EVENT,
            phase="cooldown",
            stop_reason=self.stop_reason,
            cooldown_seconds=self.limits.cooldown_seconds,
        )
        cooldown_started = time.monotonic()
        time.sleep(self.limits.cooldown_seconds)
        self.recovery_seconds = round(time.monotonic() - cooldown_started, 3)

    # ── reporting ────────────────────────────────────────────────────────────
    def recommendation(self) -> Dict[str, Any]:
        """Conservative envelope: 50% of the highest *healthy* observed step.

        Deliberately not "the last rate that worked". The last healthy step is
        the edge of what was tolerated for a few seconds; sustained scraping runs
        for minutes, against a session that ages.
        """
        healthy = [s for s in self.steps if s.healthy]
        if not healthy:
            return {
                "status": "no_healthy_envelope_observed",
                "note": (
                    "No step completed without overload or block signals. Do not raise the "
                    "configured limits; investigate session health first."
                ),
            }
        best = max(healthy, key=lambda s: (s.actual_rate_per_sec, s.concurrency))
        return {
            "status": "ok",
            "observed_healthy_rate_per_sec": round(best.actual_rate_per_sec, 4),
            "observed_healthy_concurrency": best.concurrency,
            "recommended_AIRBNB_MAX_START_RATE_PER_SEC": round(
                max(0.05, best.actual_rate_per_sec * 0.5), 3
            ),
            "recommended_AIRBNB_MAX_INFLIGHT_REQUESTS": max(1, best.concurrency // 2 or 1),
            "safety_margin": "50% of the highest healthy observed envelope",
            "note": (
                "Deployment-, session-, endpoint- and time-specific. Not an Airbnb limit. "
                "The runtime admission policy must stay adaptive regardless of these values."
            ),
        }

    def report(self, *, total_seconds: float) -> Dict[str, Any]:
        all_latencies = sorted(ms for step in self.steps for ms in step.latencies_ms)
        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "limits": asdict(self.limits),
            "stop_reason": self.stop_reason,
            "total_requests": self.total_requests,
            "total_seconds": round(total_seconds, 3),
            "time_to_first_overload_seconds": self.time_to_first_overload_seconds,
            "cooldown_recovery_seconds": self.recovery_seconds,
            "latency_p50_ms": round(_percentile(all_latencies, 50), 1) if all_latencies else None,
            "latency_p95_ms": round(_percentile(all_latencies, 95), 1) if all_latencies else None,
            "steps": [s.summary() for s in self.steps],
            "recommendation": self.recommendation(),
        }


def _live_probe(index: int) -> ProbeResult:  # pragma: no cover - live path
    """One real StaysSearch replay against the configured session.

    Uses the ordinary scraper path so the probe measures the same request the
    worker actually sends, rather than a synthetic one.
    """
    from worker.scraper.playwright_scraper import PlaywrightScraper

    scraper = getattr(_live_probe, "_scraper", None)
    if scraper is None:
        scraper = PlaywrightScraper({})
        setattr(_live_probe, "_scraper", scraper)
    started = time.perf_counter()
    try:
        result = scraper.fetch_search_direct({"itemsPerGrid": 20})
    except Exception as exc:
        return {"status": None, "error": type(exc).__name__, "elapsed_ms": (time.perf_counter() - started) * 1000.0}
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if result is None:
        # fetch_search_direct already classified and logged the failure; the
        # probe only needs to know it was not healthy.
        return {"status": None, "error": "direct_search_unavailable", "elapsed_ms": elapsed_ms}
    status, _data = result
    return {"status": status, "elapsed_ms": elapsed_ms}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded Airbnb request-envelope calibration (opt-in; sends live traffic)."
    )
    parser.add_argument("--i-understand-live-traffic", action="store_true")
    parser.add_argument("--max-requests", type=int, default=60)
    parser.add_argument("--max-seconds", type=float, default=180.0)
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--max-rate-per-sec", type=float, default=2.0)
    parser.add_argument("--healthy-window", type=int, default=10)
    parser.add_argument("--error-threshold", type=int, default=3)
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args(argv)

    if not calibration_enabled():
        print(
            "Calibration is disabled. Set AIRBNB_CALIBRATION_ENABLED=1 to allow it.",
            file=sys.stderr,
        )
        return 2
    if not args.i_understand_live_traffic:
        print(
            "Refusing to run without --i-understand-live-traffic: this sends real "
            "requests to Airbnb.",
            file=sys.stderr,
        )
        return 2

    limits = CalibrationLimits(
        max_requests=args.max_requests,
        max_seconds=args.max_seconds,
        max_concurrency=args.max_concurrency,
        max_rate_per_sec=args.max_rate_per_sec,
        healthy_window=args.healthy_window,
        error_threshold=args.error_threshold,
    )
    report = CalibrationRun(_live_probe, limits).run()

    out_path = Path(args.out) if args.out else (
        Path(__file__).resolve().parent.parent / "logs" / "airbnb_calibration.json"
    )
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"Could not write report to {out_path}: {exc}", file=sys.stderr)

    print(json.dumps(report["recommendation"], indent=2))
    print(f"\nFull report: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
