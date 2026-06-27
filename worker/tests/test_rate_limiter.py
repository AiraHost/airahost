"""Tests for the process-global Airbnb rate limiter."""

import threading
import time
import unittest

from worker.core.rate_limiter import AirbnbRateLimiter


class MinIntervalTest(unittest.TestCase):
    def test_request_starts_are_spaced(self):
        limiter = AirbnbRateLimiter(min_interval_seconds=0.05, max_inflight=8)
        starts = []
        lock = threading.Lock()

        def worker():
            with limiter.slot():
                with lock:
                    starts.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        starts.sort()
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        # Every consecutive pair of starts should be spaced ~>= the interval
        # (allow scheduler slop on the lower bound).
        for g in gaps:
            self.assertGreaterEqual(g, 0.04, msg=f"gap too small: {g:.4f}s")


class MaxInflightTest(unittest.TestCase):
    def test_concurrency_is_capped(self):
        limiter = AirbnbRateLimiter(min_interval_seconds=0.0, max_inflight=3)
        current = {"n": 0, "peak": 0}
        lock = threading.Lock()

        def worker():
            with limiter.slot():
                with lock:
                    current["n"] += 1
                    current["peak"] = max(current["peak"], current["n"])
                time.sleep(0.02)
                with lock:
                    current["n"] -= 1

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertLessEqual(current["peak"], 3)


class PenaltyTest(unittest.TestCase):
    def test_penalize_widens_interval(self):
        limiter = AirbnbRateLimiter(min_interval_seconds=0.01, max_inflight=8)
        # First call to prime _next_allowed_at.
        with limiter.slot():
            pass
        limiter.penalize(0.3)
        t0 = time.monotonic()
        with limiter.slot():
            pass
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.2, msg=f"penalty not applied: {elapsed:.3f}s")


class DisabledTest(unittest.TestCase):
    def test_disabled_is_noop(self):
        limiter = AirbnbRateLimiter(min_interval_seconds=10.0, max_inflight=1, disabled=True)
        t0 = time.monotonic()
        with limiter.slot():
            pass
        with limiter.slot():
            pass
        self.assertLess(time.monotonic() - t0, 1.0)


if __name__ == "__main__":
    unittest.main()
