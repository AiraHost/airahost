"""
Process-global, thread-safe rate limiter for outbound Airbnb requests.

Airbnb returns HTTP 503/429 when the worker sends too many requests too quickly
(across both browser navigation and direct HTTP). This limiter self-throttles all
outbound Airbnb traffic so we stay under that limit. It combines two controls:

  * **min-interval** — a leaky-bucket gate that enforces a minimum spacing between
    consecutive request *starts* (smooths bursts from many worker threads).
  * **max in-flight** — a bounded semaphore capping concurrent Airbnb requests.

A 503/429 anywhere can call :meth:`AirbnbRateLimiter.penalize` to temporarily widen
the spacing (adaptive cooldown) which decays back to the configured baseline.

Configuration (env vars, read once at process start):
  AIRBNB_MIN_REQUEST_INTERVAL_MS   minimum spacing between request starts (default 250)
  AIRBNB_MAX_INFLIGHT_REQUESTS     max concurrent in-flight requests       (default 8)
  AIRBNB_RATE_LIMIT_DISABLED       set to 1/true to disable throttling entirely

Usage::

    from worker.core.rate_limiter import get_airbnb_rate_limiter
    limiter = get_airbnb_rate_limiter()
    with limiter.slot():
        ... send one Airbnb request ...
    # on a 503/429:
    limiter.penalize()
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from typing import Iterator, Optional

logger = logging.getLogger("worker.core.rate_limiter")


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, "")).strip() or default)
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "")).strip() or default)
    except Exception:
        return default


def _env_flag(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in ("1", "true", "yes", "on")


class AirbnbRateLimiter:
    """Thread-safe min-interval + bounded-concurrency limiter with adaptive cooldown."""

    def __init__(
        self,
        *,
        min_interval_seconds: float,
        max_inflight: int,
        max_penalty_seconds: float = 8.0,
        penalty_decay_seconds: float = 5.0,
        disabled: bool = False,
    ) -> None:
        self._base_interval = max(0.0, float(min_interval_seconds))
        self._max_inflight = max(1, int(max_inflight))
        self._max_penalty = max(0.0, float(max_penalty_seconds))
        self._penalty_decay = max(0.1, float(penalty_decay_seconds))
        self._disabled = bool(disabled)

        self._interval_lock = threading.Lock()
        self._next_allowed_at = 0.0
        # Extra spacing added on top of the baseline after a 503/429; decays over time.
        self._penalty = 0.0
        self._penalty_set_at = 0.0
        self._semaphore = threading.BoundedSemaphore(self._max_inflight)

    # ── min-interval gate ────────────────────────────────────────────────────
    def _current_interval_locked(self) -> float:
        if self._penalty <= 0.0:
            return self._base_interval
        # Linearly decay the penalty back toward zero since it was last set.
        elapsed = time.monotonic() - self._penalty_set_at
        decayed = self._penalty * max(0.0, 1.0 - (elapsed / self._penalty_decay))
        if decayed <= 0.001:
            self._penalty = 0.0
            return self._base_interval
        return self._base_interval + decayed

    def _wait_for_interval(self) -> None:
        if self._base_interval <= 0.0 and self._penalty <= 0.0:
            return
        while True:
            with self._interval_lock:
                now = time.monotonic()
                interval = self._current_interval_locked()
                start_at = max(now, self._next_allowed_at)
                self._next_allowed_at = start_at + interval
            sleep_for = start_at - now
            if sleep_for <= 0:
                return
            time.sleep(sleep_for)
            return

    def penalize(self, seconds: Optional[float] = None) -> None:
        """Temporarily widen request spacing after a 503/429 (adaptive cooldown)."""
        if self._disabled:
            return
        bump = self._base_interval * 4.0 if seconds is None else float(seconds)
        bump = max(0.0, bump)
        with self._interval_lock:
            # Take the larger of the existing (decayed) penalty and the new bump.
            current = self._current_interval_locked() - self._base_interval
            self._penalty = min(self._max_penalty, max(current, bump))
            now = time.monotonic()
            self._penalty_set_at = now
            # Push the next allowed start forward so the *immediate* next request
            # waits out the cooldown, not just the one after it.
            self._next_allowed_at = max(self._next_allowed_at, now + self._penalty)
        logger.info(
            "[rate_limiter] penalize applied bump=%.2fs effective_penalty=%.2fs",
            bump,
            self._penalty,
        )

    # ── combined acquire/release ─────────────────────────────────────────────
    def acquire(self, timeout: Optional[float] = None) -> bool:
        if self._disabled:
            return True
        acquired = self._semaphore.acquire(timeout=timeout) if timeout else self._semaphore.acquire()
        if not acquired:
            return False
        try:
            self._wait_for_interval()
        except Exception:
            self._semaphore.release()
            raise
        return True

    def release(self) -> None:
        if self._disabled:
            return
        try:
            self._semaphore.release()
        except ValueError:
            # Released more than acquired — ignore (defensive).
            pass

    @contextlib.contextmanager
    def slot(self, timeout: Optional[float] = None) -> Iterator[None]:
        acquired = self.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError("Timed out acquiring Airbnb rate-limiter slot")
        try:
            yield
        finally:
            self.release()


_singleton_lock = threading.Lock()
_singleton: Optional[AirbnbRateLimiter] = None


def get_airbnb_rate_limiter() -> AirbnbRateLimiter:
    """Return the process-global Airbnb rate limiter (lazily constructed)."""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = AirbnbRateLimiter(
                min_interval_seconds=_env_float("AIRBNB_MIN_REQUEST_INTERVAL_MS", 250.0) / 1000.0,
                max_inflight=_env_int("AIRBNB_MAX_INFLIGHT_REQUESTS", 8),
                disabled=_env_flag("AIRBNB_RATE_LIMIT_DISABLED"),
            )
            logger.info(
                "[rate_limiter] initialized min_interval=%.3fs max_inflight=%s disabled=%s",
                _singleton._base_interval,
                _singleton._max_inflight,
                _singleton._disabled,
            )
    return _singleton
