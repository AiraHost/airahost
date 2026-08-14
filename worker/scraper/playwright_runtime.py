"""Process-wide ownership of Playwright driver runtimes, keyed by CDP endpoint.

Why this module exists
----------------------
Every ``PlaywrightScraper`` instance used to own an asyncio loop thread *and*
its own ``async_playwright().start()`` — i.e. its own node driver subprocess.
Pools (up to 3 clients even against a single endpoint), ``fork()`` clones, and
per-phase pool rebuilds multiplied those subprocesses within one report. Worse,
``_ensure_browser()`` assigned ``self._pw`` *before* connecting over CDP, so a
failed connect left a live driver behind and the next call started yet another
one. On Windows each driver commits enough virtual memory that the host
eventually refuses ``CreateProcess`` with ``WinError 1455`` ("The paging file is
too small for this operation to complete").

The registry replaces that with one Playwright driver per normalized CDP
endpoint, shared by every logical client targeting it:

* lease-counted ownership — many clients, one runtime, one driver;
* single-flight, transactional startup — publish only on success, roll back
  (driver *and* loop thread) on any failure or cancellation;
* an explicit cap on concurrently active runtimes;
* a typed, sanitized error for OS process-spawn resource exhaustion plus a
  short-lived breaker so a memory-exhausted host is not hammered with retries;
* retrieval of Playwright's own fire-and-forget task exceptions, so a failed
  driver spawn cannot leave `Task exception was never retrieved` behind.

Tab concurrency is deliberately *not* bounded by this module's runtime cap: one
runtime still serves up to ``tab_limit`` concurrent pages, so throughput comes
from tab leases rather than from duplicate drivers.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import threading
import time
import weakref
from concurrent.futures import Future as ConcurrentFuture, TimeoutError as FutureTimeoutError
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from worker.scraper.scraper_errors import (
    BrowserRuntimeResourceExhausted,
    BrowserRuntimeUnavailable,
)

logger = logging.getLogger(__name__)

# Hard ceiling on concurrent Playwright driver subprocesses in this process.
# Conservative on purpose: each driver is a node.exe with a multi-tens-of-MB
# commit charge, and the incident host ran out of commit before it ran out of
# work. Raise AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES only on a host with headroom.
DEFAULT_MAX_RUNTIMES = 2
RUNTIME_CAP_CEILING = 8

# Startup budget for one runtime (loop thread + driver spawn + CDP connect).
RUNTIME_START_TIMEOUT_SECONDS = 60.0
# How long a synchronously-timed-out coroutine is given to unwind after being
# cancelled, so it cannot keep initializing a browser behind the caller's back.
CANCELLED_COROUTINE_DRAIN_SECONDS = 5.0
LOOP_THREAD_NAME = "playwright-async-runtime"

DEFAULT_BREAKER_SECONDS = 120.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _clamped_int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return max(low, min(default, high))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not an integer; using default %s", name, raw, default
        )
        return default
    clamped = max(low, min(value, high))
    if clamped != value:
        logger.warning(
            "%s=%s out of range [%s, %s]; clamped to %s", name, value, low, high, clamped
        )
    return clamped


def max_playwright_runtimes() -> int:
    """Maximum Playwright driver runtimes allowed to be active at once."""
    return _clamped_int_env(
        "AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES", DEFAULT_MAX_RUNTIMES, 1, RUNTIME_CAP_CEILING
    )


def _breaker_seconds() -> float:
    raw = os.getenv("AIRAHOST_BROWSER_RUNTIME_BREAKER_SECONDS")
    if raw is None or not str(raw).strip():
        return DEFAULT_BREAKER_SECONDS
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_BREAKER_SECONDS
    return max(1.0, min(value, 3600.0))


# ---------------------------------------------------------------------------
# Endpoint normalization
# ---------------------------------------------------------------------------


def normalize_cdp_endpoint(cdp_url: Any) -> str:
    """Collapse equivalent spellings of one CDP endpoint to a single key.

    ``ws://127.0.0.1:9222/devtools/browser/<id>``, ``http://127.0.0.1:9222`` and
    ``127.0.0.1:9222`` all address the same browser, so they must share one
    runtime instead of racing to spawn three drivers.
    """
    raw = str(cdp_url or "").strip()
    if not raw:
        return ""
    # A comma-separated CDP_URL is a candidate list; the first entry is the key.
    raw = raw.split(",")[0].strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlparse(candidate)
    except Exception:
        return raw.lower()
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return raw.lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if parsed.scheme in ("https", "wss") else 9222
    scheme = "https" if parsed.scheme in ("https", "wss") else "http"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{int(port)}"


def cdp_connect_targets(cdp_url: Any) -> List[str]:
    """Raw connect candidates for one configured CDP_URL value, in order."""
    raw = str(cdp_url or "").strip()
    targets = [token.strip() for token in raw.split(",") if token.strip()]
    return targets or ([raw] if raw else [])


def endpoint_label(endpoint: str) -> str:
    """host:port only — safe to log, carries no path, token, or query."""
    try:
        parsed = urlparse(endpoint)
        if parsed.hostname:
            return f"{parsed.hostname}:{parsed.port}"
    except Exception:
        pass
    return str(endpoint or "")


# ---------------------------------------------------------------------------
# Windows process-spawn resource-exhaustion classification
# ---------------------------------------------------------------------------

# ERROR_COMMITMENT_LIMIT. Deliberately the only code here: a broader set would
# start classifying ordinary CreateProcess failures (missing binary, access
# denied) as memory pressure and trip the breaker for the wrong reason.
_RESOURCE_WINERROR_CODES = frozenset({1455})
# Message form kept for wrappers that lose .winerror (e.g. re-raised as
# RuntimeError, or an OSError rebuilt without the Windows-specific arg).
_RESOURCE_MESSAGE_MARKERS = ("paging file is too small",)


def _exception_chain(exc: BaseException) -> List[BaseException]:
    chain: List[BaseException] = []
    seen: set = set()
    current: Optional[BaseException] = exc
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        nxt = current.__cause__ or current.__context__
        current = nxt if isinstance(nxt, BaseException) else None
    return chain


def _winerror_of(exc: BaseException) -> Optional[int]:
    value = getattr(exc, "winerror", None)
    if isinstance(value, int):
        return value
    # OSError(errno, strerror, filename, winerror, filename2) — some wrappers
    # rebuild the exception from args and lose the .winerror attribute.
    args = getattr(exc, "args", ()) or ()
    if isinstance(exc, OSError) and len(args) >= 4 and isinstance(args[3], int):
        return int(args[3])
    return None


def is_process_spawn_resource_exhaustion(exc: BaseException) -> bool:
    """True only for "the OS refused to spawn a process for lack of memory".

    Intentionally narrow. CDP auth failures, browser disconnects, navigation
    timeouts, anti-bot challenges, rate limits, parse errors and arbitrary
    OSErrors must not reach the fail-fast breaker.
    """
    for item in _exception_chain(exc):
        if isinstance(item, BrowserRuntimeResourceExhausted):
            return True
        if isinstance(item, OSError) and _winerror_of(item) in _RESOURCE_WINERROR_CODES:
            return True
        text = str(item or "").lower()
        if any(marker in text for marker in _RESOURCE_MESSAGE_MARKERS):
            return True
    return False


def classify_browser_runtime_error(
    exc: BaseException,
    *,
    operation: str,
    endpoint: str,
    active_runtimes: Optional[int] = None,
    runtime_cap: Optional[int] = None,
    attempt: Optional[int] = None,
    reason_code: str = "runtime_start_failed",
) -> BrowserRuntimeUnavailable:
    """Wrap a startup failure in the typed error, preserving the cause chain."""
    if isinstance(exc, BrowserRuntimeUnavailable):
        return exc
    kwargs = dict(
        operation=operation,
        endpoint=endpoint_label(endpoint),
        active_runtimes=active_runtimes,
        runtime_cap=runtime_cap,
        attempt=attempt,
    )
    if is_process_spawn_resource_exhaustion(exc):
        classified: BrowserRuntimeUnavailable = BrowserRuntimeResourceExhausted(**kwargs)
    else:
        classified = BrowserRuntimeUnavailable(reason_code, **kwargs)
    classified.__cause__ = exc
    return classified


# ---------------------------------------------------------------------------
# Resource-exhaustion breaker
# ---------------------------------------------------------------------------


class _ResourceBreaker:
    """Short-lived, process-wide fail-fast state for an out-of-memory host.

    Without it, one exhausted host turns into a retry storm: every date, offset,
    pool slot and nested browser fallback tries its own driver spawn, each one
    logging a full traceback and each one certain to fail.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open_until: float = 0.0
        self._reason: str = ""

    def trip(self, exc: BrowserRuntimeUnavailable) -> None:
        with self._lock:
            already_open = self._open_until > _now()
            self._open_until = _now() + _breaker_seconds()
            self._reason = exc.reason_code
        if not already_open:
            logger.warning(
                "browser runtime breaker open for %.0fs (reason=%s endpoint=%s); "
                "browser fallbacks fail fast until it expires",
                _breaker_seconds(),
                exc.reason_code,
                exc.endpoint or "-",
            )

    def raise_if_open(self, *, operation: str, endpoint: str) -> None:
        with self._lock:
            open_until = self._open_until
            reason = self._reason
        if open_until <= _now():
            return
        raise BrowserRuntimeResourceExhausted(
            reason or "process_spawn_resource_exhausted",
            operation=operation,
            endpoint=endpoint_label(endpoint),
            runtime_cap=max_playwright_runtimes(),
        )

    def is_open(self) -> bool:
        with self._lock:
            return self._open_until > _now()

    def reset(self) -> None:
        with self._lock:
            self._open_until = 0.0
            self._reason = ""


