"""Each worker log event must be emitted exactly once.

The incident log contained duplicated console/formatted copies of nearly every
event, which made a single search attempt look like several and hid how many
navigations actually happened. Cause: worker/core/auto_price_assignment.py ran
logging.basicConfig() at import time — before main.py installed its own root
handlers — so every record went out twice, once through basicConfig's stderr
handler and once through main.py's formatted console handler.
"""

from __future__ import annotations

import importlib
import logging


def test_library_modules_do_not_configure_the_root_logger():
    root = logging.getLogger()
    before = list(root.handlers)

    module = importlib.import_module("worker.core.auto_price_assignment")
    importlib.reload(module)

    assert list(root.handlers) == before, (
        "importing a library module must not add root handlers"
    )


def test_worker_main_installs_each_handler_at_most_once():
    import worker.main as worker_main

    root = logging.getLogger()
    tagged = [h for h in root.handlers if hasattr(h, worker_main._HANDLER_TAG)]
    kinds = [getattr(h, worker_main._HANDLER_TAG) for h in tagged]

    assert sorted(kinds) == ["console", "file"]
    assert len(kinds) == len(set(kinds)), "no handler kind installed twice"

    # Re-importing must not attach a second copy of either handler.
    importlib.reload(worker_main)
    kinds_after = [
        getattr(h, worker_main._HANDLER_TAG)
        for h in logging.getLogger().handlers
        if hasattr(h, worker_main._HANDLER_TAG)
    ]
    assert sorted(kinds_after) == ["console", "file"]


def test_asyncio_failures_are_not_duplicated_or_globally_suppressed(caplog):
    """One asyncio failure, one record — and asyncio logging stays enabled.

    The incident log showed the same `Task exception was never retrieved`
    rendered twice: once unformatted (`ERROR:asyncio:...`, the shape
    logging.basicConfig() produces) and once through the worker's formatted
    handler. The fix for the underlying failure is to retrieve the exception,
    not to filter the text or mute the logger, so this pins both halves: the
    asyncio logger carries no extra handlers of its own, is not disabled, and
    emits exactly one record per call.
    """
    import worker.main as worker_main  # noqa: F401 - installs the root handlers

    asyncio_logger = logging.getLogger("asyncio")
    assert asyncio_logger.handlers == [], "asyncio must log through root only"
    assert asyncio_logger.disabled is False
    assert asyncio_logger.propagate is True

    root_kinds = [
        getattr(h, worker_main._HANDLER_TAG)
        for h in logging.getLogger().handlers
        if hasattr(h, worker_main._HANDLER_TAG)
    ]
    assert sorted(root_kinds) == ["console", "file"]
    # pytest attaches its own capture handlers; anything else untagged would be
    # a stray basicConfig() handler and would double every record.
    strays = [
        h
        for h in logging.getLogger().handlers
        if not hasattr(h, worker_main._HANDLER_TAG)
        and type(h).__module__.split(".")[0] != "_pytest"
    ]
    assert strays == []

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        asyncio_logger.error("Task exception was never retrieved")

    probes = [
        r for r in caplog.records if "Task exception was never retrieved" in r.getMessage()
    ]
    assert len(probes) == 1


def test_a_single_worker_log_call_produces_one_record_per_handler(caplog):
    import worker.main as worker_main

    with caplog.at_level(logging.INFO, logger="worker.scraper.comp_collection"):
        logging.getLogger("worker.scraper.comp_collection").info("page_funnel probe")

    probes = [r for r in caplog.records if r.getMessage() == "page_funnel probe"]
    assert len(probes) == 1
