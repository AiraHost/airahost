"""One admission policy for every outbound Airbnb request.

Replaces the fixed "sleep 1s, 1.5s, 2s then give up" retry loops that each
request path carried privately. Those loops could not see each other, so N
concurrent day-query threads answered a single Airbnb overload with N
independent retry storms — more load precisely when Airbnb was asking for less.

What this controller does instead:

  * **Smooths starts globally.** One token-spacing gate across all request
    classes, so the *aggregate* start rate is bounded no matter how many report
    threads are running.
  * **Caps in-flight work per class and in aggregate.** ``search``, ``pdp``,
    ``browser_navigation`` and ``session_refresh`` each have a ceiling, under a
    shared aggregate ceiling that no combination can exceed.
  * **Reacts multiplicatively, recovers additively.** A 429/503 or an
    authoritative block halves the permitted rate and concurrency and starts a
    jittered cooldown. Recovery adds back one step at a time, and only after a
    sustained healthy window — so the worker cannot walk straight back into the
    limit it just hit.
  * **Honors ``Retry-After``** (delta-seconds and HTTP-date), falling back to
    exponential backoff with full jitter when the header is absent or invalid.
  * **Opens a circuit** after repeated overload/block signals. While open,
    admission fails fast: callers defer or fail the operation rather than
    escalating into a browser stampede. Exactly one half-open probe is allowed
    after cooldown.

**Scope.** This is a process-local policy. Running more than one worker process
against Airbnb concurrently therefore needs the budget split between them: set
``AIRBNB_ADMISSION_INSTANCES`` to the number of concurrent worker instances and
each process takes a proportional share of the rate and concurrency ceilings.
That is a static partition, not dynamic coordination — a shared lease/token
gate is deliberately *not* implemented here, so a deployment that cannot
declare its instance count must run a single worker instance. The effective,
non-secret configuration is logged at startup.

None of the numbers below are a measured Airbnb limit. They are conservative
starting points; the runtime adapts from them. See
``worker.core.airbnb_calibration`` for the bounded way to validate an envelope
in a specific deployment.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import random
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterator, Optional

from worker.core import scrape_events
from worker.core.scrape_trace import (
    CLASS_BROWSER_NAVIGATION,
    CLASS_PDP,
    CLASS_SEARCH,
    CLASS_SESSION_REFRESH,
    REQUEST_CLASSES,
    RetryBudget,
)

logger = logging.getLogger("worker.core.admission")

# ── Outcome vocabulary ───────────────────────────────────────────────────────
# Kept distinct on purpose. Collapsing these is how "Airbnb returned 503" became
# indistinguishable from "this location genuinely has no listings".
OUTCOME_SUCCESS = "success"
OUTCOME_VALID_EMPTY = "valid_empty"
OUTCOME_OVERLOAD = "overload"
OUTCOME_BLOCKED = "blocked"
OUTCOME_DEGRADED = "degraded"
OUTCOME_TRANSPORT_ERROR = "transport_error"
OUTCOME_APPLICATION_ERROR = "application_error"

# Statuses Airbnb uses to say "slow down". 503 is *also* an ordinary server
# error, so it is treated as overload for throttling purposes but never reported
# as proof of rate limiting.
OVERLOAD_STATUSES = (429, 503)
# Statuses that mean the session is not permitted, not that it is too fast.
BLOCK_STATUSES = (401, 403)

CIRCUIT_CLOSED = "closed"
CIRCUIT_OPEN = "open"
CIRCUIT_HALF_OPEN = "half_open"


class AdmissionCircuitOpen(RuntimeError):
    """Admission refused: the circuit is open after repeated overload/blocks.

    Callers must treat this as "defer this work", never as "try the browser
    instead" — escalating here is what turns one block into a navigation storm.
    """

    def __init__(self, request_class: str, reason_code: str, retry_after_seconds: float):
        self.request_class = str(request_class or "unknown")
        self.reason_code = str(reason_code or "circuit_open")
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        super().__init__(
            f"Airbnb admission circuit open for {self.request_class} "
            f"(reason={self.reason_code}, retry_in={self.retry_after_seconds:.1f}s)"
        )


def parse_retry_after(value: Any, *, now: Optional[float] = None) -> Optional[float]:
    """Seconds to wait per a ``Retry-After`` header, or None if unusable.

    Accepts delta-seconds (``120``) and HTTP-dates (RFC 7231 §7.1.3). Rejects
    negatives, NaN/inf, and unparseable values so a malformed header falls back
    to ordinary backoff instead of producing a nonsense sleep.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None:
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return seconds
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    try:
        if when.tzinfo is None:
            from datetime import timezone as _tz

            when = when.replace(tzinfo=_tz.utc)
        target = when.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    reference = time.time() if now is None else float(now)
    delta = target - reference
    if not math.isfinite(delta):
        return None
    return max(0.0, delta)