def _now() -> float:
    return time.monotonic()


_BREAKER = _ResourceBreaker()


def breaker_is_open() -> bool:
    return _BREAKER.is_open()


def reset_resource_breaker() -> None:
    _BREAKER.reset()


# ---------------------------------------------------------------------------
# Event-loop thread plumbing
# ---------------------------------------------------------------------------


def _install_orphan_task_reaper(loop: asyncio.AbstractEventLoop) -> None:
    """Retrieve exceptions from fire-and-forget tasks created on this loop.

    Playwright's ``PlaywrightContextManager.__aenter__`` does
    ``loop.create_task(self._connection.run())`` and keeps no reference. When the
    driver subprocess cannot be spawned, ``run()`` finishes with that OSError and
    nobody ever calls ``.exception()`` on it, so asyncio reports
    "Task exception was never retrieved" from the task's destructor — the second
    half of the incident log.

    A task factory is the supported way to see every task the loop creates. The
    done-callback *retrieves* the exception and routes it to structured logging
    at debug level; it never swallows it silently and never filters log text.
    Tasks we submit ourselves already have their exception retrieved by the
    concurrent-future bridge, so the extra call is a harmless no-op for them.
    """
    tracked: "weakref.WeakSet[asyncio.Task]" = weakref.WeakSet()
    # Coroutines this module submitted itself. Their exceptions are already
    # delivered to the calling thread through the concurrent future, so
    # reporting them here too would be the duplicate diagnostic the incident log
    # showed — one failure, two tracebacks.
    owned: set = set()
    owned_lock = threading.Lock()

    def _on_done(task: "asyncio.Task") -> None:
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception:
            return
        if exc is None:
            return
        logger.debug(
            "[playwright_runtime] background task failed reason=%s type=%s",
            "resource_exhausted"
            if is_process_spawn_resource_exhaustion(exc)
            else "other",
            type(exc).__name__,
        )

    def _factory(loop_: asyncio.AbstractEventLoop, coro, **kwargs):
        task = asyncio.Task(coro, loop=loop_, **kwargs)
        with owned_lock:
            is_ours = id(coro) in owned
            owned.discard(id(coro))
        tracked.add(task)
        if not is_ours:
            task.add_done_callback(_on_done)
        return task

    loop.set_task_factory(_factory)
    setattr(loop, "_airahost_tracked_tasks", tracked)
    setattr(loop, "_airahost_owned_coros", (owned, owned_lock))


