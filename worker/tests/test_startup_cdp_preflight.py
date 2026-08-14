"""Startup CDP preflight + auth-check orchestration in worker.main.

Deterministic — the CDP preflight and the Airbnb login-state probe are both
monkeypatched, so these tests exercise only the decision logic: which
endpoints get used, when auto-login runs, and when startup must fail fast.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

import worker.main as worker_main
from worker.scraper import cdp_preflight


def _preflight_result(url: str, status: str, reason: str = "") -> cdp_preflight.CdpPreflightResult:
    return cdp_preflight.CdpPreflightResult(
        url=url, label=cdp_preflight.endpoint_label(url), status=status, reason=reason
    )


def _patch_preflight(monkeypatch: pytest.MonkeyPatch, results: List[cdp_preflight.CdpPreflightResult]) -> None:
    monkeypatch.setattr(
        worker_main.cdp_preflight, "check_cdp_endpoints", lambda urls, **kw: results
    )


def _patch_resolve_urls(monkeypatch: pytest.MonkeyPatch, urls: List[str]) -> None:
    monkeypatch.setattr(worker_main, "_resolve_startup_cdp_urls", lambda: urls)


def test_probe_unavailable_does_not_invoke_auto_login(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "http://127.0.0.1:9222"
    _patch_resolve_urls(monkeypatch, [url])
    _patch_preflight(monkeypatch, [_preflight_result(url, cdp_preflight.STATUS_READY)])
    monkeypatch.setattr(worker_main, "_probe_airbnb_login_state", lambda *_a, **_k: None)

    login_calls: List[Any] = []
    monkeypatch.setattr(
        worker_main,
        "_run_startup_auto_login_for_cdp",
        lambda *a, **k: login_calls.append((a, k)) or True,
    )

    worker_main._maybe_run_startup_auto_login()

    assert login_calls == []


def test_confirmed_logged_out_runs_auto_login_only_against_that_endpoint_and_reverifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "http://127.0.0.1:9222"
    _patch_resolve_urls(monkeypatch, [url])
    _patch_preflight(monkeypatch, [_preflight_result(url, cdp_preflight.STATUS_READY)])

    probe_calls: List[str] = []
    # First probe (before login): logged out. Second probe (after login attempt): logged in.
    probe_sequence = iter([False, True])

    def _fake_probe(cdp_url: str, _timeout_ms: int):
        probe_calls.append(cdp_url)
        return next(probe_sequence)

    monkeypatch.setattr(worker_main, "_probe_airbnb_login_state", _fake_probe)

    login_calls: List[Any] = []

    def _fake_login(cdp_url: str, slot_index: int) -> bool:
        login_calls.append((cdp_url, slot_index))
        return True

    monkeypatch.setattr(worker_main, "_run_startup_auto_login_for_cdp", _fake_login)

    worker_main._maybe_run_startup_auto_login()

    assert login_calls == [(url, 0)]
    assert probe_calls == [url, url]  # pre-login probe, post-login re-verify


def test_run_startup_auto_login_for_cdp_passes_explicit_url_without_env_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    captured: Dict[str, Any] = {}

    def _fake_run_login_flow(*, out_dir, dump_only, cdp_url=None):
        captured["out_dir"] = out_dir
        captured["dump_only"] = dump_only
        captured["cdp_url"] = cdp_url
        captured["env_cdp_url_during_call"] = os.environ.get("CDP_URL")

    monkeypatch.setattr(
        "worker.airbnb_auto_login.run_login_flow", _fake_run_login_flow
    )
    monkeypatch.delenv("CDP_URL", raising=False)

    ok = worker_main._run_startup_auto_login_for_cdp("http://127.0.0.1:9223", 1)

    assert ok is True
    assert captured["cdp_url"] == "http://127.0.0.1:9223"
    # No global env mutation before or during the call.
    assert captured["env_cdp_url_during_call"] is None
    assert os.environ.get("CDP_URL") is None


def test_one_unhealthy_endpoint_leaves_two_healthy_endpoints_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy_a = "http://127.0.0.1:9222"
    broken = "http://127.0.0.1:9223"
    healthy_b = "http://127.0.0.1:9224"
    _patch_resolve_urls(monkeypatch, [healthy_a, broken, healthy_b])
    _patch_preflight(
        monkeypatch,
        [
            _preflight_result(healthy_a, cdp_preflight.STATUS_READY),
            _preflight_result(broken, cdp_preflight.STATUS_CDP_ATTACH_FAILED, "cdp_connect_failed"),
            _preflight_result(healthy_b, cdp_preflight.STATUS_READY),
        ],
    )

    probed: List[str] = []
    monkeypatch.setattr(
        worker_main,
        "_probe_airbnb_login_state",
        lambda cdp_url, _timeout_ms: probed.append(cdp_url) or True,
    )
    monkeypatch.setattr(
        worker_main,
        "_run_startup_auto_login_for_cdp",
        lambda *a, **k: pytest.fail("auto-login should not run for logged-in endpoints"),
    )

    worker_main._maybe_run_startup_auto_login()

    assert probed == [healthy_a, healthy_b]
    assert broken not in probed


def test_all_endpoints_unhealthy_fails_startup_once_with_no_login_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = ["http://127.0.0.1:9222", "http://127.0.0.1:9223", "http://127.0.0.1:9224"]
    _patch_resolve_urls(monkeypatch, urls)
    _patch_preflight(
        monkeypatch,
        [
            _preflight_result(urls[0], cdp_preflight.STATUS_TCP_OR_HTTP_UNAVAILABLE),
            _preflight_result(urls[1], cdp_preflight.STATUS_TCP_OR_HTTP_UNAVAILABLE),
            _preflight_result(urls[2], cdp_preflight.STATUS_TCP_OR_HTTP_UNAVAILABLE),
        ],
    )
    monkeypatch.setattr(
        worker_main,
        "_probe_airbnb_login_state",
        lambda *a, **k: pytest.fail("no probe when transport is unavailable"),
    )
    monkeypatch.setattr(
        worker_main,
        "_run_startup_auto_login_for_cdp",
        lambda *a, **k: pytest.fail("no auto-login when transport is unavailable"),
    )

    with pytest.raises(worker_main.StartupCdpUnavailable) as excinfo:
        worker_main._maybe_run_startup_auto_login()

    message = str(excinfo.value)
    for url in urls:
        assert cdp_preflight.endpoint_label(url) in message
