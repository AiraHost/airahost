"""Startup identity logging — provenance evidence for the next incident.

The idle-worker PDP-failure investigation
(docs/scraper implementation prompts/investigate_idle_worker_pdp_failure.md)
found that an operator inspecting worker logs could not tell which process,
host, PID, or code revision actually produced a given report's error, because
none of that was logged anywhere. This pins down what
`_log_worker_startup_identity()` must emit so a future incident's timeline can
be built without guessing.
"""

from __future__ import annotations

import logging
import os
import re

import worker.main as worker_main


def test_startup_identity_logs_process_and_host_facts(caplog):
    with caplog.at_level(logging.INFO, logger=worker_main.logger.name):
        worker_instance_id = worker_main._log_worker_startup_identity()

    messages = "\n".join(r.getMessage() for r in caplog.records)

    assert re.fullmatch(r"[0-9a-f]{12}", worker_instance_id), (
        "instance id must be a short, non-secret opaque token"
    )
    assert f"instance_id={worker_instance_id}" in messages
    assert f"pid={os.getpid()}" in messages
    assert "host=" in messages
    assert "start_time_utc=" in messages
    assert "executable=" in messages
    assert "cwd=" in messages
    assert f"version={worker_main.WORKER_VERSION}" in messages


def test_startup_identity_is_unique_per_process_start(caplog):
    with caplog.at_level(logging.INFO, logger=worker_main.logger.name):
        first = worker_main._log_worker_startup_identity()
        second = worker_main._log_worker_startup_identity()

    assert first != second, (
        "each worker start must get its own instance id so two overlapping "
        "revisions/processes on the same host are distinguishable in logs"
    )