def _start_loop_thread(label: str) -> Tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Start a dedicated loop thread. Raises if it does not come up."""
    ready = threading.Event()
    holder: Dict[str, Any] = {}

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _install_orphan_task_reaper(loop)
        holder["loop"] = loop
        ready.set()
        try:
            loop.run_forever()
        finally:
            _drain_and_close_loop(loop)

    thread = threading.Thread(target=_run, name=LOOP_THREAD_NAME, daemon=True)
    thread.start()
    if not ready.wait(timeout=10.0) or holder.get("loop") is None:
        raise BrowserRuntimeUnavailable(
            "loop_thread_start_failed", operation="start_runtime", endpoint=label
        )
    return holder["loop"], thread


def _drain_and_close_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel and await every pending task before closing, then close."""
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    except Exception:
        pass
    try:
        loop.close()
    except Exception:
        pass


def _stop_loop_thread(
    loop: Optional[asyncio.AbstractEventLoop],
    thread: Optional[threading.Thread],
    *,
    join_timeout: float = 10.0,
) -> None:
    if loop is not None:
        try:
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
    if (
        thread is not None
        and thread.is_alive()
        and thread.ident != threading.get_ident()
    ):
        try:
            thread.join(timeout=join_timeout)
        except Exception:
            pass


def _submit(
    loop: asyncio.AbstractEventLoop,
    coro,
    *,
    op_name: str,
    timeout_seconds: Optional[float],
):
    """Run a coroutine on ``loop`` from another thread, bounded and drainable.

    On synchronous timeout the coroutine is cancelled and given a bounded window
    to unwind, so it cannot keep connecting a browser after its caller gave up.
    """
    registry = getattr(loop, "_airahost_owned_coros", None)
    if registry is not None:
        owned, owned_lock = registry
        with owned_lock:
            owned.add(id(coro))
    future: ConcurrentFuture = asyncio.run_coroutine_threadsafe(coro, loop)
    if not (isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0):
        return future.result()
    try:
        return future.result(timeout=float(timeout_seconds))
    except FutureTimeoutError:
        future.cancel()
        try:
            future.exception(timeout=CANCELLED_COROUTINE_DRAIN_SECONDS)
        except Exception:
            pass
        logger.warning(
            "[playwright_runtime] %s timed out after %ss; coroutine cancelled and drained",
            op_name,
            timeout_seconds,
        )
        raise


