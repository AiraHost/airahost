"""The admission policy is the only thing standing between concurrent report
threads and an Airbnb 503 storm, so each test here pins a property that, if it
broke, would let load through rather than merely change a number.
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from worker.core.admission import (
    AdmissionCircuitOpen,
    AdmissionConfig,
    AirbnbAdmissionController,
    OUTCOME_APPLICATION_ERROR,
    OUTCOME_BLOCKED,
    OUTCOME_DEGRADED,
    OUTCOME_OVERLOAD,
    OUTCOME_SUCCESS,
    OUTCOME_TRANSPORT_ERROR,
    classify_response_outcome,
    config_from_env,
    parse_retry_after,
)
from worker.core.scrape_trace import (
    CLASS_BROWSER_NAVIGATION,
    CLASS_PDP,
    CLASS_SEARCH,
    RetryBudget,
)


def _config(**overrides) -> AdmissionConfig:
    base = dict(
        max_start_rate_per_sec=1000.0,
        min_start_rate_per_sec=1.0,
        max_inflight=4,
        min_inflight=1,
        class_inflight_caps={
            CLASS_SEARCH: 4,
            CLASS_PDP: 4,
            CLASS_BROWSER_NAVIGATION: 4,
            "session_refresh": 4,
        },
        recovery_interval_seconds=0.05,
        recovery_success_threshold=2,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.05,
        circuit_failure_threshold=2,
        circuit_cooldown_seconds=0.2,
        circuit_max_cooldown_seconds=1.0,
    )
    base.update(overrides)
    return AdmissionConfig(**base)


class AggregateCeilingTest(unittest.TestCase):
    def test_search_pdp_and_browser_share_one_ceiling(self):
        """A per-class cap alone is not a limit on what Airbnb receives.

        Search, PDP and browser work are three code paths but one origin. If
        each only respected its own ceiling, the site would see their sum — the
        exact reason a "safe" configuration still produced 503s.
        """
        controller = AirbnbAdmissionController(_config(max_inflight=3))
        peak = {"n": 0, "max": 0}
        lock = threading.Lock()
        barrier_hold = 0.03

        def worker(request_class: str) -> None:
            with controller.slot(request_class):
                with lock:
                    peak["n"] += 1
                    peak["max"] = max(peak["max"], peak["n"])
                time.sleep(barrier_hold)
                with lock:
                    peak["n"] -= 1

        classes = [CLASS_SEARCH, CLASS_PDP, CLASS_BROWSER_NAVIGATION] * 4
        threads = [threading.Thread(target=worker, args=(c,)) for c in classes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertLessEqual(peak["max"], 3, "aggregate ceiling was exceeded")

    def test_per_class_cap_is_enforced_under_the_aggregate(self):
        controller = AirbnbAdmissionController(
            _config(
                max_inflight=6,
                class_inflight_caps={
                    CLASS_SEARCH: 2,
                    CLASS_PDP: 6,
                    CLASS_BROWSER_NAVIGATION: 6,
                    "session_refresh": 6,
                },
            )
        )
        peak = {"n": 0, "max": 0}
        lock = threading.Lock()

        def worker() -> None:
            with controller.slot(CLASS_SEARCH):
                with lock:
                    peak["n"] += 1
                    peak["max"] = max(peak["max"], peak["n"])
                time.sleep(0.03)
                with lock:
                    peak["n"] -= 1

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(peak["max"], 2)


class StartSmoothingTest(unittest.TestCase):
    def test_request_starts_are_spaced_across_classes(self):
        """Bursts from many threads must be smoothed globally, not per class."""
        controller = AirbnbAdmissionController(
            _config(max_start_rate_per_sec=20.0, min_start_rate_per_sec=1.0, max_inflight=8)
        )
        starts = []
        lock = threading.Lock()

        def worker(request_class: str) -> None:
            with controller.slot(request_class):
                with lock:
                    starts.append(time.monotonic())

        classes = [CLASS_SEARCH, CLASS_PDP, CLASS_BROWSER_NAVIGATION] * 2
        threads = [threading.Thread(target=worker, args=(c,)) for c in classes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        starts.sort()
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        for gap in gaps:
            self.assertGreaterEqual(gap, 0.04, f"start gap too small: {gap:.4f}s")


class RetryAfterTest(unittest.TestCase):
    def test_delta_seconds(self):
        self.assertEqual(parse_retry_after("120"), 120.0)
        self.assertEqual(parse_retry_after(" 0 "), 0.0)

    def test_http_date(self):
        target = datetime.now(timezone.utc) + timedelta(seconds=45)
        parsed = parse_retry_after(format_datetime(target, usegmt=True))
        self.assertIsNotNone(parsed)
        self.assertGreater(parsed, 30.0)
        self.assertLess(parsed, 60.0)

    def test_past_http_date_is_zero_not_negative(self):
        target = datetime.now(timezone.utc) - timedelta(seconds=300)
        self.assertEqual(parse_retry_after(format_datetime(target, usegmt=True)), 0.0)

    def test_invalid_values_fall_back_safely(self):
        """An unusable header must yield None so the caller uses jittered backoff.

        Returning 0 instead would turn a malformed header into an immediate
        retry — the opposite of what Retry-After asks for.
        """
        for bad in (None, "", "   ", "soon", "-5", "NaN", "inf", "Tue, 99 Xxx 2026"):
            self.assertIsNone(parse_retry_after(bad), f"expected None for {bad!r}")

    def test_controller_honors_retry_after_over_jitter(self):
        controller = AirbnbAdmissionController(_config(backoff_max_seconds=120.0))
        cooldown = controller.record_overload(CLASS_SEARCH, status=429, retry_after="30")
        self.assertEqual(cooldown, 30.0)

    def test_controller_caps_absurd_retry_after(self):
        controller = AirbnbAdmissionController(_config(backoff_max_seconds=5.0))
        self.assertEqual(
            controller.record_overload(CLASS_SEARCH, status=503, retry_after="86400"), 5.0
        )


class AdaptiveLimitTest(unittest.TestCase):
    def test_overload_reduces_rate_and_concurrency_multiplicatively(self):
        controller = AirbnbAdmissionController(
            _config(max_start_rate_per_sec=8.0, max_inflight=8, decrease_factor=0.5)
        )
        before = controller.snapshot()
        controller.record_overload(CLASS_SEARCH, status=503, retry_after=None)
        after = controller.snapshot()
        self.assertLess(after["permitted_rate_per_sec"], before["permitted_rate_per_sec"])
        self.assertLess(after["permitted_concurrency"], before["permitted_concurrency"])
        self.assertAlmostEqual(after["permitted_rate_per_sec"], 4.0, places=3)
        self.assertEqual(after["permitted_concurrency"], 4)

    def test_reduction_never_falls_below_the_configured_floor(self):
        controller = AirbnbAdmissionController(
            _config(max_start_rate_per_sec=2.0, min_start_rate_per_sec=0.5, max_inflight=4)
        )
        for _ in range(12):
            controller.record_overload(CLASS_SEARCH, status=429, retry_after="0")
        snap = controller.snapshot()
        self.assertGreaterEqual(snap["permitted_rate_per_sec"], 0.5)
        self.assertGreaterEqual(snap["permitted_concurrency"], 1)

    def test_cooldown_is_jittered_not_fixed(self):
        """Two workers penalized together must not resume in lockstep."""
        controller = AirbnbAdmissionController(
            _config(backoff_base_seconds=1.0, backoff_max_seconds=30.0)
        )
        samples = {round(controller.backoff_seconds(4), 6) for _ in range(30)}
        self.assertGreater(len(samples), 5, "backoff appears deterministic")
        self.assertTrue(all(0.0 <= s <= 30.0 for s in samples))

    def test_healthy_window_recovers_additively_without_exceeding_maxima(self):
        controller = AirbnbAdmissionController(
            _config(
                max_start_rate_per_sec=4.0,
                max_inflight=4,
                recovery_interval_seconds=0.01,
                recovery_success_threshold=1,
                rate_increase_step=0.5,
            )
        )
        controller.record_overload(CLASS_SEARCH, status=503, retry_after="0")
        reduced = controller.snapshot()

        for _ in range(40):
            time.sleep(0.012)
            controller.record_success(CLASS_SEARCH)

        recovered = controller.snapshot()
        self.assertGreater(recovered["permitted_rate_per_sec"], reduced["permitted_rate_per_sec"])
        self.assertLessEqual(recovered["permitted_rate_per_sec"], 4.0)
        self.assertLessEqual(recovered["permitted_concurrency"], 4)

    def test_recovery_requires_a_sustained_window_not_one_success(self):
        controller = AirbnbAdmissionController(
            _config(
                max_start_rate_per_sec=4.0,
                recovery_interval_seconds=60.0,
                recovery_success_threshold=100,
            )
        )
        controller.record_overload(CLASS_SEARCH, status=503, retry_after="0")
        reduced = controller.snapshot()["permitted_rate_per_sec"]
        for _ in range(20):
            controller.record_success(CLASS_SEARCH)
        self.assertEqual(controller.snapshot()["permitted_rate_per_sec"], reduced)


class CircuitBreakerTest(unittest.TestCase):
    def test_open_circuit_refuses_admission_for_every_thread(self):
        """While open, no thread may start browser work.

        This is the anti-stampede property: without it, a blocked session turns
        into one Playwright navigation per date per offset.
        """
        controller = AirbnbAdmissionController(_config(circuit_failure_threshold=2))
        controller.record_block(CLASS_SEARCH, reason_code="graphql_auth_error")
        controller.record_block(CLASS_SEARCH, reason_code="graphql_auth_error")

        refused = 0
        for _ in range(6):
            try:
                with controller.slot(CLASS_BROWSER_NAVIGATION):
                    pass
            except AdmissionCircuitOpen:
                refused += 1
        self.assertEqual(refused, 6)

    def test_half_open_admits_exactly_one_probe(self):
        controller = AirbnbAdmissionController(
            _config(circuit_failure_threshold=1, circuit_cooldown_seconds=0.05)
        )
        controller.record_block(CLASS_SEARCH, reason_code="graphql_auth_error")
        time.sleep(0.08)

        admitted = []
        refused = []
        lock = threading.Lock()
        release = threading.Event()

        def worker() -> None:
            try:
                ticket = controller.acquire(CLASS_BROWSER_NAVIGATION)
            except AdmissionCircuitOpen:
                with lock:
                    refused.append(1)
                return
            with lock:
                admitted.append(ticket)
            release.wait(1.0)
            controller.release(ticket)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        time.sleep(0.1)
        release.set()
        for t in threads:
            t.join()

        self.assertEqual(len(admitted), 1, "more than one half-open probe was admitted")
        self.assertEqual(len(refused), 4)
        self.assertTrue(admitted[0].is_half_open_probe)

    def test_successful_probe_closes_the_circuit(self):
        controller = AirbnbAdmissionController(
            _config(circuit_failure_threshold=1, circuit_cooldown_seconds=0.05)
        )
        controller.record_block(CLASS_SEARCH, reason_code="graphql_auth_error")
        time.sleep(0.08)
        with controller.slot(CLASS_SEARCH):
            pass
        controller.record_success(CLASS_SEARCH)
        self.assertEqual(controller.snapshot()["circuit_state"], "closed")
        with controller.slot(CLASS_SEARCH):
            pass  # no raise

    def test_failed_probe_reopens_with_a_longer_cooldown(self):
        controller = AirbnbAdmissionController(
            _config(
                circuit_failure_threshold=1,
                circuit_cooldown_seconds=0.05,
                circuit_max_cooldown_seconds=10.0,
            )
        )
        controller.record_block(CLASS_SEARCH, reason_code="graphql_auth_error")
        time.sleep(0.08)
        ticket = controller.acquire(CLASS_SEARCH)
        controller.release(ticket)
        controller.record_overload(CLASS_SEARCH, status=503, retry_after="0")
        self.assertEqual(controller.snapshot()["circuit_state"], "open")
        with self.assertRaises(AdmissionCircuitOpen):
            controller.acquire(CLASS_SEARCH)

    def test_routine_fallback_blocks_slow_down_but_do_not_open_the_circuit(self):
        """A challenged direct replay that the browser then serves is normal.

        Counting those toward the breaker opened the circuit during healthy
        scraping, turning an ordinary direct->Playwright fallback into a failed
        report. The throttle still applies; only the breaker is spared.
        """
        controller = AirbnbAdmissionController(
            _config(circuit_failure_threshold=2, max_start_rate_per_sec=4.0)
        )
        before = controller.snapshot()["permitted_rate_per_sec"]
        for _ in range(10):
            controller.record_block(
                CLASS_SEARCH,
                reason_code="graphql_auth_error",
                counts_toward_circuit=False,
            )
        snap = controller.snapshot()
        self.assertEqual(snap["circuit_state"], "closed")
        self.assertLess(snap["permitted_rate_per_sec"], before, "throttle must still apply")
        # Admission still works, so the browser fallback can run.
        with controller.slot(CLASS_BROWSER_NAVIGATION):
            pass

    def test_authoritative_blocks_still_open_the_circuit(self):
        controller = AirbnbAdmissionController(_config(circuit_failure_threshold=2))
        controller.record_block(CLASS_SEARCH, reason_code="graphql_auth_error")
        controller.record_block(CLASS_SEARCH, reason_code="graphql_auth_error")
        self.assertEqual(controller.snapshot()["circuit_state"], "open")

    def test_abandoned_probe_does_not_wedge_the_circuit(self):
        """A probe that raises before recording a verdict must free its slot."""
        controller = AirbnbAdmissionController(
            _config(circuit_failure_threshold=1, circuit_cooldown_seconds=0.05)
        )
        controller.record_block(CLASS_SEARCH, reason_code="graphql_auth_error")
        time.sleep(0.08)
        try:
            with controller.slot(CLASS_SEARCH):
                raise RuntimeError("probe blew up")
        except RuntimeError:
            pass
        # A later caller can still take the probe rather than being locked out.
        ticket = controller.acquire(CLASS_SEARCH)
        self.assertTrue(ticket.is_half_open_probe)
        controller.release(ticket)


class RetryBudgetTest(unittest.TestCase):
    def test_per_operation_budget_is_enforced(self):
        budget = RetryBudget(per_operation=2, per_report=100)
        self.assertTrue(budget.try_consume("search:2026-01-01"))
        self.assertTrue(budget.try_consume("search:2026-01-01"))
        self.assertFalse(budget.try_consume("search:2026-01-01"))
        # A different operation still has its own allowance.
        self.assertTrue(budget.try_consume("search:2026-01-02"))

    def test_per_report_budget_caps_the_sum_across_operations(self):
        """Without this, N concurrent threads multiply retries N-fold."""
        budget = RetryBudget(per_operation=5, per_report=3)
        granted = sum(1 for i in range(20) if budget.try_consume(f"op-{i}"))
        self.assertEqual(granted, 3)

    def test_budget_is_thread_safe(self):
        budget = RetryBudget(per_operation=100, per_report=10)
        granted = []
        lock = threading.Lock()

        def worker() -> None:
            if budget.try_consume("shared"):
                with lock:
                    granted.append(1)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(granted), 10)


class OutcomeClassificationTest(unittest.TestCase):
    def test_outcomes_remain_distinct(self):
        """Overload, block, degraded, transport and server error are not one thing.

        Collapsing them is how a real application bug would get "fixed" by
        slowing the scraper down, and how a 503 would be reported as proof of
        rate limiting.
        """
        cases = [
            (dict(status=200), OUTCOME_SUCCESS),
            (dict(status=200, payload_malformed=True), OUTCOME_DEGRADED),
            (dict(status=429), OUTCOME_OVERLOAD),
            (dict(status=503), OUTCOME_OVERLOAD),
            (dict(status=403), OUTCOME_BLOCKED),
            (dict(status=401), OUTCOME_BLOCKED),
            (dict(status=200, auth_blocked=True), OUTCOME_BLOCKED),
            (dict(status=500), OUTCOME_APPLICATION_ERROR),
            (dict(status=404), OUTCOME_DEGRADED),
            (dict(status=None, exception=OSError("reset")), OUTCOME_TRANSPORT_ERROR),
        ]
        for kwargs, expected in cases:
            self.assertEqual(classify_response_outcome(**kwargs), expected, msg=str(kwargs))

    def test_transport_error_does_not_throttle(self):
        """A socket reset is our problem, not Airbnb asking us to slow down."""
        controller = AirbnbAdmissionController(_config(max_start_rate_per_sec=4.0))
        before = controller.snapshot()["permitted_rate_per_sec"]
        controller.record_neutral_failure(CLASS_SEARCH, outcome=OUTCOME_TRANSPORT_ERROR)
        self.assertEqual(controller.snapshot()["permitted_rate_per_sec"], before)


class ConfigBoundsTest(unittest.TestCase):
    def test_env_values_are_clamped(self):
        import os

        keys = {
            "AIRBNB_MAX_START_RATE_PER_SEC": "99999",
            "AIRBNB_MAX_INFLIGHT_REQUESTS": "9999",
            "AIRBNB_BACKOFF_MAX_SECONDS": "-4",
            "AIRBNB_OVERLOAD_DECREASE_FACTOR": "12",
            "AIRBNB_ADMISSION_INSTANCES": "0",
        }
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ.update(keys)
            cfg = config_from_env()
            self.assertLessEqual(cfg.max_start_rate_per_sec, 20.0)
            self.assertLessEqual(cfg.max_inflight, 32)
            self.assertGreaterEqual(cfg.backoff_max_seconds, 1.0)
            self.assertLessEqual(cfg.decrease_factor, 0.95)
            self.assertGreaterEqual(cfg.instances, 1)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_declared_instances_partition_the_budget(self):
        """Multi-instance deployments split one envelope; they do not each get it."""
        import os

        saved = {
            k: os.environ.get(k)
            for k in ("AIRBNB_ADMISSION_INSTANCES", "AIRBNB_MAX_START_RATE_PER_SEC", "AIRBNB_MAX_INFLIGHT_REQUESTS")
        }
        try:
            os.environ["AIRBNB_MAX_START_RATE_PER_SEC"] = "4"
            os.environ["AIRBNB_MAX_INFLIGHT_REQUESTS"] = "8"
            os.environ["AIRBNB_ADMISSION_INSTANCES"] = "1"
            single = config_from_env()
            os.environ["AIRBNB_ADMISSION_INSTANCES"] = "4"
            shared = config_from_env()
            self.assertLess(shared.max_start_rate_per_sec, single.max_start_rate_per_sec)
            self.assertLess(shared.max_inflight, single.max_inflight)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
