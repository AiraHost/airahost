"""Browser-pool sizing against real endpoints and real resource limits.

Before the shared runtime, ``build_warmed_browser_client_pool`` created up to
three clients even when only one CDP endpoint existed, and every one of them
started its own Playwright driver subprocess. Several pools per report (main,
comp enrichment, price estimator, per-phase rebuilds) multiplied that again.
The pool now expresses concurrency as logical clients over *shared* endpoint
runtimes, so client count no longer drives driver count.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from worker.scraper import browser_runtime
from worker.scraper import playwright_runtime as pr
from worker.scraper.playwright_scraper import PlaywrightScraper
from worker.scraper.scraper_errors import BrowserRuntimeResourceExhausted
from worker.tests.playwright_doubles import PlaywrightFactory, winerror_1455


class _FakeAirbnbClient:
    def __init__(self, config: Dict[str, Any]):
        self.config = dict(config)
        self.cdp_url = str(self.config.get("CDP_URL") or "")
        self.ready_calls = 0
        self.close_calls = 0

    def ensure_browser_ready(self) -> None:
        self.ready_calls += 1

    def close_browser(self) -> None:
        self.close_calls += 1


def test_build_pool_keeps_requested_size_with_single_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(browser_runtime, "AirbnbClient", _FakeAirbnbClient)

    pool = browser_runtime.build_warmed_browser_client_pool(
        base_config={"CDP_URLS": "http://127.0.0.1:9222"},
        requested_size=3,
        pool_name="test_pool",
    )
    try:
        # Callers size their thread pools from len(pool), so the client count is
        # preserved — what must not multiply is the runtime behind them.
        assert len(pool) == 3
        assert all(client.cdp_url == "http://127.0.0.1:9222" for client in pool)
        assert all(client.ready_calls == 1 for client in pool)
    finally:
        browser_runtime.close_browser_client_pool(pool)

    assert all(client.close_calls == 1 for client in pool)


def test_pool_of_many_clients_on_one_endpoint_starts_one_driver(
    install_playwright,
) -> None:
    factory = install_playwright(PlaywrightFactory())

    pool = browser_runtime.build_warmed_browser_client_pool(
        base_config={"CDP_URLS": "http://127.0.0.1:9222"},
        requested_size=3,
        pool_name="single_endpoint_pool",
    )
    try:
        assert len(pool) == 3
        assert factory.start_calls == 1, "one endpoint means one Playwright driver"
        assert pr.active_runtime_count() == 1
    finally:
        browser_runtime.close_browser_client_pool(pool)

    assert pr.active_runtime_count() == 0
    assert pr.registry_size() == 0


def test_distinct_endpoints_distribute_work_within_the_runtime_cap(
    install_playwright, monkeypatch
) -> None:
    monkeypatch.setenv("AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES", "2")
    factory = install_playwright(PlaywrightFactory())

    pool = browser_runtime.build_warmed_browser_client_pool(
        base_config={"CDP_URLS": "http://127.0.0.1:9222,http://127.0.0.1:9223,http://127.0.0.1:9224"},
        requested_size=3,
        pool_name="multi_endpoint_pool",
    )
    try:
        # Round-robin still spreads clients over the endpoints that fit the cap.
        assert sorted({client.cdp_url for client in pool}) == [
            "http://127.0.0.1:9222",
            "http://127.0.0.1:9223",
        ]
        assert factory.start_calls == 2
        assert pr.active_runtime_count() == 2
    finally:
        browser_runtime.close_browser_client_pool(pool)


def test_all_slots_resource_exhausted_raises_and_returns_no_cold_client(
    install_playwright,
) -> None:
    factory = install_playwright(PlaywrightFactory(start_error=winerror_1455()))

    with pytest.raises(BrowserRuntimeResourceExhausted) as ctx:
        browser_runtime.build_warmed_browser_client_pool(
            base_config={"CDP_URLS": "http://127.0.0.1:9222"},
            requested_size=3,
            pool_name="exhausted_pool",
        )

    # A cold fallback client would only defer the identical spawn into the
    # browser-fallback path, once per date and offset.
    assert ctx.value.public_message == (
        "browser runtime unavailable: system virtual memory exhausted"
    )
    assert factory.start_calls == 1, "no retry multiplication across pool slots"
    assert pr.active_runtime_count() == 0
    assert pr.active_lease_count() == 0


def test_one_broken_endpoint_still_yields_healthy_capacity(
    install_playwright, monkeypatch
) -> None:
    monkeypatch.setenv("AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES", "2")
    factory = install_playwright(
        PlaywrightFactory(endpoint_errors={":9223": RuntimeError("ECONNREFUSED")})
    )

    pool = browser_runtime.build_warmed_browser_client_pool(
        base_config={"CDP_URLS": "http://127.0.0.1:9222,http://127.0.0.1:9223"},
        requested_size=3,
        pool_name="half_broken_pool",
    )
    try:
        assert {client.cdp_url for client in pool} == {"http://127.0.0.1:9222"}
        assert len(pool) == 2  # slots 0 and 2; the broken endpoint's slot is dropped
        assert pr.active_runtime_count() == 1
        # The broken endpoint is warmed once, not once per slot bound to it.
        assert factory.connect_targets.count("http://127.0.0.1:9223") == 1
    finally:
        browser_runtime.close_browser_client_pool(pool)


def test_non_resource_total_failure_still_returns_one_cold_client(
    install_playwright,
) -> None:
    # Browser not up yet is a transient, recoverable condition — the existing
    # bounded retry path must still get a client to work with.
    install_playwright(PlaywrightFactory(connect_error=RuntimeError("ECONNREFUSED")))

    pool = browser_runtime.build_warmed_browser_client_pool(
        base_config={"CDP_URLS": "http://127.0.0.1:9222"},
        requested_size=3,
        pool_name="cold_pool",
    )
    try:
        assert len(pool) == 1
    finally:
        browser_runtime.close_browser_client_pool(pool)


def test_resolve_cdp_urls_discovers_ipv6_ports(monkeypatch) -> None:
    monkeypatch.setenv("CDP_DISCOVERY_PORTS", "9224,9225,9226")

    def _fake_is_cdp_endpoint(url: str, timeout_seconds: float = 0.25) -> bool:
        return url in {
            "http://[::1]:9224",
            "http://[::1]:9225",
            "http://[::1]:9226",
        }

    monkeypatch.setattr(browser_runtime, "_is_cdp_endpoint", _fake_is_cdp_endpoint)

    resolved = browser_runtime.resolve_cdp_urls(
        {"CDP_URL": "ws://[::1]:9224/devtools/browser/test"}
    )

    assert resolved == [
        "http://[::1]:9224",
        "http://[::1]:9225",
        "http://[::1]:9226",
    ]


def test_equivalent_spellings_collapse_to_one_endpoint() -> None:
    # Keyed on the raw string, these looked like three endpoints and each one
    # started its own driver.
    resolved = browser_runtime.resolve_cdp_urls(
        {
            "CDP_URLS": (
                "http://127.0.0.1:9222,"
                "ws://127.0.0.1:9222/devtools/browser/abc,"
                "127.0.0.1:9222"
            )
        }
    )
    assert resolved == ["http://127.0.0.1:9222"]


def test_playwright_tab_gate_is_shared_per_endpoint_and_capped(monkeypatch) -> None:
    # AIRBNB_PLAYWRIGHT_MAX_TABS is honored up to the 8-tab safety ceiling.
    monkeypatch.setenv("AIRBNB_PLAYWRIGHT_MAX_TABS", "7")

    first = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})
    second = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9223"})
    same_endpoint = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})

    assert first._tab_limit == 7
    assert second._tab_limit == 7
    # Distinct browsers keep distinct tab budgets...
    assert first._tab_gate is not second._tab_gate
    # ...but two clients on one browser must not double its tab budget.
    assert first._tab_gate is same_endpoint._tab_gate


def test_playwright_tab_gate_capped_at_ceiling(monkeypatch) -> None:
    # Requests above the ceiling are clamped to 8.
    monkeypatch.setenv("AIRBNB_PLAYWRIGHT_MAX_TABS", "20")
    scraper = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})
    assert scraper._tab_limit == 8


def test_playwright_tab_gate_defaults_to_worker_cap(monkeypatch) -> None:
    # With no explicit tab override, tabs default to the worker cap (clamped to 8)
    # so tab concurrency matches the configured worker count.
    monkeypatch.delenv("AIRBNB_PLAYWRIGHT_MAX_TABS", raising=False)
    scraper = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})
    assert scraper._tab_limit == 8