def _close_unawaited(coro: Any) -> None:
    """Close a coroutine we will never submit, so Python does not warn."""
    close = getattr(coro, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------


class PlaywrightRuntime:
    """One event-loop thread + one Playwright driver + one CDP browser.

    Ownership contract:

    * the runtime owns the loop thread, the Playwright object, the CDP browser
      *connection*, any context it created itself, and the tab gate;
    * it never owns the user's browser process — an externally managed CDP
      browser is only disconnected from, never closed;
    * a context that already existed in the browser is used but not owned, so
      it is never closed on teardown;
    * ``ensure_started`` is single-flight; ``stop`` and ``reset`` are idempotent
      and safe under concurrent calls.
    """

    def __init__(self, endpoint: str, targets: List[str], tab_limit: int) -> None:
        self.endpoint = endpoint
        self.targets = list(targets) or [endpoint]
        self.tab_limit = int(tab_limit)

        # Guards start/stop/reset. Never acquired from inside a coroutine, so it
        # is never held across an await.
        self._start_lock = threading.RLock()
        self._started = False
        self._start_count = 0
        self._stop_count = 0

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._owner_tid: Optional[int] = None
        self._pw = None
        self._browser = None
        self._context = None
        self._context_owned = False

        self._tab_lock = threading.Lock()
        self._tab_gate = threading.BoundedSemaphore(self.tab_limit)
        self._open_tab_count = 0

        self.leases = 0

    # -- introspection -----------------------------------------------------

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    @property
    def open_tab_count(self) -> int:
        with self._tab_lock:
            return self._open_tab_count

    @property
    def start_count(self) -> int:
        return self._start_count

    @property
    def stop_count(self) -> int:
        return self._stop_count

    # -- tab gate ----------------------------------------------------------

    async def acquire_tab_slot(self, timeout_seconds: float = 120.0) -> None:
        deadline = _now() + timeout_seconds
        while True:
            if self._tab_gate.acquire(blocking=False):
                with self._tab_lock:
                    self._open_tab_count += 1
                return
            if _now() >= deadline:
                raise RuntimeError(
                    f"Timed out waiting for Playwright tab slot (limit={self.tab_limit})"
                )
            await asyncio.sleep(0.05)

    def release_tab_slot(self) -> None:
        with self._tab_lock:
            if self._open_tab_count <= 0:
                # Nothing outstanding: releasing anyway would push the bounded
                # semaphore above its ceiling and quietly widen the tab cap.
                return
            self._open_tab_count -= 1
        try:
            self._tab_gate.release()
        except ValueError:
            pass

    def reset_tab_gate(self, *, force: bool = False) -> None:
        """Re-arm the tab gate. Refuses while tabs are outstanding.

        The gate is shared by every client on this endpoint now, so one client
        finishing its phase must not zero the counter under another client that
        still holds slots — that would let the browser exceed its tab cap.
        """
        with self._tab_lock:
            if self._open_tab_count > 0 and not force:
                logger.debug(
                    "[playwright_runtime] tab gate reset skipped endpoint=%s open=%s",
                    endpoint_label(self.endpoint),
                    self._open_tab_count,
                )
                return
            self._open_tab_count = 0
            self._tab_gate = threading.BoundedSemaphore(self.tab_limit)

    # -- lifecycle ---------------------------------------------------------

    def ensure_started(self, *, operation: str = "ensure_runtime") -> None:
        """Start the runtime exactly once; concurrent callers wait and share.

        Every waiter that arrives during a failing start gets the same
        classified error rather than queueing up its own spawn attempt.
        """
        if self._started:
            return
        _BREAKER.raise_if_open(operation=operation, endpoint=self.endpoint)
        with self._start_lock:
            if self._started:
                return
            _BREAKER.raise_if_open(operation=operation, endpoint=self.endpoint)
            _reserve_runtime_capacity(self.endpoint, operation=operation)
            try:
                self._start_locked(operation=operation)
            except BaseException as exc:
                classified = classify_browser_runtime_error(
                    exc,
                    operation=operation,
                    endpoint=self.endpoint,
                    active_runtimes=active_runtime_count(),
                    runtime_cap=max_playwright_runtimes(),
                )
                if isinstance(classified, BrowserRuntimeResourceExhausted):
                    _BREAKER.trip(classified)
                logger.warning(
                    "[playwright_runtime] start failed %s",
                    _fields_text(classified.structured_fields()),
                )
                raise classified from exc
            finally:
                _release_runtime_capacity(self.endpoint)

    def _start_locked(self, *, operation: str) -> None:
        """Transactional start: nothing is published until everything succeeds."""
        loop, thread = _start_loop_thread(self.endpoint)
        pw = None
        browser = None
        context = None
        context_owned = False
        try:
            pw, browser, context, context_owned = _submit(
                loop,
                self._connect_async(),
                op_name=f"{operation}:connect",
                timeout_seconds=RUNTIME_START_TIMEOUT_SECONDS,
            )
        except BaseException:
            # _connect_async already stopped any Playwright it started; the loop
            # thread is ours to unwind here so no orphan thread survives.
            _stop_loop_thread(loop, thread)
            raise

        self._loop = loop
        self._loop_thread = thread
        self._owner_tid = thread.ident
        self._pw = pw
        self._browser = browser
        self._context = context
        self._context_owned = context_owned
        self._started = True
        self._start_count += 1
        logger.info(
            "[playwright_runtime] started endpoint=%s runtimes=%s/%s tab_limit=%s",
            endpoint_label(self.endpoint),
            active_runtime_count(),
            max_playwright_runtimes(),
            self.tab_limit,
        )

    async def _connect_async(self):
        """Start the driver, connect over CDP, and pick a context — or roll back."""
        pw = await self._start_playwright_retrieving_orphans()
        try:
            browser = None
            last_exc: Optional[BaseException] = None
            for target in self.targets:
                try:
                    browser = await pw.chromium.connect_over_cdp(target, timeout=15000)
                    break
                except Exception as exc:
                    logger.warning(
                        "[playwright_runtime] CDP connect failed endpoint=%s: %s",
                        endpoint_label(self.endpoint),
                        exc,
                    )
                    last_exc = exc
            if browser is None:
                raise BrowserRuntimeUnavailable(
                    "cdp_connect_failed",
                    operation="connect_over_cdp",
                    endpoint=endpoint_label(self.endpoint),
                ) from last_exc
            context, owned = await self._select_context(browser)
            return pw, browser, context, owned
        except BaseException:
            # Exactly one stop() for the driver we started in this attempt.
            try:
                await pw.stop()
            except Exception:
                pass
            raise

    async def _start_playwright_retrieving_orphans(self):
        """``async_playwright().start()`` with its orphan tasks accounted for."""
        try:
            return await async_playwright().start()
        except BaseException:
            await self._reap_tracked_tasks()
            raise

    async def _reap_tracked_tasks(self) -> None:
        loop = asyncio.get_running_loop()
        tracked = getattr(loop, "_airahost_tracked_tasks", None)
        if not tracked:
            return
        current = asyncio.current_task()
        orphans = [t for t in list(tracked) if t is not current]
        for task in orphans:
            if not task.done():
                task.cancel()
        if orphans:
            # gather() retrieves each exception, which is what clears asyncio's
            # "never retrieved" flag; the done-callback logs them.
            await asyncio.gather(*orphans, return_exceptions=True)

    async def _select_context(self, browser):
        if browser.contexts:
            # Prefer the existing persistent context so new pages open as tabs
            # in the user's original browser window. Not ours, so never closed.
            return browser.contexts[0], False
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        )
        context = await browser.new_context(
            user_agent=user_agent, viewport={"width": 1280, "height": 800}
        )
        return context, True

    def reset(self) -> None:
        """Tear the runtime down so the next use starts from a clean state.

        Used after a browser disconnect. A half-started runtime must never be
        re-entered — the old code called ``async_playwright().start()`` again on
        an instance that already held a live driver, leaking one per attempt.
        """
        self.stop()

    def stop(self) -> None:
        with self._start_lock:
            if not self._started:
                # Still clear any half-set state from a rolled-back start.
                self._clear_state()
                return
            loop, thread = self._loop, self._loop_thread
            context, context_owned, pw = self._context, self._context_owned, self._pw
            self._clear_state()
            self._started = False
            self._stop_count += 1

        if loop is not None:
            try:
                _submit(
                    loop,
                    self._teardown_async(context, context_owned, pw),
                    op_name="runtime_teardown",
                    timeout_seconds=20.0,
                )
            except Exception as exc:
                logger.warning(
                    "[playwright_runtime] teardown endpoint=%s incomplete: %s",
                    endpoint_label(self.endpoint),
                    exc,
                )
        _stop_loop_thread(loop, thread)
        logger.info(
            "[playwright_runtime] stopped endpoint=%s runtimes=%s/%s",
            endpoint_label(self.endpoint),
            active_runtime_count(),
            max_playwright_runtimes(),
        )

    @staticmethod
    async def _teardown_async(context, context_owned: bool, pw) -> None:
        if context is not None and context_owned:
            try:
                await context.close()
            except Exception as exc:
                logger.debug("[playwright_runtime] owned context close failed: %s", exc)
        # The CDP browser belongs to the user: disconnecting the driver is the
        # whole teardown. browser.close() would close their Chrome.
        if pw is not None:
            try:
                await pw.stop()
            except Exception as exc:
                logger.debug("[playwright_runtime] playwright stop failed: %s", exc)

    def _clear_state(self) -> None:
        self._loop = None
        self._loop_thread = None
        self._owner_tid = None
        self._pw = None
        self._browser = None
        self._context = None
        self._context_owned = False
        self.reset_tab_gate(force=True)

    # -- work submission ---------------------------------------------------

    def run(
        self,
        coro_or_factory: Any,
        *,
        op_name: str,
        timeout_seconds: Optional[float] = None,
    ):
        """Run a coroutine on this runtime's loop from a caller thread."""
        try:
            self.ensure_started(operation=op_name)
        except BaseException:
            if not callable(coro_or_factory):
                _close_unawaited(coro_or_factory)
            raise
        loop = self._loop
        if loop is None:
            if not callable(coro_or_factory):
                _close_unawaited(coro_or_factory)
            raise BrowserRuntimeUnavailable(
                "runtime_not_running",
                operation=op_name,
                endpoint=endpoint_label(self.endpoint),
            )
        if threading.get_ident() == self._owner_tid:
            if not callable(coro_or_factory):
                _close_unawaited(coro_or_factory)
            raise RuntimeError(f"Playwright {op_name} called from runtime loop thread")
        coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
        return _submit(loop, coro, op_name=op_name, timeout_seconds=timeout_seconds)

    async def get_context(self):
        """The live context for this runtime (coroutine — runs on our loop)."""
        if self._context is None:
            raise BrowserRuntimeUnavailable(
                "context_unavailable",
                operation="get_context",
                endpoint=endpoint_label(self.endpoint),
            )
        return self._context

    async def get_browser(self):
        if self._browser is None:
            raise BrowserRuntimeUnavailable(
                "browser_unavailable",
                operation="get_browser",
                endpoint=endpoint_label(self.endpoint),
            )
        return self._browser


