"""Fault-injectable stand-ins for the Playwright driver.

Shared by the runtime-lifecycle and browser-pool regressions so both exercise
the real ownership code with no Playwright install, no CDP browser, no actual
memory exhaustion and no paging-file changes.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, List, Optional


def winerror_1455() -> OSError:
    """The exact exception CreateProcess raised in the production incident."""
    return OSError(
        22,
        "The paging file is too small for this operation to complete",
        None,
        1455,
        None,
    )


class FakeContext:
    def __init__(self) -> None:
        self.pages: List[Any] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, with_context: bool = True) -> None:
        self.contexts = [FakeContext()] if with_context else []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def new_context(self, **_kwargs) -> FakeContext:
        context = FakeContext()
        self.contexts.append(context)
        return context


class _FakeChromium:
    def __init__(self, connect) -> None:
        self._connect = connect

    async def connect_over_cdp(self, target: str, timeout: Optional[int] = None):
        return await self._connect(target)


class FakePlaywright:
    def __init__(self, connect) -> None:
        self.chromium = _FakeChromium(connect)
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


class PlaywrightFactory:
    """Stands in for ``async_playwright``.

    ``orphan_error`` reproduces Playwright's real shape: ``__aenter__`` does
    ``loop.create_task(self._connection.run())`` and keeps no reference, so a
    driver-spawn failure ends up on a task nobody ever awaits.

    ``endpoint_errors`` maps a CDP target substring to the exception its connect
    (or driver start, via ``start_error_endpoints``) should raise, so one healthy
    and one broken endpoint can be modelled in the same process.
    """

    def __init__(
        self,
        *,
        start_error: Optional[BaseException] = None,
        orphan_error: Optional[BaseException] = None,
        connect_error: Optional[BaseException] = None,
        endpoint_errors: Optional[dict] = None,
        with_context: bool = True,
        start_delay: float = 0.0,
    ) -> None:
        self.start_error = start_error
        self.orphan_error = orphan_error
        self.connect_error = connect_error
        self.endpoint_errors = dict(endpoint_errors or {})
        self.with_context = with_context
        self.start_delay = start_delay
        self.start_calls = 0
        self.connect_targets: List[str] = []
        self.instances: List[FakePlaywright] = []
        self.browsers: List[FakeBrowser] = []
        self._lock = threading.Lock()

    def _error_for(self, target: str) -> Optional[BaseException]:
        for marker, exc in self.endpoint_errors.items():
            if marker in str(target):
                return exc
        return None

    async def _connect(self, target: str):
        with self._lock:
            self.connect_targets.append(target)
        scoped = self._error_for(target)
        if scoped is not None:
            raise scoped
        if self.connect_error is not None:
            raise self.connect_error
        browser = FakeBrowser(with_context=self.with_context)
        with self._lock:
            self.browsers.append(browser)
        return browser

    def __call__(self) -> "PlaywrightFactory._Manager":
        return self._Manager(self)

    class _Manager:
        def __init__(self, factory: "PlaywrightFactory") -> None:
            self._factory = factory

        async def start(self):
            factory = self._factory
            with factory._lock:
                factory.start_calls += 1
            if factory.start_delay:
                await asyncio.sleep(factory.start_delay)
            if factory.orphan_error is not None:
                error = factory.orphan_error

                async def _connection_run() -> None:
                    raise error

                asyncio.get_running_loop().create_task(_connection_run())
                # Let the orphan run to completion before we raise, exactly as
                # the transport does when create_subprocess_exec fails.
                await asyncio.sleep(0)
            if factory.start_error is not None:
                raise factory.start_error
            instance = FakePlaywright(factory._connect)
            with factory._lock:
                factory.instances.append(instance)
            return instance
