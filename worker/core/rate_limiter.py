"""Deprecated compatibility shim over :mod:`worker.core.admission`.

This module used to own an independent min-interval + bounded-concurrency
limiter. It now delegates to the single admission controller, because two
limiters cannot coordinate: each would enforce its own ceiling while Airbnb sees
their sum, which is how "we are under our configured limit" and "Airbnb is
returning 503" were both true at the same time.

New code should call :func:`worker.core.admission.get_admission_controller`
directly and pass an explicit request class (``search``, ``pdp``,
``browser_navigation``, ``session_refresh``); this shim admits everything as
``search``.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Iterator, Optional

from worker.core.admission import (
    AirbnbAdmissionController,
    get_admission_controller,
)
from worker.core.scrape_trace import CLASS_SEARCH

logger = logging.getLogger("worker.core.rate_limiter")


class AirbnbRateLimiterShim:
    """The pre-existing ``slot()`` / ``penalize()`` surface, backed by admission."""

    def __init__(self, controller: AirbnbAdmissionController) -> None:
        self._controller = controller

    @contextlib.contextmanager
    def slot(self, timeout: Optional[float] = None) -> Iterator[None]:
        with self._controller.slot(CLASS_SEARCH, timeout=timeout):
            yield

    def penalize(self, seconds: Optional[float] = None) -> None:
        self._controller.record_overload(
            CLASS_SEARCH, retry_after=seconds, reason_code="legacy_penalize"
        )


def get_airbnb_rate_limiter() -> AirbnbRateLimiterShim:
    """Deprecated. Returns a shim over the process-global admission controller."""
    return AirbnbRateLimiterShim(get_admission_controller())