def _fields_text(fields: Dict[str, Any]) -> str:
    return " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY_LOCK = threading.RLock()
_RUNTIMES: Dict[str, PlaywrightRuntime] = {}
# Endpoints whose start is in flight. Counted against the cap so a failed or
# cancelled initializer can never leave a slot permanently consumed either.
_STARTING: set = set()


def _default_tab_limit() -> int:
    """Tabs per browser.

    Defaults to the configured worker cap so tab concurrency matches
    DAY_QUERY/MAX_SCRAPER_WORKERS instead of bottlenecking below it, with a hard
    ceiling of 8 concurrent tabs per browser to limit Airbnb anti-bot challenges
    under load. AIRBNB_PLAYWRIGHT_MAX_TABS still overrides.
    """
    from worker.core.concurrent_runner import MAX_SCRAPER_WORKERS

    return _clamped_int_env(
        "AIRBNB_PLAYWRIGHT_MAX_TABS", int(MAX_SCRAPER_WORKERS), 1, 8
    )


def active_runtime_count() -> int:
    """Runtimes with a live Playwright driver right now."""
    with _REGISTRY_LOCK:
        return sum(1 for rt in _RUNTIMES.values() if rt.is_started)


def registry_size() -> int:
    with _REGISTRY_LOCK:
        return len(_RUNTIMES)


