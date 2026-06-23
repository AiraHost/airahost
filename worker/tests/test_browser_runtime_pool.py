from __future__ import annotations

from typing import Any, Dict

from worker.scraper import browser_runtime
from worker.scraper.playwright_scraper import PlaywrightScraper


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
        assert len(pool) == 3
        assert all(client.cdp_url == "http://127.0.0.1:9222" for client in pool)
        assert all(client.ready_calls == 1 for client in pool)
    finally:
        browser_runtime.close_browser_client_pool(pool)

    assert all(client.close_calls == 1 for client in pool)


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


def test_playwright_tab_gate_is_per_instance_and_capped(monkeypatch) -> None:
    monkeypatch.setenv("AIRBNB_PLAYWRIGHT_MAX_TABS", "7")

    first = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9222"})
    second = PlaywrightScraper({"CDP_URL": "http://127.0.0.1:9223"})

    assert first._tab_limit == 5
    assert second._tab_limit == 5
    assert first._tab_gate is not second._tab_gate
