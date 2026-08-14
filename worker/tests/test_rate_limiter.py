"""The legacy rate-limiter surface must be one policy with admission, not two.

`worker.core.rate_limiter` used to own an independent min-interval +
bounded-concurrency limiter. Two limiters cannot coordinate — each enforces its
own ceiling while Airbnb sees their sum — so the module is now a shim over the
single admission controller. These tests pin that it really delegates: if
someone reintroduced private state here, the shim would pass traffic the
admission policy had already decided to hold back.
"""

import threading
import time
import unittest

from worker.core import admission
from worker.core.admission import (
    AdmissionCircuitOpen,
    AdmissionConfig,
    AirbnbAdmissionController,
)
from worker.core.rate_limiter import get_airbnb_rate_limiter
from worker.core.scrape_trace import CLASS_BROWSER_NAVIGATION, CLASS_SEARCH


class ShimDelegationTest(unittest.TestCase):
    def setUp(self) -> None:
        admission.reset_admission_controller_for_tests()
        self.addCleanup(admission.reset_admission_controller_for_tests)

    def _install(self, config: AdmissionConfig) -> AirbnbAdmissionController:
        controller = AirbnbAdmissionController(config)
        admission._singleton = controller
        return controller

    def test_shim_shares_the_process_global_controller(self):
        controller = self._install(AdmissionConfig(max_start_rate_per_sec=1000.0))
        limiter = get_airbnb_rate_limiter()
        before = controller.snapshot()["count_admitted"]
        with limiter.slot():
            pass
        self.assertEqual(controller.snapshot()["count_admitted"], before + 1)

    def test_shim_traffic_counts_against_the_same_ceiling_as_browser_work(self):
        """Legacy callers must not get a private concurrency allowance."""
        controller = self._install(
            AdmissionConfig(
                max_start_rate_per_sec=1000.0,
                max_inflight=1,
                class_inflight_caps={
                    CLASS_SEARCH: 1,
                    "pdp": 1,
                    CLASS_BROWSER_NAVIGATION: 1,
                    "session_refresh": 1,
                },
            )
        )
        limiter = get_airbnb_rate_limiter()
        held = threading.Event()
        release = threading.Event()
        peak = {"n": 0, "max": 0}
        lock = threading.Lock()

        def legacy_caller():
            with limiter.slot():
                with lock:
                    peak["n"] += 1
                    peak["max"] = max(peak["max"], peak["n"])
                held.set()
                release.wait(1.0)
                with lock:
                    peak["n"] -= 1

        def browser_caller():
            held.wait(1.0)
            with controller.slot(CLASS_BROWSER_NAVIGATION):
                with lock:
                    peak["n"] += 1
                    peak["max"] = max(peak["max"], peak["n"])
                    peak["n"] -= 1

        threads = [threading.Thread(target=legacy_caller), threading.Thread(target=browser_caller)]
        for t in threads:
            t.start()
        time.sleep(0.15)
        release.set()
        for t in threads:
            t.join(2.0)

        self.assertEqual(peak["max"], 1, "shim bypassed the aggregate ceiling")

    def test_penalize_drives_the_shared_adaptive_state(self):
        controller = self._install(
            AdmissionConfig(max_start_rate_per_sec=8.0, min_start_rate_per_sec=0.5)
        )
        before = controller.snapshot()["permitted_rate_per_sec"]
        get_airbnb_rate_limiter().penalize(0.0)
        self.assertLess(controller.snapshot()["permitted_rate_per_sec"], before)

    def test_shim_respects_an_open_circuit(self):
        controller = self._install(
            AdmissionConfig(circuit_failure_threshold=1, circuit_cooldown_seconds=5.0)
        )
        controller.record_block(CLASS_SEARCH, reason_code="graphql_auth_error")
        with self.assertRaises(AdmissionCircuitOpen):
            with get_airbnb_rate_limiter().slot():
                pass

    def test_disabled_limiter_is_a_noop(self):
        self._install(AdmissionConfig(max_start_rate_per_sec=0.1, max_inflight=1, disabled=True))
        limiter = get_airbnb_rate_limiter()
        started = time.monotonic()
        with limiter.slot():
            pass
        with limiter.slot():
            pass
        self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
