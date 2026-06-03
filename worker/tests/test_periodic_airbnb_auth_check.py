from __future__ import annotations

import threading

import pytest

import worker.main as worker_main


def _build_job(idx: int) -> dict[str, object]:
    return {
        "id": f"job-{idx}",
        "worker_attempts": 0,
        "job_lane": "interactive",
        "target_env": "production",
    }


def _build_auto_job(idx: int) -> dict[str, object]:
    return {
        "id": f"auto-{idx}",
        "worker_attempts": 0,
    }


def test_maybe_run_periodic_airbnb_auth_check_runs_only_at_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup_runs: list[int] = []

    monkeypatch.setattr(worker_main, "AIRBNB_AUTH_CHECK_EVERY_TASKS", 50)
    monkeypatch.setattr(
        worker_main,
        "_maybe_run_startup_auto_login",
        lambda: startup_runs.append(len(startup_runs) + 1),
    )

    for processed_count in (0, 1, 49, 50, 51, 99, 100):
        worker_main._maybe_run_periodic_airbnb_auth_check(processed_count)

    assert startup_runs == [1, 2]


def test_main_counts_report_jobs_toward_periodic_auth_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown_event = threading.Event()
    claimed_jobs = [_build_job(i) for i in range(50)]
    processed_ids: list[str] = []
    periodic_counts: list[int] = []
    startup_calls: list[str] = []

    def _fake_claim_job(_client, _worker_token, _stale_minutes, _worker_env, _worker_lane):
        if claimed_jobs:
            return claimed_jobs.pop(0)
        shutdown_event.set()
        return None

    def _fake_process_job(job, _worker_token):
        processed_ids.append(str(job["id"]))
        if len(processed_ids) >= 50:
            shutdown_event.set()

    monkeypatch.setattr(worker_main, "_shutdown_event", shutdown_event)
    monkeypatch.setattr(worker_main, "AIRBNB_AUTH_CHECK_EVERY_TASKS", 50)
    monkeypatch.setattr(
        worker_main,
        "_maybe_run_startup_auto_login",
        lambda: startup_calls.append("startup"),
    )
    monkeypatch.setattr(
        worker_main,
        "_maybe_run_periodic_airbnb_auth_check",
        lambda processed_count: periodic_counts.append(int(processed_count)),
    )
    monkeypatch.setattr(worker_main.db_helpers, "get_client", lambda: object())
    monkeypatch.setattr(worker_main.db_helpers, "claim_job", _fake_claim_job)
    monkeypatch.setattr(
        worker_main.db_helpers,
        "claim_price_update_job",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(worker_main, "process_job", _fake_process_job)
    monkeypatch.setattr(
        worker_main,
        "process_price_update_job",
        lambda *_args, **_kwargs: pytest.fail("auto-apply path should not run in this test"),
    )

    worker_main.main()

    assert startup_calls == ["startup"]
    assert processed_ids == [f"job-{idx}" for idx in range(50)]
    assert periodic_counts == list(range(1, 51))


def test_main_counts_auto_apply_jobs_toward_periodic_auth_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown_event = threading.Event()
    claimed_auto_jobs = [_build_auto_job(i) for i in range(50)]
    processed_ids: list[str] = []
    periodic_counts: list[int] = []
    startup_calls: list[str] = []

    def _fake_claim_auto_job(_client, _worker_token, _stale_minutes):
        if claimed_auto_jobs:
            return claimed_auto_jobs.pop(0)
        shutdown_event.set()
        return None

    def _fake_process_auto_job(job, _worker_token, _client):
        processed_ids.append(str(job["id"]))
        if len(processed_ids) >= 50:
            shutdown_event.set()

    monkeypatch.setattr(worker_main, "_shutdown_event", shutdown_event)
    monkeypatch.setattr(worker_main, "AIRBNB_AUTH_CHECK_EVERY_TASKS", 50)
    monkeypatch.setattr(
        worker_main,
        "_maybe_run_startup_auto_login",
        lambda: startup_calls.append("startup"),
    )
    monkeypatch.setattr(
        worker_main,
        "_maybe_run_periodic_airbnb_auth_check",
        lambda processed_count: periodic_counts.append(int(processed_count)),
    )
    monkeypatch.setattr(worker_main.db_helpers, "get_client", lambda: object())
    monkeypatch.setattr(
        worker_main.db_helpers,
        "claim_job",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        worker_main.db_helpers,
        "claim_price_update_job",
        _fake_claim_auto_job,
    )
    monkeypatch.setattr(
        worker_main,
        "process_job",
        lambda *_args, **_kwargs: pytest.fail("report-job path should not run in this test"),
    )
    monkeypatch.setattr(worker_main, "process_price_update_job", _fake_process_auto_job)

    worker_main.main()

    assert startup_calls == ["startup"]
    assert processed_ids == [f"auto-{idx}" for idx in range(50)]
    assert periodic_counts == list(range(1, 51))
