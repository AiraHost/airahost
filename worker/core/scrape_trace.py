"""Correlation IDs for one report's scraping work.

Three levels, because "why did this report fall back to Playwright?" needs all
three to be answerable from the log alone:

  * ``trace_id``   — one per report/job. Every event a report produces carries it.
  * ``search_id``  — one per *logical* listing search. Stable across that
    search's direct-HTTP attempt, any raw/rendered-HTML attempt, and a
    Playwright escalation, so the fallback chain is one queryable sequence.
  * ``attempt_id`` — one per network attempt. Never reused, including retries.

State lives in :mod:`contextvars`, not module globals, so concurrent report
threads never see each other's IDs. ``contextvars`` are *not* inherited by
threads started with ``threading.Thread``/``ThreadPoolExecutor.submit``, so
:func:`propagate` must wrap any callable handed to another thread — see
``worker.core.concurrent_runner``.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import os
import socket
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Optional

# Request classes recognised by the admission policy. Kept here (not in
# admission.py) because events reference them and admission imports this module.
CLASS_SEARCH = "search"
CLASS_PDP = "pdp"
CLASS_BROWSER_NAVIGATION = "browser_navigation"
CLASS_SESSION_REFRESH = "session_refresh"

REQUEST_CLASSES = (
    CLASS_SEARCH,
    CLASS_PDP,
    CLASS_BROWSER_NAVIGATION,
    CLASS_SESSION_REFRESH,
)

# Where a payload came from. `raw_http_html` and `rendered_html` are deliberately
# distinct: the first is a plain HTTP GET, the second is markup a browser
# produced. Conflating them would make "did this need a browser?" unanswerable.
SOURCE_DIRECT_JSON = "direct_json"
SOURCE_RAW_HTTP_HTML = "raw_http_html"
SOURCE_RENDERED_HTML = "rendered_html"
SOURCE_PLAYWRIGHT_CAPTURE = "playwright_capture"


def _short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _resolve_worker_instance_id() -> str:
    explicit = str(os.getenv("WORKER_INSTANCE_ID", "") or "").strip()
    if explicit:
        return explicit[:64]
    lane = str(os.getenv("WORKER_LANE", "interactive") or "interactive").strip()
    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown-host"
    return f"{host}:{lane}:{os.getpid()}"


_WORKER_INSTANCE_ID = _resolve_worker_instance_id()


def worker_instance_id() -> str:
    """Stable identity for this worker process (host:lane:pid unless overridden)."""
    return _WORKER_INSTANCE_ID


class RetryBudget:
    """Bounded retries for one report, enforced per operation *and* in aggregate.

    Without the aggregate half, N concurrent day-query threads each retrying
    within their own budget multiply a single Airbnb overload into N times the
    load — the exact behaviour that turns one 503 into a storm.
    """

    def __init__(self, per_operation: int, per_report: int) -> None:
        self.per_operation = max(0, int(per_operation))
        self.per_report = max(0, int(per_report))
        self._lock = threading.Lock()
        self._by_operation: Dict[str, int] = {}
        self._total = 0

    def try_consume(self, operation_key: str) -> bool:
        """Claim one retry for ``operation_key``. False means: stop retrying."""
        key = str(operation_key or "unknown")
        with self._lock:
            if self._total >= self.per_report:
                return False
            if self._by_operation.get(key, 0) >= self.per_operation:
                return False
            self._by_operation[key] = self._by_operation.get(key, 0) + 1
            self._total += 1
            return True

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "retries_used": self._total,
                "retry_budget_per_report": self.per_report,
                "retry_budget_per_operation": self.per_operation,
            }


@dataclass(frozen=True)
class TraceContext:
    """Per-report identity. Immutable: derived scopes replace, never mutate."""

    trace_id: str
    report_id: Optional[str] = None
    target_listing_id: Optional[str] = None
    worker_instance_id: str = field(default_factory=worker_instance_id)
    retry_budget: Optional[RetryBudget] = None


@dataclass(frozen=True)
class SearchContext:
    """Identity of one logical search, stable across its fallback chain."""

    search_id: str
    operation: str
    listing_id: Optional[str] = None
    checkin: Optional[str] = None
    checkout: Optional[str] = None
    offset: Optional[int] = None


_trace_var: contextvars.ContextVar[Optional[TraceContext]] = contextvars.ContextVar(
    "airahost_scrape_trace", default=None
)
_search_var: contextvars.ContextVar[Optional[SearchContext]] = contextvars.ContextVar(
    "airahost_scrape_search", default=None
)


def current_trace() -> Optional[TraceContext]:
    return _trace_var.get()


def current_search() -> Optional[SearchContext]:
    return _search_var.get()


def current_retry_budget() -> Optional[RetryBudget]:
    trace = _trace_var.get()
    return trace.retry_budget if trace is not None else None


@contextlib.contextmanager
def trace_scope(
    *,
    report_id: Optional[str] = None,
    target_listing_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    retry_budget: Optional[RetryBudget] = None,
) -> Iterator[TraceContext]:
    """Open one report-scoped trace. Nested calls inherit the outer trace_id."""
    existing = _trace_var.get()
    ctx = TraceContext(
        trace_id=trace_id or (existing.trace_id if existing else _short_id("trc")),
        report_id=report_id if report_id is not None else (existing.report_id if existing else None),
        target_listing_id=(
            target_listing_id
            if target_listing_id is not None
            else (existing.target_listing_id if existing else None)
        ),
        retry_budget=(
            retry_budget
            if retry_budget is not None
            else (existing.retry_budget if existing else None)
        ),
    )
    token = _trace_var.set(ctx)
    try:
        yield ctx
    finally:
        _trace_var.reset(token)


@contextlib.contextmanager
def search_scope(
    *,
    operation: str,
    listing_id: Optional[str] = None,
    checkin: Optional[str] = None,
    checkout: Optional[str] = None,
    offset: Optional[int] = None,
    search_id: Optional[str] = None,
) -> Iterator[SearchContext]:
    """Open one logical search. Every attempt inside shares this ``search_id``."""
    ctx = SearchContext(
        search_id=search_id or _short_id("sch"),
        operation=str(operation or "unknown"),
        listing_id=str(listing_id) if listing_id is not None else None,
        checkin=checkin,
        checkout=checkout,
        offset=int(offset) if offset is not None else None,
    )
    token = _search_var.set(ctx)
    try:
        yield ctx
    finally:
        _search_var.reset(token)


def new_attempt_id() -> str:
    """A fresh ID for one network attempt. Retries must call this again."""
    return _short_id("att")


def context_fields() -> Dict[str, Any]:
    """Trace/search fields for a structured event. Empty when no scope is open."""
    out: Dict[str, Any] = {}
    trace = _trace_var.get()
    if trace is not None:
        out["trace_id"] = trace.trace_id
        out["worker_instance_id"] = trace.worker_instance_id
        if trace.report_id:
            out["report_id"] = trace.report_id
        if trace.target_listing_id:
            out["target_listing_id"] = trace.target_listing_id
    search = _search_var.get()
    if search is not None:
        out["search_id"] = search.search_id
        out["operation"] = search.operation
        if search.listing_id:
            out["listing_id"] = search.listing_id
        if search.checkin:
            out["checkin"] = search.checkin
        if search.checkout:
            out["checkout"] = search.checkout
        if search.offset is not None:
            out["offset"] = search.offset
    return out


def propagate(func: Callable[..., Any]) -> Callable[..., Any]:
    """Bind the *calling* thread's context to ``func`` for another thread.

    ``contextvars`` do not cross thread boundaries on their own, so a day-query
    submitted to a pool would otherwise lose its report's trace_id and emit
    orphan events.

    The captured values are re-applied inside a *fresh* context on every call.
    A single ``Context`` cannot be entered twice at once, so handing one wrapper
    to several concurrent tasks would otherwise raise ``RuntimeError: cannot
    enter context``. Copying per call keeps the wrapper reusable and stops one
    task's ``set()`` from leaking into a sibling.
    """
    captured = list(contextvars.copy_context().items())

    @functools.wraps(func)
    def _run(*args: Any, **kwargs: Any) -> Any:
        def _call() -> Any:
            for var, value in captured:
                var.set(value)
            return func(*args, **kwargs)

        return contextvars.copy_context().run(_call)

    return _run