def classify_response_outcome(
    status: Any = None,
    *,
    headers: Any = None,
    exception: Optional[BaseException] = None,
    auth_blocked: bool = False,
    payload_malformed: bool = False,
) -> str:
    """Map one HTTP result to the outcome vocabulary above.

    Deliberately does not treat every failure as throttling: a transport error
    and a 500 get different handling from a 429, because reacting to all of them
    by widening the rate limit would hide real bugs behind a slower scraper.
    """
    if exception is not None:
        return OUTCOME_TRANSPORT_ERROR
    if auth_blocked:
        return OUTCOME_BLOCKED
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    if status_int is None:
        return OUTCOME_DEGRADED if payload_malformed else OUTCOME_SUCCESS
    if status_int in OVERLOAD_STATUSES:
        return OUTCOME_OVERLOAD
    if status_int in BLOCK_STATUSES:
        return OUTCOME_BLOCKED
    if 200 <= status_int < 300:
        return OUTCOME_DEGRADED if payload_malformed else OUTCOME_SUCCESS
    if status_int >= 500:
        return OUTCOME_APPLICATION_ERROR
    return OUTCOME_DEGRADED


# ── Configuration ────────────────────────────────────────────────────────────


def _env_float(name: str, default: float) -> float:
    try:
        raw = str(os.getenv(name, "") or "").strip()
        return float(raw) if raw else float(default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = str(os.getenv(name, "") or "").strip()
        return int(float(raw)) if raw else int(default)
    except (TypeError, ValueError):
        return int(default)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class AdmissionConfig:
    """Validated, bounded runtime configuration. No secrets: safe to log.

    These are *ceilings*, not operating points. The protection against overload
    is the adaptive machinery — multiplicative decrease, cooldown, circuit —
    which reacts to what Airbnb actually returns. Setting a low ceiling instead
    is not extra safety; it is a fixed tax paid even when every response is a
    200, and it is what pushed reports past their runtime budget while Airbnb
    was answering normally.

    So the ceiling matches the last configuration observed to complete reports
    (4 starts/sec, 8 concurrent) and the adaptive policy cuts from there on real
    429/503/block evidence.
    """

    max_start_rate_per_sec: float = 4.0
    min_start_rate_per_sec: float = 0.2
    max_inflight: int = 8
    min_inflight: int = 1
    class_inflight_caps: Dict[str, int] = field(
        default_factory=lambda: {
            CLASS_SEARCH: 6,
            CLASS_PDP: 6,
            # Browser work is expensive and is a recovery path, but capping it at
            # 1 serialized every Playwright fallback *and* every target-extractor
            # navigation behind a single slot.
            CLASS_BROWSER_NAVIGATION: 3,
            CLASS_SESSION_REFRESH: 1,
        }
    )
    decrease_factor: float = 0.5
    recovery_interval_seconds: float = 30.0
    recovery_success_threshold: int = 20
    rate_increase_step: float = 0.25
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    circuit_failure_threshold: int = 5
    circuit_cooldown_seconds: float = 120.0
    circuit_max_cooldown_seconds: float = 900.0
    retry_budget_per_operation: int = 2
    retry_budget_per_report: int = 20
    instances: int = 1
    disabled: bool = False

    def public_fields(self) -> Dict[str, Any]:
        return {
            "max_start_rate_per_sec": round(self.max_start_rate_per_sec, 4),
            "min_start_rate_per_sec": round(self.min_start_rate_per_sec, 4),
            "max_inflight": self.max_inflight,
            "min_inflight": self.min_inflight,
            "class_inflight_caps": dict(self.class_inflight_caps),
            "decrease_factor": self.decrease_factor,
            "recovery_interval_seconds": self.recovery_interval_seconds,
            "recovery_success_threshold": self.recovery_success_threshold,
            "backoff_base_seconds": self.backoff_base_seconds,
            "backoff_max_seconds": self.backoff_max_seconds,
            "circuit_failure_threshold": self.circuit_failure_threshold,
            "circuit_cooldown_seconds": self.circuit_cooldown_seconds,
            "retry_budget_per_operation": self.retry_budget_per_operation,
            "retry_budget_per_report": self.retry_budget_per_report,
            "instances": self.instances,
            "disabled": self.disabled,
        }


def config_from_env() -> AdmissionConfig:
    """Read and clamp configuration. Every bound is enforced here, not at use."""
    # Backwards compatibility: the previous knob was a minimum spacing in ms.
    defaults = AdmissionConfig()
    legacy_interval_ms = _env_float("AIRBNB_MIN_REQUEST_INTERVAL_MS", 0.0)
    default_max_rate = (
        1000.0 / legacy_interval_ms
        if legacy_interval_ms > 0
        else defaults.max_start_rate_per_sec
    )

    instances = max(1, min(_env_int("AIRBNB_ADMISSION_INSTANCES", 1), 64))
    max_rate = _clamp(_env_float("AIRBNB_MAX_START_RATE_PER_SEC", default_max_rate), 0.05, 20.0)
    min_rate = _clamp(_env_float("AIRBNB_MIN_START_RATE_PER_SEC", 0.2), 0.01, max_rate)
    max_inflight = max(
        1, min(_env_int("AIRBNB_MAX_INFLIGHT_REQUESTS", defaults.max_inflight), 32)
    )
    min_inflight = max(1, min(_env_int("AIRBNB_MIN_INFLIGHT_REQUESTS", 1), max_inflight))

    # Static partition across declared worker instances (see module docstring).
    if instances > 1:
        max_rate = max(min_rate, max_rate / instances)
        max_inflight = max(min_inflight, int(max_inflight // instances) or 1)

    default_caps = defaults.class_inflight_caps
    caps = {
        CLASS_SEARCH: max(
            1,
            min(_env_int("AIRBNB_MAX_INFLIGHT_SEARCH", default_caps[CLASS_SEARCH]), max_inflight),
        ),
        CLASS_PDP: max(
            1, min(_env_int("AIRBNB_MAX_INFLIGHT_PDP", default_caps[CLASS_PDP]), max_inflight)
        ),
        CLASS_BROWSER_NAVIGATION: max(
            1,
            min(
                _env_int("AIRBNB_MAX_INFLIGHT_BROWSER", default_caps[CLASS_BROWSER_NAVIGATION]),
                max_inflight,
            ),
        ),
        CLASS_SESSION_REFRESH: max(
            1,
            min(
                _env_int(
                    "AIRBNB_MAX_INFLIGHT_SESSION_REFRESH", default_caps[CLASS_SESSION_REFRESH]
                ),
                max_inflight,
            ),
        ),
    }

    return AdmissionConfig(
        max_start_rate_per_sec=max_rate,
        min_start_rate_per_sec=min_rate,
        max_inflight=max_inflight,
        min_inflight=min_inflight,
        class_inflight_caps=caps,
        decrease_factor=_clamp(_env_float("AIRBNB_OVERLOAD_DECREASE_FACTOR", 0.5), 0.1, 0.95),
        recovery_interval_seconds=_clamp(
            _env_float("AIRBNB_RECOVERY_INTERVAL_SECONDS", 30.0), 1.0, 3600.0
        ),
        recovery_success_threshold=max(
            1, min(_env_int("AIRBNB_RECOVERY_SUCCESS_THRESHOLD", 20), 10000)
        ),
        rate_increase_step=_clamp(_env_float("AIRBNB_RATE_INCREASE_STEP", 0.25), 0.01, 5.0),
        backoff_base_seconds=_clamp(_env_float("AIRBNB_BACKOFF_BASE_SECONDS", 1.0), 0.05, 60.0),
        backoff_max_seconds=_clamp(_env_float("AIRBNB_BACKOFF_MAX_SECONDS", 60.0), 1.0, 900.0),
        circuit_failure_threshold=max(
            1, min(_env_int("AIRBNB_CIRCUIT_FAILURE_THRESHOLD", 5), 100)
        ),
        circuit_cooldown_seconds=_clamp(
            _env_float("AIRBNB_CIRCUIT_COOLDOWN_SECONDS", 120.0), 1.0, 3600.0
        ),
        circuit_max_cooldown_seconds=_clamp(
            _env_float("AIRBNB_CIRCUIT_MAX_COOLDOWN_SECONDS", 900.0), 1.0, 7200.0
        ),
        retry_budget_per_operation=max(
            0, min(_env_int("AIRBNB_RETRY_BUDGET_PER_OPERATION", 2), 20)
        ),
        retry_budget_per_report=max(0, min(_env_int("AIRBNB_RETRY_BUDGET_PER_REPORT", 20), 1000)),
        instances=instances,
        disabled=_env_flag("AIRBNB_RATE_LIMIT_DISABLED", False),
    )


@dataclass
class AdmissionTicket:
    """What one admitted request learned on the way in."""

    request_class: str
    attempt_id: str
    limiter_wait_ms: int
    permitted_rate: float
    permitted_concurrency: int
    circuit_state: str
    # True when this request is *the* half-open probe. Release clears the probe
    # slot so an abandoned probe (an exception before any record_* call) cannot
    # wedge the circuit half-open forever.
    is_half_open_probe: bool = False


class AirbnbAdmissionController:
    """Thread-safe adaptive admission policy shared by every request path."""

    def __init__(self, config: Optional[AdmissionConfig] = None) -> None:
        self.config = config or AdmissionConfig()
        self._cond = threading.Condition()

        self._permitted_rate = float(self.config.max_start_rate_per_sec)
        self._permitted_concurrency = float(self.config.max_inflight)
        self._next_start_at = 0.0

        self._inflight_total = 0
        self._inflight_by_class: Dict[str, int] = {c: 0 for c in REQUEST_CLASSES}

        self._cooldown_until = 0.0
        self._consecutive_failures = 0
        self._healthy_successes = 0
        self._last_penalty_at = 0.0
        self._last_increase_at = time.monotonic()

        self._circuit_state = CIRCUIT_CLOSED
        self._circuit_open_until = 0.0
        self._circuit_cooldown = float(self.config.circuit_cooldown_seconds)
        self._half_open_probe_taken = False

        # Counters for the before/after report; cheap and always on.
        self._counters: Dict[str, int] = {
            "admitted": 0,
            "overload": 0,
            "blocked": 0,
            "degraded": 0,
            "transport_error": 0,
            "success": 0,
            "circuit_openings": 0,
        }

    # ── introspection ────────────────────────────────────────────────────────
    def snapshot(self) -> Dict[str, Any]:
        with self._cond:
            return {
                "permitted_rate_per_sec": round(self._permitted_rate, 4),
                "permitted_concurrency": int(self._permitted_concurrency),
                "inflight_total": self._inflight_total,
                "inflight_by_class": dict(self._inflight_by_class),
                "circuit_state": self._circuit_state,
                "cooldown_remaining_ms": max(
                    0, round((self._cooldown_until - time.monotonic()) * 1000)
                ),
                "consecutive_failures": self._consecutive_failures,
                **{f"count_{k}": v for k, v in self._counters.items()},
            }

    def make_retry_budget(self) -> RetryBudget:
        return RetryBudget(
            per_operation=self.config.retry_budget_per_operation,
            per_report=self.config.retry_budget_per_report,
        )

    # ── admission ────────────────────────────────────────────────────────────
    def _class_cap_locked(self, request_class: str) -> int:
        configured = self.config.class_inflight_caps.get(request_class, self.config.max_inflight)
        # A class can never exceed the (possibly reduced) aggregate ceiling.
        return max(1, min(int(configured), int(self._permitted_concurrency)))

    def _claim_circuit_admission_locked(self, request_class: str) -> bool:
        """Raise if the circuit forbids admission; return True if this is the probe.

        Promotes open -> half-open once the cooldown has elapsed. Called exactly
        once per :meth:`acquire`, before the capacity wait, so the thread holding
        the probe is not rejected by its own claim on a later loop iteration.
        """
        if self._circuit_state == CIRCUIT_CLOSED:
            return False
        now = time.monotonic()
        half_open_announced = False
        if self._circuit_state == CIRCUIT_OPEN:
            if now < self._circuit_open_until:
                raise AdmissionCircuitOpen(
                    request_class, "circuit_open", self._circuit_open_until - now
                )
            self._circuit_state = CIRCUIT_HALF_OPEN
            self._half_open_probe_taken = False
            half_open_announced = True
        if self._half_open_probe_taken:
            # Exactly one probe is in flight; everyone else waits out the
            # cooldown rather than piling onto a possibly-still-blocked site.
            raise AdmissionCircuitOpen(
                request_class, "circuit_half_open_probe_in_flight", self._circuit_cooldown
            )
        self._half_open_probe_taken = True
        if half_open_announced:
            scrape_events.emit(
                scrape_events.CIRCUIT_HALF_OPEN,
                request_class=request_class,
                circuit_state=CIRCUIT_HALF_OPEN,
            )
        return True

    def acquire(self, request_class: str, *, timeout: Optional[float] = None) -> AdmissionTicket:
        """Block until one request of ``request_class`` may start.

        Raises :class:`AdmissionCircuitOpen` when the circuit forbids it, and
        ``TimeoutError`` when ``timeout`` elapses first.
        """
        from worker.core.scrape_trace import new_attempt_id

        rc = request_class if request_class in self._inflight_by_class else CLASS_SEARCH
        attempt_id = new_attempt_id()
        if self.config.disabled:
            return AdmissionTicket(
                rc, attempt_id, 0, self._permitted_rate, self.config.max_inflight, CIRCUIT_CLOSED
            )

        started = time.monotonic()
        deadline = None if timeout is None else started + float(timeout)

        with self._cond:
            is_probe = self._claim_circuit_admission_locked(rc)
            while True:
                now = time.monotonic()
                blocked_until = max(self._cooldown_until, 0.0)
                has_capacity = (
                    self._inflight_total < int(self._permitted_concurrency)
                    and self._inflight_by_class[rc] < self._class_cap_locked(rc)
                )
                if now >= blocked_until and has_capacity:
                    break
                wait_for = 0.05
                if now < blocked_until:
                    wait_for = min(blocked_until - now, 1.0)
                if deadline is not None:
                    remaining = deadline - now
                    if remaining <= 0:
                        if is_probe:
                            self._half_open_probe_taken = False
                        raise TimeoutError(f"Timed out waiting for Airbnb admission ({rc})")
                    wait_for = min(wait_for, remaining)
                self._cond.wait(wait_for)

            self._inflight_total += 1
            self._inflight_by_class[rc] += 1
            self._counters["admitted"] += 1
            # Global start spacing, computed inside the lock so concurrent
            # admissions cannot both claim the same slot.
            interval = 1.0 / max(self._permitted_rate, 0.001)
            now = time.monotonic()
            start_at = max(now, self._next_start_at)
            self._next_start_at = start_at + interval
            permitted_rate = self._permitted_rate
            permitted_concurrency = int(self._permitted_concurrency)
            circuit_state = self._circuit_state

        sleep_for = start_at - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)

        return AdmissionTicket(
            request_class=rc,
            attempt_id=attempt_id,
            limiter_wait_ms=max(0, round((time.monotonic() - started) * 1000)),
            permitted_rate=permitted_rate,
            permitted_concurrency=permitted_concurrency,
            circuit_state=circuit_state,
            is_half_open_probe=is_probe,
        )

    def _release_half_open_probe_locked(self) -> None:
        if self._circuit_state == CIRCUIT_HALF_OPEN:
            self._half_open_probe_taken = False

    def release(self, ticket: AdmissionTicket) -> None:
        if self.config.disabled:
            return
        with self._cond:
            rc = ticket.request_class
            if self._inflight_total > 0:
                self._inflight_total -= 1
            if self._inflight_by_class.get(rc, 0) > 0:
                self._inflight_by_class[rc] -= 1
            if ticket.is_half_open_probe:
                self._release_half_open_probe_locked()
            self._cond.notify_all()

    @contextlib.contextmanager
    def slot(
        self, request_class: str = CLASS_SEARCH, *, timeout: Optional[float] = None
    ) -> Iterator[AdmissionTicket]:
        ticket = self.acquire(request_class, timeout=timeout)
        try:
            yield ticket
        finally:
            self.release(ticket)

    # ── feedback ─────────────────────────────────────────────────────────────
    def record_success(self, request_class: str = CLASS_SEARCH) -> None:
        """A healthy response. Drives slow additive recovery."""
        if self.config.disabled:
            return
        adjusted: Optional[Dict[str, Any]] = None
        closed = False
        with self._cond:
            self._counters["success"] += 1
            self._consecutive_failures = 0
            self._healthy_successes += 1
            if self._circuit_state != CIRCUIT_CLOSED:
                self._circuit_state = CIRCUIT_CLOSED
                self._circuit_open_until = 0.0
                self._half_open_probe_taken = False
                self._circuit_cooldown = float(self.config.circuit_cooldown_seconds)
                closed = True
            now = time.monotonic()
            healthy_window_elapsed = (
                now - max(self._last_increase_at, self._last_penalty_at)
                >= self.config.recovery_interval_seconds
            )
            if (
                healthy_window_elapsed
                and self._healthy_successes >= self.config.recovery_success_threshold
            ):
                before_rate = self._permitted_rate
                before_conc = int(self._permitted_concurrency)
                self._permitted_rate = min(
                    self.config.max_start_rate_per_sec,
                    self._permitted_rate + self.config.rate_increase_step,
                )
                self._permitted_concurrency = min(
                    float(self.config.max_inflight), self._permitted_concurrency + 1.0
                )
                self._healthy_successes = 0
                self._last_increase_at = now
                if before_rate != self._permitted_rate or before_conc != int(
                    self._permitted_concurrency
                ):
                    adjusted = {
                        "direction": "increase",
                        "permitted_rate_per_sec": round(self._permitted_rate, 4),
                        "permitted_concurrency": int(self._permitted_concurrency),
                        "previous_rate_per_sec": round(before_rate, 4),
                        "previous_concurrency": before_conc,
                    }
                self._cond.notify_all()
        if closed:
            scrape_events.emit(
                scrape_events.CIRCUIT_CLOSED,
                request_class=request_class,
                circuit_state=CIRCUIT_CLOSED,
            )
        if adjusted:
            scrape_events.emit(scrape_events.LIMIT_ADJUSTED, request_class=request_class, **adjusted)

    def record_overload(
        self,
        request_class: str = CLASS_SEARCH,
        *,
        status: Any = None,
        retry_after: Any = None,
        reason_code: str = "http_overload",
    ) -> float:
        """A 429/503. Returns the cooldown in seconds now in force."""
        return self._penalize(
            request_class,
            outcome=OUTCOME_OVERLOAD,
            status=status,
            retry_after=retry_after,
            reason_code=reason_code,
        )

    def record_block(
        self,
        request_class: str = CLASS_SEARCH,
        *,
        reason_code: str = "blocked",
        counts_toward_circuit: bool = True,
    ) -> float:
        """Authoritative challenge/auth-block evidence. Same conservative response.

        ``counts_toward_circuit=False`` still slows the worker down but does not
        advance the breaker. Use it where a block is a *routine* signal to try
        the next method in a working cascade — a challenged direct-HTTP replay
        that the browser then serves successfully is normal operation, not
        evidence that Airbnb has cut the worker off. Counting those would open
        the circuit during healthy scraping and turn an ordinary fallback into a
        failed report.
        """
        return self._penalize(
            request_class,
            outcome=OUTCOME_BLOCKED,
            status=None,
            retry_after=None,
            reason_code=reason_code,
            counts_toward_circuit=counts_toward_circuit,
        )

    def record_neutral_failure(
        self, request_class: str = CLASS_SEARCH, *, outcome: str = OUTCOME_DEGRADED
    ) -> None:
        """A malformed payload or transport error: counted, but not throttled.

        Widening the rate limit for these would slow the scraper down in
        response to bugs on our side rather than pressure from Airbnb's.
        """
        if self.config.disabled:
            return
        with self._cond:
            key = "transport_error" if outcome == OUTCOME_TRANSPORT_ERROR else "degraded"
            self._counters[key] = self._counters.get(key, 0) + 1
            self._healthy_successes = 0
            self._release_half_open_probe_locked()
            self._cond.notify_all()

    def _penalize(
        self,
        request_class: str,
        *,
        outcome: str,
        status: Any,
        retry_after: Any,
        reason_code: str,
        counts_toward_circuit: bool = True,
    ) -> float:
        if self.config.disabled:
            return 0.0
        opened = False
        with self._cond:
            self._counters[
                "overload" if outcome == OUTCOME_OVERLOAD else "blocked"
            ] += 1
            if counts_toward_circuit:
                self._consecutive_failures += 1
            self._healthy_successes = 0
            now = time.monotonic()
            self._last_penalty_at = now

            before_rate = self._permitted_rate
            before_conc = int(self._permitted_concurrency)
            self._permitted_rate = max(
                self.config.min_start_rate_per_sec,
                self._permitted_rate * self.config.decrease_factor,
            )
            self._permitted_concurrency = max(
                float(self.config.min_inflight),
                self._permitted_concurrency * self.config.decrease_factor,
            )

            honored = parse_retry_after(retry_after)
            if honored is not None:
                cooldown = min(honored, self.config.backoff_max_seconds)
                backoff_source = "retry_after"
            else:
                # Full jitter: uniform in [0, capped exponential]. Two workers
                # penalized at the same instant therefore resume at different
                # times instead of retrying in lockstep.
                ceiling = min(
                    self.config.backoff_max_seconds,
                    self.config.backoff_base_seconds * (2 ** min(self._consecutive_failures, 10)),
                )
                cooldown = random.uniform(0.0, ceiling)
                backoff_source = "exponential_full_jitter"

            self._cooldown_until = max(self._cooldown_until, now + cooldown)
            # Push the spacing gate out too, so the first request after cooldown
            # does not start early on a stale token.
            self._next_start_at = max(self._next_start_at, self._cooldown_until)

            if self._circuit_state == CIRCUIT_HALF_OPEN and counts_toward_circuit:
                # The probe failed: reopen with a longer cooldown.
                self._circuit_cooldown = min(
                    self.config.circuit_max_cooldown_seconds, self._circuit_cooldown * 2.0
                )
                self._open_circuit_locked(now)
                opened = True
            elif (
                counts_toward_circuit
                and self._consecutive_failures >= self.config.circuit_failure_threshold
            ):
                self._open_circuit_locked(now)
                opened = True

            adjusted = {
                "direction": "decrease",
                "permitted_rate_per_sec": round(self._permitted_rate, 4),
                "permitted_concurrency": int(self._permitted_concurrency),
                "previous_rate_per_sec": round(before_rate, 4),
                "previous_concurrency": before_conc,
            }
            circuit_state = self._circuit_state
            self._cond.notify_all()

        scrape_events.emit(
            scrape_events.LIMIT_ADJUSTED,
            request_class=request_class,
            reason_code=reason_code,
            status=status,
            outcome=outcome,
            **adjusted,
        )
        scrape_events.emit(
            scrape_events.COOLDOWN_STARTED,
            request_class=request_class,
            reason_code=reason_code,
            status=status,
            outcome=outcome,
            cooldown_seconds=round(cooldown, 3),
            backoff_source=backoff_source,
            circuit_state=circuit_state,
        )
        if opened:
            scrape_events.emit(
                scrape_events.CIRCUIT_OPENED,
                level=logging.WARNING,
                request_class=request_class,
                reason_code=reason_code,
                circuit_state=CIRCUIT_OPEN,
                cooldown_seconds=round(self._circuit_cooldown, 3),
            )
        return cooldown

    def _open_circuit_locked(self, now: float) -> None:
        self._circuit_state = CIRCUIT_OPEN
        self._circuit_open_until = now + self._circuit_cooldown
        self._half_open_probe_taken = False
        self._counters["circuit_openings"] += 1
        self._cooldown_until = max(self._cooldown_until, self._circuit_open_until)

    def backoff_seconds(self, attempt: int, retry_after: Any = None) -> float:
        """Backoff for one retry: ``Retry-After`` when valid, else full jitter."""
        honored = parse_retry_after(retry_after)
        if honored is not None:
            return min(honored, self.config.backoff_max_seconds)
        ceiling = min(
            self.config.backoff_max_seconds,
            self.config.backoff_base_seconds * (2 ** max(0, min(int(attempt), 10))),
        )
        return random.uniform(0.0, ceiling)


_singleton_lock = threading.Lock()
_singleton: Optional[AirbnbAdmissionController] = None


def get_admission_controller() -> AirbnbAdmissionController:
    """The process-global admission controller (lazily constructed)."""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            config = config_from_env()
            _singleton = AirbnbAdmissionController(config)
            logger.info(
                "[admission] initialized %s",
                " ".join(f"{k}={v}" for k, v in config.public_fields().items()),
            )
            scrape_events.emit(scrape_events.ADMISSION_CONFIGURED, **config.public_fields())
    return _singleton


def reset_admission_controller_for_tests() -> None:
    """Drop the singleton so a test can install a differently configured one."""
    global _singleton
    with _singleton_lock:
        _singleton = None