def active_lease_count() -> int:
    with _REGISTRY_LOCK:
        return sum(rt.leases for rt in _RUNTIMES.values())


def _reserve_runtime_capacity(endpoint: str, *, operation: str) -> None:
    """Claim one slot of the configured driver cap, or refuse.

    The in-flight set matters: without it two endpoints starting concurrently
    both see ``started < cap`` and both spawn a driver.
    """
    cap = max_playwright_runtimes()
    with _REGISTRY_LOCK:
        claimed = {ep for ep, rt in _RUNTIMES.items() if rt.is_started} | _STARTING
        if endpoint in claimed:
            return
        if len(claimed) < cap:
            _STARTING.add(endpoint)
            return
        in_use = len(claimed)
    raise BrowserRuntimeUnavailable(
        "runtime_cap_reached",
        operation=operation,
        endpoint=endpoint_label(endpoint),
        active_runtimes=in_use,
        runtime_cap=cap,
    )


def _release_runtime_capacity(endpoint: str) -> None:
    with _REGISTRY_LOCK:
        _STARTING.discard(endpoint)


class RuntimeLease:
    """One logical client's claim on a shared runtime.

    Release is idempotent and reference-counted: closing one client must not
    tear down a runtime another client is still using, and the last release
    stops it exactly once.
    """

    def __init__(self, runtime: PlaywrightRuntime) -> None:
        self._runtime = runtime
        self._released = False
        self._release_lock = threading.Lock()

    @property
    def runtime(self) -> PlaywrightRuntime:
        return self._runtime

    @property
    def endpoint(self) -> str:
        return self._runtime.endpoint

    @property
    def released(self) -> bool:
        return self._released

    def ensure_started(self, *, operation: str = "ensure_runtime") -> PlaywrightRuntime:
        self._raise_if_released()
        self._runtime.ensure_started(operation=operation)
        return self._runtime

    def run(self, coro_or_factory, *, op_name: str, timeout_seconds=None):
        self._raise_if_released(coro_or_factory)
        return self._runtime.run(
            coro_or_factory, op_name=op_name, timeout_seconds=timeout_seconds
        )

    def _raise_if_released(self, coro_or_factory: Any = None) -> None:
        if not self._released:
            return
        if coro_or_factory is not None and not callable(coro_or_factory):
            _close_unawaited(coro_or_factory)
        raise BrowserRuntimeUnavailable(
            "lease_released",
            operation="use_after_release",
            endpoint=endpoint_label(self._runtime.endpoint),
        )

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        stop_runtime: Optional[PlaywrightRuntime] = None
        with _REGISTRY_LOCK:
            self._runtime.leases = max(0, self._runtime.leases - 1)
            if self._runtime.leases == 0:
                # Drop the entry here so the registry cannot grow without bound
                # as endpoint configuration changes across reports.
                if _RUNTIMES.get(self._runtime.endpoint) is self._runtime:
                    _RUNTIMES.pop(self._runtime.endpoint, None)
                stop_runtime = self._runtime
        # stop() takes the runtime's start lock; never hold the registry lock
        # across it, or release() and ensure_started() can deadlock.
        if stop_runtime is not None:
            stop_runtime.stop()


