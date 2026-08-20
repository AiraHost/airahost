"""Calibration-tool tests.

The tool sends real traffic when it runs, so the properties that matter are the
ones that stop it: every safety bound must terminate the run, and a unit test
must never reach the network. Each test drives an injected probe.
"""

from __future__ import annotations

import unittest

from worker.core.airbnb_calibration import (
    ABSOLUTE_MAX_CONCURRENCY,
    ABSOLUTE_MAX_RATE_PER_SEC,
    ABSOLUTE_MAX_REQUESTS,
    ABSOLUTE_MAX_SECONDS,
    CalibrationLimits,
    CalibrationRun,
    calibration_enabled,
    main,
)


def _healthy_probe(_index: int) -> dict:
    return {"status": 200, "elapsed_ms": 12.0}


def _overloaded_after(n: int):
    def probe(index: int) -> dict:
        if index > n:
            return {"status": 503, "elapsed_ms": 8.0}
        return {"status": 200, "elapsed_ms": 10.0}

    return probe


def _fast_limits(**overrides) -> CalibrationLimits:
    base = dict(
        max_requests=20,
        max_seconds=30.0,
        max_concurrency=2,
        max_rate_per_sec=5.0,
        healthy_window=3,
        error_threshold=2,
        cooldown_seconds=0.0,
    )
    base.update(overrides)
    limits = CalibrationLimits(**base)
    # start_interval_seconds is floored at 1s by design; unit tests must not
    # spend that, so lower it after clamping to keep the run instantaneous.
    limits.start_interval_seconds = 0.001
    return limits


class SafetyBoundTest(unittest.TestCase):
    def test_absolute_ceilings_clamp_absurd_arguments(self):
        """A typo in an argument must not become a load test."""
        limits = CalibrationLimits(
            max_requests=10 ** 9,
            max_seconds=10 ** 9,
            max_concurrency=1000,
            max_rate_per_sec=1000.0,
        )
        self.assertEqual(limits.max_requests, ABSOLUTE_MAX_REQUESTS)
        self.assertEqual(limits.max_seconds, ABSOLUTE_MAX_SECONDS)
        self.assertEqual(limits.max_concurrency, ABSOLUTE_MAX_CONCURRENCY)
        self.assertEqual(limits.max_rate_per_sec, ABSOLUTE_MAX_RATE_PER_SEC)

    def test_start_is_always_conservative(self):
        limits = CalibrationLimits(start_concurrency=99, start_interval_seconds=0.001)
        self.assertLessEqual(limits.start_concurrency, limits.max_concurrency)
        self.assertGreaterEqual(limits.start_interval_seconds, 1.0)

    def test_request_cap_stops_the_run(self):
        # Headroom on rate/concurrency so the request cap — not the ceiling —
        # is the bound under test.
        run = CalibrationRun(
            _healthy_probe,
            _fast_limits(max_requests=7, max_rate_per_sec=5.0, max_concurrency=8),
        )
        report = run.run()
        self.assertLessEqual(report["total_requests"], 7)
        self.assertEqual(report["stop_reason"], "max_requests_reached")

    def test_duration_cap_stops_the_run(self):
        limits = _fast_limits(
            max_requests=ABSOLUTE_MAX_REQUESTS, max_seconds=1.0, max_concurrency=8
        )
        limits.max_rate_per_sec = 1.6  # ~0.6s per request; the clock wins first
        report = CalibrationRun(_healthy_probe, limits).run()
        self.assertEqual(report["stop_reason"], "max_duration_reached")

    def test_error_threshold_stops_immediately_and_never_pushes_through(self):
        """Repeated 429/503 ends the run; it is not something to power through."""
        run = CalibrationRun(_overloaded_after(2), _fast_limits(error_threshold=2))
        report = run.run()
        self.assertIn(report["stop_reason"], ("error_threshold_reached", "unhealthy_step"))
        self.assertLessEqual(report["total_requests"], 5)
        self.assertIsNotNone(report["time_to_first_overload_seconds"])

    def test_ceiling_stops_the_ramp(self):
        report = CalibrationRun(
            _healthy_probe,
            _fast_limits(max_requests=ABSOLUTE_MAX_REQUESTS, max_concurrency=1, max_rate_per_sec=5.0),
        ).run()
        self.assertIn(
            report["stop_reason"], ("reached_configured_ceiling", "max_duration_reached")
        )

    def test_run_never_exceeds_configured_concurrency_in_the_report(self):
        report = CalibrationRun(_healthy_probe, _fast_limits(max_concurrency=2)).run()
        for step in report["steps"]:
            self.assertLessEqual(step["concurrency"], 2)


class ReportTest(unittest.TestCase):
    def test_report_records_the_measurements_an_operator_needs(self):
        report = CalibrationRun(_healthy_probe, _fast_limits(max_requests=9)).run()
        for key in (
            "limits",
            "stop_reason",
            "total_requests",
            "total_seconds",
            "latency_p50_ms",
            "latency_p95_ms",
            "steps",
            "recommendation",
        ):
            self.assertIn(key, report)
        step = report["steps"][0]
        for key in (
            "target_rate_per_sec",
            "actual_rate_per_sec",
            "concurrency",
            "status_counts",
            "latency_p50_ms",
            "latency_p95_ms",
            "healthy",
        ):
            self.assertIn(key, step)

    def test_recommendation_applies_a_margin_below_the_observed_envelope(self):
        """The last rate that survived a few seconds is not a sustainable rate."""
        report = CalibrationRun(_healthy_probe, _fast_limits(max_requests=12)).run()
        rec = report["recommendation"]
        self.assertEqual(rec["status"], "ok")
        self.assertLess(
            rec["recommended_AIRBNB_MAX_START_RATE_PER_SEC"],
            rec["observed_healthy_rate_per_sec"],
        )
        self.assertIn("Not an Airbnb limit", rec["note"])

    def test_no_healthy_step_yields_no_recommendation(self):
        """Absent evidence, the tool must not invent a number."""
        report = CalibrationRun(_overloaded_after(0), _fast_limits(error_threshold=1)).run()
        self.assertEqual(report["recommendation"]["status"], "no_healthy_envelope_observed")
        self.assertNotIn("recommended_AIRBNB_MAX_START_RATE_PER_SEC", report["recommendation"])

    def test_a_raising_probe_is_data_not_a_crash(self):
        def boom(_index: int) -> dict:
            raise ConnectionError("network down")

        report = CalibrationRun(boom, _fast_limits(error_threshold=2)).run()
        self.assertGreaterEqual(report["steps"][0]["transport_errors"], 1)


class OptInTest(unittest.TestCase):
    def test_disabled_by_default(self):
        import os

        saved = os.environ.pop("AIRBNB_CALIBRATION_ENABLED", None)
        try:
            self.assertFalse(calibration_enabled())
            self.assertEqual(main(["--i-understand-live-traffic"]), 2)
        finally:
            if saved is not None:
                os.environ["AIRBNB_CALIBRATION_ENABLED"] = saved

    def test_enabled_still_requires_explicit_acknowledgement(self):
        import os

        saved = os.environ.get("AIRBNB_CALIBRATION_ENABLED")
        os.environ["AIRBNB_CALIBRATION_ENABLED"] = "1"
        try:
            # No --i-understand-live-traffic: refuses without sending anything.
            self.assertEqual(main([]), 2)
        finally:
            if saved is None:
                os.environ.pop("AIRBNB_CALIBRATION_ENABLED", None)
            else:
                os.environ["AIRBNB_CALIBRATION_ENABLED"] = saved


if __name__ == "__main__":
    unittest.main()
