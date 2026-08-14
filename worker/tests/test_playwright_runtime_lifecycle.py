"""Playwright runtime ownership, transactional startup, and WinError 1455.

Production incident (docs/scraper implementation prompts/error log/log-2026-8-7):

    direct StaysSearch empty -> browser fallback
      -> async_playwright().start() -> CreateProcess
      -> OSError [WinError 1455] The paging file is too small
      -> Playwright's own Connection.run() task keeps that exception
      -> "Task exception was never retrieved", logged twice.

Two failures in one: the worker spawned a Playwright driver subprocess per
logical client (pool slot, fork, per-phase pool rebuild) and leaked one more on
every failed CDP connect, until the host could not commit memory for another
process; and the resulting async failure was neither retrieved nor classified,
so it reached the report as a generic "service is busy".

These tests reproduce that deterministically — no real Playwright, no real CDP
browser, no real memory exhaustion, no paging-file changes — and pin the
runtime ownership contract that replaced it.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
import warnings
from typing import List

import pytest

from worker.scraper import playwright_runtime as pr
from worker.scraper.playwright_scraper import PlaywrightScraper
from worker.scraper.scraper_errors import (
    BrowserRuntimeResourceExhausted,
    BrowserRuntimeUnavailable,
)
from worker.tests.playwright_doubles import PlaywrightFactory as _PlaywrightFactory
from worker.tests.playwright_doubles import winerror_1455

CDP = "http://127.0.0.1:9222"
CDP_ALT = "http://127.0.0.1:9223"


# ── 1-2. Classification ──────────────────────────────────────────────────────


def test_winerror_1455_at_startup_reproduces_the_classified_failure(install_playwright):
    factory = install_playwright(_PlaywrightFactory(start_error=winerror_1455()))
    lease = pr.acquire_runtime(CDP)

    with pytest.raises(BrowserRuntimeResourceExhausted) as ctx:
        lease.ensure_started(operation="browser_fallback")

    assert factory.start_calls == 1
    assert ctx.value.reason_code == "process_spawn_resource_exhausted"
    assert isinstance(ctx.value.__cause__, OSError)
    assert ctx.value.__cause__.winerror == 1455


def test_nested_winerror_is_recognized_and_unrelated_oserrors_are_not():
    try:
        try:
            raise winerror_1455()
        except OSError as inner:
            raise RuntimeError("playwright driver failed to start") from inner
    except RuntimeError as chained:
        assert pr.is_process_spawn_resource_exhaustion(chained)

    # Nothing else may reach the fail-fast breaker.
    assert not pr.is_process_spawn_resource_exhaustion(OSError(13, "Permission denied"))
    assert not pr.is_process_spawn_resource_exhaustion(
        OSError(2, "The system cannot find the file specified")
    )
    assert not pr.is_process_spawn_resource_exhaustion(
        RuntimeError("Timeout 15000ms exceeded connecting over CDP")
    )
    assert not pr.is_process_spawn_resource_exhaustion(
        RuntimeError("Target page, context or browser has been closed")
    )


def test_message_only_form_without_winerror_is_still_recognized():
    # Some wrappers rebuild the OSError and lose .winerror.
    assert pr.is_process_spawn_resource_exhaustion(
        OSError("The paging file is too small for this operation to complete")
    )


# ── 3-4. Transactional startup ───────────────────────────────────────────────


def test_failed_startup_publishes_no_state_and_no_orphan_thread(install_playwright):
    threads_before = len(pr.live_runtime_threads())
    install_playwright(_PlaywrightFactory(start_error=winerror_1455()))
    lease = pr.acquire_runtime(CDP)

    with pytest.raises(BrowserRuntimeResourceExhausted):
        lease.ensure_started()

    runtime = lease.runtime
    assert runtime.is_started is False
    assert runtime.loop is None
    assert runtime._pw is None
    assert runtime._browser is None
    assert runtime._context is None
    assert pr.active_runtime_count() == 0
    # The reserved cap slot is handed back, so a failed initializer cannot
    # poison the registry against later recovery.
    assert pr._STARTING == set()
    _wait_for(lambda: len(pr.live_runtime_threads()) == threads_before)


def test_partial_start_then_cdp_failure_stops_the_driver_exactly_once(
    install_playwright,
):
    factory = install_playwright(
        _PlaywrightFactory(connect_error=RuntimeError("connect ECONNREFUSED"))
    )
    lease = pr.acquire_runtime(CDP)

    with pytest.raises(BrowserRuntimeUnavailable) as ctx:
        lease.ensure_started()

    assert ctx.value.reason_code == "cdp_connect_failed"
    # The driver that *did* start is stopped once — the leak that walked the
    # host into WinError 1455 was leaving it running and starting another.
    assert len(factory.instances) == 1
    assert factory.instances[0].stop_calls == 1
    assert lease.runtime.is_started is False
    assert lease.runtime._pw is None


# ── 5. Cancellation / synchronous timeout ────────────────────────────────────


def test_sync_timeout_cancels_and_drains_the_submitted_coroutine(install_playwright):
    install_playwright(_PlaywrightFactory())
    lease = pr.acquire_runtime(CDP)
    lease.ensure_started()

    started = threading.Event()
    cancelled = threading.Event()
    finished_anyway = threading.Event()

    async def _slow():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finished_anyway.set()

    from concurrent.futures import TimeoutError as FutureTimeoutError

    with pytest.raises(FutureTimeoutError):
        lease.run(_slow(), op_name="slow_op", timeout_seconds=0.2)

    assert started.wait(timeout=5)
    assert cancelled.wait(timeout=5), "timed-out coroutine must be cancelled"
    assert not finished_anyway.is_set(), "cancelled work must not keep running"


def test_setup_failure_before_submission_closes_the_coroutine(install_playwright):
    install_playwright(_PlaywrightFactory(start_error=winerror_1455()))
    lease = pr.acquire_runtime(CDP)

    async def _never_run():
        return "unreachable"

    coro = _never_run()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(BrowserRuntimeResourceExhausted):
            lease.run(coro, op_name="browser_fallback")
        del coro
        gc.collect()

    assert not [w for w in caught if "never awaited" in str(w.message)]


# ── 6. Bounded recovery after cleanup ────────────────────────────────────────


def test_a_later_retry_after_cleanup_can_start_successfully(install_playwright):
    factory = install_playwright(_PlaywrightFactory(start_error=winerror_1455()))
    lease = pr.acquire_runtime(CDP)

    with pytest.raises(BrowserRuntimeResourceExhausted):
        lease.ensure_started()

    # The breaker is what bounds retries; a recovered host is allowed back in.
    pr.reset_resource_breaker()
    factory.start_error = None
    lease.ensure_started()

    assert lease.runtime.is_started is True
    # Start #2 began from a clean state, not from the half-started instance.
    assert factory.start_calls == 2
    assert lease.runtime.start_count == 1


# ── 7-9. Sharing, single-flight, and the runtime cap ─────────────────────────


def test_concurrent_first_use_starts_playwright_exactly_once(install_playwright):
    factory = install_playwright(_PlaywrightFactory(start_delay=0.05))
    leases = [pr.acquire_runtime(CDP) for _ in range(12)]
    barrier = threading.Barrier(len(leases))
    errors: List[BaseException] = []

    def _worker(lease):
        try:
            barrier.wait(timeout=10)
            lease.ensure_started()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(lease,)) for lease in leases]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert not errors
    assert factory.start_calls == 1
    assert pr.active_runtime_count() == 1
    assert len({id(lease.runtime) for lease in leases}) == 1


def test_many_clients_on_one_endpoint_share_one_runtime_and_one_tab_gate(
    install_playwright, monkeypatch
):
    monkeypatch.setenv("AIRBNB_PLAYWRIGHT_MAX_TABS", "3")
    factory = install_playwright(_PlaywrightFactory())

    scrapers = [PlaywrightScraper({"CDP_URL": CDP}) for _ in range(4)]
    for scraper in scrapers:
        scraper.ensure_browser_ready()

    assert factory.start_calls == 1
    assert pr.active_runtime_count() == 1
    # Tab limiting is still per browser, and shared — not multiplied by clients.
    assert {s._tab_limit for s in scrapers} == {3}
    assert len({id(s._tab_gate) for s in scrapers}) == 1

    for scraper in scrapers:
        scraper.close_browser()


def test_distinct_endpoints_respect_the_runtime_cap(install_playwright, monkeypatch):
    monkeypatch.setenv("AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES", "2")
    factory = install_playwright(_PlaywrightFactory())

    a = pr.acquire_runtime(CDP)
    b = pr.acquire_runtime(CDP_ALT)
    c = pr.acquire_runtime("http://127.0.0.1:9224")
    a.ensure_started()
    b.ensure_started()

    with pytest.raises(BrowserRuntimeUnavailable) as ctx:
        c.ensure_started()

    assert ctx.value.reason_code == "runtime_cap_reached"
    assert not isinstance(ctx.value, BrowserRuntimeResourceExhausted)
    assert factory.start_calls == 2
    assert pr.active_runtime_count() == 2
    # Distinct endpoints still get distinct runtimes (work stays distributed).
    assert a.runtime is not b.runtime


def test_equivalent_endpoint_spellings_share_one_runtime(install_playwright):
    factory = install_playwright(_PlaywrightFactory())

    spellings = [
        "http://127.0.0.1:9222",
        "127.0.0.1:9222",
        "ws://127.0.0.1:9222/devtools/browser/abc-123",
        "HTTP://127.0.0.1:9222",
    ]
    leases = [pr.acquire_runtime(url) for url in spellings]
    for lease in leases:
        lease.ensure_started()

    assert factory.start_calls == 1
    assert pr.registry_size() == 1


def test_invalid_runtime_cap_values_are_clamped(monkeypatch):
    monkeypatch.setenv("AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES", "0")
    assert pr.max_playwright_runtimes() == 1
    monkeypatch.setenv("AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES", "9999")
    assert pr.max_playwright_runtimes() == pr.RUNTIME_CAP_CEILING
    monkeypatch.setenv("AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES", "not-a-number")
    assert pr.max_playwright_runtimes() == pr.DEFAULT_MAX_RUNTIMES
    monkeypatch.delenv("AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES")
    assert pr.max_playwright_runtimes() == pr.DEFAULT_MAX_RUNTIMES


# ── 10-11. Lease accounting ──────────────────────────────────────────────────


def test_closing_one_lease_keeps_the_runtime_and_the_last_one_stops_it(
    install_playwright,
):
    factory = install_playwright(_PlaywrightFactory())
    first, second = pr.acquire_runtime(CDP), pr.acquire_runtime(CDP)
    first.ensure_started()
    runtime = first.runtime

    first.release()
    assert runtime.is_started is True, "a live lease must keep the runtime up"
    assert factory.instances[0].stop_calls == 0

    second.release()
    assert runtime.is_started is False
    assert runtime.stop_count == 1
    assert factory.instances[0].stop_calls == 1
    assert pr.registry_size() == 0


def test_repeated_and_concurrent_release_is_idempotent(install_playwright):
    factory = install_playwright(_PlaywrightFactory())
    leases = [pr.acquire_runtime(CDP) for _ in range(6)]
    leases[0].ensure_started()
    runtime = leases[0].runtime

    barrier = threading.Barrier(len(leases) * 2)

    def _release(lease):
        barrier.wait(timeout=10)
        lease.release()
        lease.release()  # double release must not underflow

    threads = [
        threading.Thread(target=_release, args=(lease,))
        for lease in leases
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert runtime.leases == 0
    assert runtime.stop_count == 1
    assert factory.instances[0].stop_calls == 1
    assert pr.registry_size() == 0


def test_tab_slots_are_released_even_when_page_creation_fails(install_playwright):
    install_playwright(_PlaywrightFactory())
    scraper = PlaywrightScraper({"CDP_URL": CDP})
    scraper.ensure_browser_ready()
    runtime = scraper._runtime

    class _ExplodingContext:
        async def new_page(self):
            raise RuntimeError("Failed to open a new tab")

    async def _attempt():
        with pytest.raises(RuntimeError):
            await scraper._open_capped_page(_ExplodingContext())

    scraper._run_async(_attempt(), op_name="tab_slot_probe")
    assert runtime.open_tab_count == 0
    scraper.close_browser()


def test_tab_gate_reset_does_not_steal_slots_from_another_client(install_playwright):
    # close_extra_tabs() at the end of one client's phase must not zero a gate
    # that another client on the same browser is still holding slots in.
    install_playwright(_PlaywrightFactory())
    runtime = pr.acquire_runtime(CDP).runtime

    async def _hold():
        await runtime.acquire_tab_slot()

    asyncio.run(_hold())
    assert runtime.open_tab_count == 1

    runtime.reset_tab_gate()
    assert runtime.open_tab_count == 1

    runtime.release_tab_slot()
    runtime.reset_tab_gate()
    assert runtime.open_tab_count == 0


def test_over_release_cannot_widen_the_tab_cap(install_playwright):
    install_playwright(_PlaywrightFactory())
    runtime = pr.acquire_runtime(CDP).runtime
    for _ in range(5):
        runtime.release_tab_slot()
    assert runtime.open_tab_count == 0

    acquired = 0
    while runtime._tab_gate.acquire(blocking=False):
        acquired += 1
    assert acquired == runtime.tab_limit


# ── 12. Failed initializer wakes every waiter ────────────────────────────────


def test_a_failed_initializer_wakes_all_waiters_with_the_classified_error(
    install_playwright,
):
    factory = install_playwright(
        _PlaywrightFactory(start_error=winerror_1455(), start_delay=0.05)
    )
    leases = [pr.acquire_runtime(CDP) for _ in range(8)]
    barrier = threading.Barrier(len(leases))
    outcomes: List[BaseException] = []
    lock = threading.Lock()

    def _worker(lease):
        barrier.wait(timeout=10)
        try:
            lease.ensure_started()
        except BaseException as exc:  # noqa: BLE001
            with lock:
                outcomes.append(exc)

    threads = [threading.Thread(target=_worker, args=(lease,)) for lease in leases]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a failed initializer must not deadlock its waiters"

    assert len(outcomes) == len(leases)
    assert all(isinstance(exc, BrowserRuntimeResourceExhausted) for exc in outcomes)
    # Every waiter after the first fails fast on the breaker instead of queueing
    # up its own spawn attempt against a host that just refused one.
    assert factory.start_calls == 1


# ── 15. Breaker: fail fast, then recover ─────────────────────────────────────


def test_breaker_makes_later_fallbacks_fail_fast_without_new_playwright_starts(
    install_playwright, monkeypatch
):
    monkeypatch.setenv("AIRAHOST_BROWSER_RUNTIME_BREAKER_SECONDS", "600")
    factory = install_playwright(_PlaywrightFactory(start_error=winerror_1455()))

    with pytest.raises(BrowserRuntimeResourceExhausted):
        pr.acquire_runtime(CDP).ensure_started()
    assert factory.start_calls == 1

    # Later dates/offsets in the same report, and other endpoints, fail fast.
    for url in (CDP, CDP, CDP_ALT):
        with pytest.raises(BrowserRuntimeResourceExhausted):
            pr.acquire_runtime(url).ensure_started()
    assert factory.start_calls == 1, "no further driver spawns while the breaker is open"


def test_the_breaker_expires_so_the_worker_can_recover(install_playwright, monkeypatch):
    monkeypatch.setenv("AIRAHOST_BROWSER_RUNTIME_BREAKER_SECONDS", "60")
    clock = {"now": 1_000.0}
    monkeypatch.setattr(pr, "_now", lambda: clock["now"])
    factory = install_playwright(_PlaywrightFactory(start_error=winerror_1455()))

    with pytest.raises(BrowserRuntimeResourceExhausted):
        pr.acquire_runtime(CDP).ensure_started()
    assert pr.breaker_is_open() is True

    clock["now"] += 61.0  # documented reset window elapsed
    assert pr.breaker_is_open() is False
    factory.start_error = None

    lease = pr.acquire_runtime(CDP)
    lease.ensure_started()
    assert lease.runtime.is_started is True


def test_the_breaker_is_not_tripped_by_ordinary_cdp_failures(install_playwright):
    install_playwright(_PlaywrightFactory(connect_error=RuntimeError("ECONNREFUSED")))

    with pytest.raises(BrowserRuntimeUnavailable):
        pr.acquire_runtime(CDP).ensure_started()

    assert pr.breaker_is_open() is False


# ── 18-19. One event per failure, and no unretrieved task exceptions ─────────


def test_one_resource_event_is_logged_and_no_unretrieved_task_exception_remains(
    install_playwright, caplog
):
    # The orphan task mirrors Playwright's Connection.run(): it ends with the
    # same OSError and nothing ever calls .exception() on it.
    install_playwright(
        _PlaywrightFactory(start_error=winerror_1455(), orphan_error=winerror_1455())
    )
    asyncio_records: List[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            asyncio_records.append(record)

    handler = _Capture()
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_logger.addHandler(handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="worker.scraper.playwright_runtime"):
            with pytest.raises(BrowserRuntimeResourceExhausted):
                pr.acquire_runtime(CDP).ensure_started()
        pr.reset_for_tests()
        for _ in range(3):
            gc.collect()
    finally:
        asyncio_logger.removeHandler(handler)

    messages = [record.getMessage() for record in asyncio_records]
    assert not [m for m in messages if "Task exception was never retrieved" in m]
    assert not [m for m in messages if "never awaited" in m]

    warned = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "start failed" in r.getMessage()
    ]
    assert len(warned) == 1, "exactly one primary resource-exhaustion event"
    assert "browser_runtime_resource_exhausted" in warned[0].getMessage()
    # ... plus at most one intentionally distinct diagnostic, at debug level.
    diagnostics = [
        r for r in caplog.records if "background task failed" in r.getMessage()
    ]
    assert len(diagnostics) <= 1
    assert all(r.levelno == logging.DEBUG for r in diagnostics)


def test_browser_fallback_surfaces_the_typed_error_not_empty_results(
    install_playwright,
):
    # The incident path end to end: direct search is empty, the browser fallback
    # runs, and the driver cannot be spawned. What must reach the caller is the
    # typed operational failure — not an empty payload, not a challenge, and not
    # a generic "search failed after N attempts".
    factory = install_playwright(_PlaywrightFactory(start_error=winerror_1455()))
    scraper = PlaywrightScraper({"CDP_URL": CDP})
    scraper.captured_search_req = None
    scraper.hardcoded_search_req = None

    with pytest.raises(BrowserRuntimeResourceExhausted) as ctx:
        scraper.search_listings_with_overrides({"checkin": "2026-09-01"})

    assert "attempts" not in str(ctx.value)
    assert scraper._search_blocked_reason is None, "this is not an Airbnb challenge"
    # One attempt only: the retry budget must not become extra driver spawns.
    assert factory.start_calls == 1


def test_sanitized_error_carries_no_cookies_tokens_urls_or_html():
    exc = BrowserRuntimeResourceExhausted(
        operation="browser_fallback",
        endpoint="127.0.0.1:9222",
        active_runtimes=1,
        runtime_cap=2,
        attempt=1,
    )
    text = f"{exc.public_message} {exc} {exc.structured_fields()}"
    lowered = text.lower()
    for forbidden in ("cookie", "token", "checkin", "<html", "sessionid", "authorization"):
        assert forbidden not in lowered
    assert exc.public_message == (
        "browser runtime unavailable: system virtual memory exhausted"
    )
    assert exc.structured_fields()["endpoint"] == "127.0.0.1:9222"


# ── 20-21. Happy path and full teardown ──────────────────────────────────────


def test_normal_startup_serves_context_and_tabs(install_playwright):
    factory = install_playwright(_PlaywrightFactory())
    scraper = PlaywrightScraper({"CDP_URL": CDP})
    scraper.ensure_browser_ready()

    async def _probe():
        context = await scraper._get_thread_context()
        await scraper._acquire_tab_slot()
        try:
            return context is not None
        finally:
            scraper._release_tab_slot()

    assert scraper._run_async(_probe(), op_name="probe") is True
    assert factory.start_calls == 1
    assert scraper._loop is not None
    scraper.close_browser()


def test_full_pool_workflow_leaves_no_runtimes_leases_tabs_or_threads(
    install_playwright,
):
    threads_before = len(pr.live_runtime_threads())
    install_playwright(_PlaywrightFactory())

    for _ in range(3):
        scrapers = [PlaywrightScraper({"CDP_URL": CDP}) for _ in range(3)]
        forks = [scrapers[0].fork() for _ in range(2)]
        for scraper in scrapers + forks:
            scraper.ensure_browser_ready()
        assert pr.active_runtime_count() == 1
        for scraper in scrapers + forks:
            scraper.close_browser()

    assert pr.registry_size() == 0
    assert pr.active_lease_count() == 0
    assert pr.active_runtime_count() == 0
    _wait_for(lambda: len(pr.live_runtime_threads()) == threads_before)


def test_shutdown_all_releases_everything(install_playwright):
    threads_before = len(pr.live_runtime_threads())
    install_playwright(_PlaywrightFactory())
    for url in (CDP, CDP_ALT):
        pr.acquire_runtime(url).ensure_started()
    assert pr.active_runtime_count() == 2

    pr.shutdown_all()

    assert pr.registry_size() == 0
    assert pr.active_runtime_count() == 0
    _wait_for(lambda: len(pr.live_runtime_threads()) == threads_before)


def _wait_for(predicate, timeout: float = 10.0) -> None:
    """Bounded wait so a leak fails the test instead of hanging the suite."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached within timeout")
