"""Regression coverage: a failed CDP attach must never silently launch a
replacement browser (the production incident this fix addresses).

Deterministic — no live Airbnb, no real Chrome. ``sync_playwright`` is
replaced with a minimal fake whose ``chromium.launch`` either must not be
called at all (default config) or is reached only when explicitly opted in.
"""

from __future__ import annotations

from typing import List, Optional

import pytest

from worker import airbnb_auto_login
from worker.scraper.scraper_errors import CdpAttachFailed


class _MarkerLaunchCalled(Exception):
    """Proves chromium.launch() was reached, without needing a full browser fake."""


class _RefusingChromium:
    """connect_over_cdp always fails; launch() behavior is injected per test."""

    def __init__(self, on_launch=None) -> None:
        self.launch_calls: List[bool] = []
        self._on_launch = on_launch

    def connect_over_cdp(self, url: str, timeout: Optional[int] = None):
        raise RuntimeError("ECONNREFUSED")

    def launch(self, headless: bool = False):
        self.launch_calls.append(headless)
        if self._on_launch is not None:
            return self._on_launch(headless)
        raise AssertionError(
            "chromium.launch() must not be called when CDP attach fails and "
            "AIRAHOST_ALLOW_BROWSER_LAUNCH is not enabled"
        )


class _FakeSyncPlaywright:
    def __init__(self, chromium) -> None:
        self.chromium = chromium

    def __enter__(self) -> "_FakeSyncPlaywright":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def test_run_login_flow_raises_instead_of_launching_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("AIRAHOST_EMAIL", "qa@example.com")
    monkeypatch.delenv("AIRAHOST_ALLOW_BROWSER_LAUNCH", raising=False)
    chromium = _RefusingChromium()
    monkeypatch.setattr(
        airbnb_auto_login, "sync_playwright", lambda: _FakeSyncPlaywright(chromium)
    )

    with pytest.raises(CdpAttachFailed) as excinfo:
        airbnb_auto_login.run_login_flow(
            out_dir=tmp_path / "artifacts",
            dump_only=False,
            cdp_url="http://127.0.0.1:9222",
        )

    assert excinfo.value.endpoint == "127.0.0.1:9222"
    assert excinfo.value.reason == "RuntimeError"
    # Exception chaining preserved (raise ... from exc).
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert chromium.launch_calls == []


def test_run_login_flow_default_config_never_launches_regardless_of_dump_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """dump_only historically short-circuited before login logic ran — but the
    CDP attach itself happens first, so this must fail loud either way."""
    monkeypatch.setenv("AIRAHOST_EMAIL", "qa@example.com")
    monkeypatch.delenv("AIRAHOST_ALLOW_BROWSER_LAUNCH", raising=False)
    chromium = _RefusingChromium()
    monkeypatch.setattr(
        airbnb_auto_login, "sync_playwright", lambda: _FakeSyncPlaywright(chromium)
    )

    with pytest.raises(CdpAttachFailed):
        airbnb_auto_login.run_login_flow(
            out_dir=tmp_path / "artifacts",
            dump_only=True,
            cdp_url="http://127.0.0.1:9222",
        )
    assert chromium.launch_calls == []


def test_run_login_flow_opt_in_flag_reaches_launch_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """AIRAHOST_ALLOW_BROWSER_LAUNCH=true is the only way to reach launch()."""
    monkeypatch.setenv("AIRAHOST_EMAIL", "qa@example.com")
    monkeypatch.setenv("AIRAHOST_ALLOW_BROWSER_LAUNCH", "true")
    chromium = _RefusingChromium(on_launch=lambda _headless: (_ for _ in ()).throw(_MarkerLaunchCalled()))
    monkeypatch.setattr(
        airbnb_auto_login, "sync_playwright", lambda: _FakeSyncPlaywright(chromium)
    )

    with pytest.raises(_MarkerLaunchCalled):
        airbnb_auto_login.run_login_flow(
            out_dir=tmp_path / "artifacts",
            dump_only=True,
            cdp_url="http://127.0.0.1:9222",
        )
    assert chromium.launch_calls == [False]


def test_run_login_flow_uses_explicit_cdp_url_without_touching_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Endpoint routing must not depend on mutating os.environ["CDP_URL"]."""
    import os

    monkeypatch.setenv("AIRAHOST_EMAIL", "qa@example.com")
    monkeypatch.delenv("CDP_URL", raising=False)
    connect_targets: List[str] = []

    class _RecordingChromium:
        def connect_over_cdp(self, url: str, timeout: Optional[int] = None):
            connect_targets.append(url)
            raise RuntimeError("ECONNREFUSED")

        def launch(self, headless: bool = False):
            raise AssertionError("must not launch")

    monkeypatch.setattr(
        airbnb_auto_login,
        "sync_playwright",
        lambda: _FakeSyncPlaywright(_RecordingChromium()),
    )

    with pytest.raises(CdpAttachFailed):
        airbnb_auto_login.run_login_flow(
            out_dir=tmp_path / "artifacts",
            dump_only=False,
            cdp_url="http://127.0.0.1:9223",
        )

    assert connect_targets == ["http://127.0.0.1:9223"]
    # CDP_URL was never set as a side effect of routing to this endpoint.
    assert os.environ.get("CDP_URL") is None
