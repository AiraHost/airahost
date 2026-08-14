from __future__ import annotations

import pytest

from worker.scraper import playwright_runtime


@pytest.fixture(autouse=True)
def _isolate_playwright_runtime_registry():
    """Give every test a clean runtime registry and a closed breaker.

    The registry is process-wide by design (that is the whole point: one
    Playwright driver per CDP endpoint per worker process), so without this a
    runtime created by one test — including its tab limit, breaker state and
    loop thread — would leak into the next one.
    """
    playwright_runtime.reset_for_tests()
    try:
        yield
    finally:
        playwright_runtime.reset_for_tests()


@pytest.fixture
def install_playwright(monkeypatch):
    """Swap the real Playwright driver for a fault-injectable double."""

    def _install(factory):
        monkeypatch.setattr(playwright_runtime, "async_playwright", factory)
        return factory

    return _install
