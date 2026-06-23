"""Concurrency helpers for day-by-day scraping queries."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import os
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


try:
    _configured_max_workers = int(os.getenv("MAX_SCRAPER_WORKERS", "12"))
except Exception:
    _configured_max_workers = 12
MAX_SCRAPER_WORKERS = max(1, min(_configured_max_workers, 15))


@dataclass
class ConcurrentRunState:
    completed_indices: List[int]
    early_stop_triggered: bool
    consecutive_empty_peak: int
    cancelled_pending_count: int


def _invoke_query_func(query_func: Callable[..., Any], args: Any) -> Any:
    if isinstance(args, dict):
        return query_func(**args)
    if isinstance(args, (tuple, list)):
        return query_func(*args)
    return query_func(args)


def execute_day_queries_concurrently(
    query_func: Callable[..., Any],
    args_list: Sequence[Any],
    max_workers: int = 2,
    early_stop_threshold: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Tuple[List[Any], ConcurrentRunState]:
    """
    Execute day-query calls in parallel while returning chronological results.

    Returns:
      - ordered results list (chronological by args_list index)
      - ConcurrentRunState with aggregate execution telemetry
    """
    if max_workers < 1:
        raise ValueError("max_workers must be >= 1")
    max_workers = min(int(max_workers), MAX_SCRAPER_WORKERS)

    total = len(args_list)
    if total == 0:
        return [], ConcurrentRunState([], False, 0, 0)

    progress_lock = threading.Lock()
    index_by_future: Dict[Any, int] = {}
    completed_results: Dict[int, Any] = {}
    ordered_results: List[Any] = []
    completed_indices: List[int] = []

    completed_count = 0
    next_expected_index = 0
    consecutive_empty = 0
    consecutive_empty_peak = 0
    cancelled_pending_count = 0
    early_stop_triggered = False

    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        for idx, args in enumerate(args_list):
            future = executor.submit(_invoke_query_func, query_func, args)
            index_by_future[future] = idx

        pending = set(index_by_future.keys())
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                idx = index_by_future[future]
                error = future.exception()
                if error is not None:
                    raise error

                completed_results[idx] = future.result()
                completed_count += 1
                if progress_callback is not None:
                    with progress_lock:
                        try:
                            progress_callback(completed_count, total)
                        except Exception:
                            pass

            while next_expected_index in completed_results:
                result = completed_results.pop(next_expected_index)
                ordered_results.append(result)
                completed_indices.append(next_expected_index)

                median_price = getattr(result, "median_price", None)
                if median_price is None and isinstance(result, dict):
                    median_price = result.get("median_price")

                if median_price is None:
                    consecutive_empty += 1
                    consecutive_empty_peak = max(
                        consecutive_empty_peak, consecutive_empty
                    )
                else:
                    consecutive_empty = 0

                next_expected_index += 1
                if (
                    early_stop_threshold is not None
                    and consecutive_empty >= early_stop_threshold
                ):
                    early_stop_triggered = True
                    break

            if early_stop_triggered:
                for pending_future in pending:
                    if pending_future.cancel():
                        cancelled_pending_count += 1
                break
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    return ordered_results, ConcurrentRunState(
        completed_indices=completed_indices,
        early_stop_triggered=early_stop_triggered,
        consecutive_empty_peak=consecutive_empty_peak,
        cancelled_pending_count=cancelled_pending_count,
    )
