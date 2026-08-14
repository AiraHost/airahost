"""Regression coverage for the bounded, two-stage CDP endpoint preflight.

Uses local fakes only — no live Airbnb, no real Chrome, no real HTTP/TCP
sockets for the version-check stage. The attach stage reuses the shared
Playwright-runtime fault-injectable double (``playwright_doubles``) so it
exercises the real ownership/teardown code without a real CDP browser.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest

from worker.scraper import cdp_preflight
from worker.scraper import playwright_runtime as pr
from worker.tests.playwright_doubles import PlaywrightFactory, winerror_1455


class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self, *_args, **_kwargs) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _install_fake_http(
    monkeypatch: pytest.MonkeyPatch, responses: Dict[str, Any]
) -> list[str]:
    """Map a CDP endpoint URL to a canned /json/version outcome.

    ``responses`` values are either a dict (serialized as the JSON body of a
    200 response), bytes/str (raw 200 body — for the "not valid JSON" case),
    or an Exception instance (the connection/HTTP failure case).
    """
    requested: list[str] = []

    def _fake_urlopen(request, timeout=None):  # noqa: ANN001 - matches urlopen signature
        url = request.full_url
        requested.append(url)
        base = url.rsplit("/json/version", 1)[0]
        outcome = responses.get(base)
        if outcome is None:
            raise OSError("connection refused")
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, (bytes, str)):
            body = outcome.encode("utf-8") if isinstance(outcome, str) else outcome
        else:
            body = json.dumps(outcome).encode("utf-8")
        return _FakeHTTPResponse(200, body)

    monkeypatch.setattr(cdp_preflight, "urlopen", _fake_urlopen)
    return requested


_VALID_VERSION_BODY = {
    "Browser": "Chrome/121.0.0.0",
    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/opaque-token",
}


def test_three_ports_with_valid_version_and_successful_attach_are_all_ready(
    monkeypatch: pytest.MonkeyPatch, install_playwright
) -> None:
    urls = [
        "http://127.0.0.1:9222",
        "http://127.0.0.1:9223",
        "http://127.0.0.1:9224",
    ]
    _install_fake_http(monkeypatch, {u: _VALID_VERSION_BODY for u in urls})
    install_playwright(PlaywrightFactory())

    results = cdp_preflight.check_cdp_endpoints(urls)

    assert [r.status for r in results] == [cdp_preflight.STATUS_READY] * 3
    assert cdp_preflight.summarize_preflight(results) == (
        "127.0.0.1:9222=ready 127.0.0.1:9223=ready 127.0.0.1:9224=ready"
    )
    # Sequential probing releases each lease before the next endpoint starts,
    # so no runtime is left running afterward.
    assert pr.active_runtime_count() == 0


def test_http_200_with_non_cdp_body_is_rejected_without_attaching(
    monkeypatch: pytest.MonkeyPatch, install_playwright
) -> None:
    url = "http://127.0.0.1:9222"
    _install_fake_http(monkeypatch, {url: "<html>not a CDP endpoint</html>"})
    factory = install_playwright(PlaywrightFactory())

    result = cdp_preflight.check_cdp_endpoint(url)

    assert result.status == cdp_preflight.STATUS_INVALID_VERSION_RESPONSE
    # Stage 2 (attach) must never run when stage 1 already failed.
    assert factory.start_calls == 0


def test_http_200_with_json_missing_usable_identity_is_rejected(
    monkeypatch: pytest.MonkeyPatch, install_playwright
) -> None:
    url = "http://127.0.0.1:9222"
    _install_fake_http(monkeypatch, {url: {"unrelated": "field"}})
    factory = install_playwright(PlaywrightFactory())

    result = cdp_preflight.check_cdp_endpoint(url)

    assert result.status == cdp_preflight.STATUS_INVALID_VERSION_RESPONSE
    assert factory.start_calls == 0


def test_no_response_is_tcp_or_http_unavailable(
    monkeypatch: pytest.MonkeyPatch, install_playwright
) -> None:
    url = "http://127.0.0.1:9222"
    _install_fake_http(monkeypatch, {})  # nothing registered -> simulated refusal
    factory = install_playwright(PlaywrightFactory())

    result = cdp_preflight.check_cdp_endpoint(url)

    assert result.status == cdp_preflight.STATUS_TCP_OR_HTTP_UNAVAILABLE
    assert factory.start_calls == 0


def test_http_ok_then_connect_over_cdp_failure_is_cdp_attach_failed(
    monkeypatch: pytest.MonkeyPatch, install_playwright
) -> None:
    url = "http://127.0.0.1:9222"
    _install_fake_http(monkeypatch, {url: _VALID_VERSION_BODY})
    install_playwright(PlaywrightFactory(connect_error=RuntimeError("ECONNREFUSED")))

    result = cdp_preflight.check_cdp_endpoint(url)

    assert result.status == cdp_preflight.STATUS_CDP_ATTACH_FAILED
    # No leftover runtime/lease after a failed attach.
    assert pr.active_runtime_count() == 0
    assert pr.active_lease_count() == 0


def test_attach_exceptions_are_sanitized_reason_codes(
    monkeypatch: pytest.MonkeyPatch, install_playwright
) -> None:
    """The result carries a short reason code, never a raw exception message
    (which could contain a full CDP URL, a stack path, or other detail)."""
    url = "http://127.0.0.1:9222"
    _install_fake_http(monkeypatch, {url: _VALID_VERSION_BODY})
    install_playwright(PlaywrightFactory(start_error=winerror_1455()))

    result = cdp_preflight.check_cdp_endpoint(url)

    assert result.status == cdp_preflight.STATUS_CDP_ATTACH_FAILED
    assert result.reason == "process_spawn_resource_exhausted"
    assert "paging file" not in result.reason.lower()
    assert "ws://" not in result.reason


def test_successful_preflight_never_closes_the_operator_browser(
    monkeypatch: pytest.MonkeyPatch, install_playwright
) -> None:
    url = "http://127.0.0.1:9222"
    _install_fake_http(monkeypatch, {url: _VALID_VERSION_BODY})
    factory = install_playwright(PlaywrightFactory())

    result = cdp_preflight.check_cdp_endpoint(url)

    assert result.ready
    assert len(factory.browsers) == 1
    # Disconnecting the driver must never call browser.close() on the
    # operator-owned Chrome — only the owned context (if any) and the driver
    # itself are torn down.
    assert factory.browsers[0].closed is False


def test_failed_attach_never_closes_the_operator_browser(
    monkeypatch: pytest.MonkeyPatch, install_playwright
) -> None:
    url = "http://127.0.0.1:9222"
    _install_fake_http(monkeypatch, {url: _VALID_VERSION_BODY})
    factory = install_playwright(
        PlaywrightFactory(connect_error=RuntimeError("ECONNREFUSED"))
    )

    cdp_preflight.check_cdp_endpoint(url)

    # connect_over_cdp itself failed, so no browser was ever handed back to
    # close in the first place.
    assert factory.browsers == []