def acquire_runtime(cdp_url: Any, *, tab_limit: Optional[int] = None) -> RuntimeLease:
    """Take a lease on the runtime for ``cdp_url``. Cheap: starts nothing."""
    endpoint = normalize_cdp_endpoint(cdp_url)
    if not endpoint:
        raise BrowserRuntimeUnavailable(
            "cdp_url_missing", operation="acquire_runtime", endpoint=""
        )
    targets = cdp_connect_targets(cdp_url)
    limit = int(tab_limit) if tab_limit else _default_tab_limit()
    with _REGISTRY_LOCK:
        runtime = _RUNTIMES.get(endpoint)
        if runtime is None:
            runtime = PlaywrightRuntime(endpoint, targets, limit)
            _RUNTIMES[endpoint] = runtime
        runtime.leases += 1
        return RuntimeLease(runtime)


def shutdown_all() -> None:
    """Release everything: used at worker shutdown and between tests."""
    with _REGISTRY_LOCK:
        runtimes = list(_RUNTIMES.values())
        _RUNTIMES.clear()
        _STARTING.clear()
    for runtime in runtimes:
        runtime.leases = 0
        try:
            runtime.stop()
        except Exception as exc:
            logger.debug("[playwright_runtime] shutdown stop failed: %s", exc)


def reset_for_tests() -> None:
    shutdown_all()
    reset_resource_breaker()


def live_runtime_threads() -> List[threading.Thread]:
    """Every still-running loop thread this module owns — used by regressions."""
    return [
        t
        for t in threading.enumerate()
        if t.name == LOOP_THREAD_NAME and t.is_alive()
    ]


atexit.register(shutdown_all)
