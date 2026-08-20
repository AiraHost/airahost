"""Correlation and event-log tests.

The question these exist to keep answerable is the one the incident could not
answer: *for this report, this listing, these dates — which method served the
search, why did the previous one fail, and what did it cost?* That needs a
stable search_id across the fallback chain, a unique attempt_id per network
attempt, ordered transitions, and no secrets anywhere in the output.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List

from worker.core import scrape_artifacts, scrape_events, scrape_trace
from worker.core.concurrent_runner import execute_day_queries_concurrently
from worker.scraper.playwright_scraper import PlaywrightScraper
from worker.scraper.search_result_contract import classify_search_payload


class _EventCapture(logging.Handler):
    """Collects the structured payloads and the rendered JSONL lines."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.events: List[Dict[str, Any]] = []
        self.lines: List[str] = []
        self._formatter = scrape_events.JsonlEventFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        payload = getattr(record, scrape_events.EVENT_ATTR, None)
        if isinstance(payload, dict):
            self.events.append(payload)
            self.lines.append(self._formatter.format(record))

    def names(self) -> List[str]:
        return [e.get("event") for e in self.events]


class _EventCaptureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.capture = _EventCapture()
        self.events_logger = logging.getLogger("worker.events")
        self.events_logger.addHandler(self.capture)
        self.addCleanup(self.events_logger.removeHandler, self.capture)
        # Artifact capture stays *on* — these tests must cover the real path,
        # including the event fields the artifact store contributes — but writes
        # into a throwaway directory rather than worker/logs.
        self._artifact_dir = TemporaryDirectory()
        self.addCleanup(self._artifact_dir.cleanup)
        previous = os.environ.get("SCRAPER_ARTIFACT_DIR")
        os.environ["SCRAPER_ARTIFACT_DIR"] = self._artifact_dir.name
        self.addCleanup(
            lambda: os.environ.__setitem__("SCRAPER_ARTIFACT_DIR", previous)
            if previous is not None
            else os.environ.pop("SCRAPER_ARTIFACT_DIR", None)
        )
        scrape_artifacts.reset_report_quota()
        self.addCleanup(scrape_artifacts.reset_report_quota)


class TraceContextTest(_EventCaptureCase):
    def test_search_id_is_stable_while_attempt_ids_are_unique(self):
        with scrape_trace.trace_scope(report_id="rep-1", target_listing_id="777"):
            with scrape_trace.search_scope(
                operation="stays_search", checkin="2026-06-01", checkout="2026-06-02", offset=0
            ) as search:
                attempts = [scrape_trace.new_attempt_id() for _ in range(4)]
                for attempt in attempts:
                    scrape_events.emit("probe", attempt_id=attempt)

        search_ids = {e["search_id"] for e in self.capture.events}
        self.assertEqual(search_ids, {search.search_id})
        emitted_attempts = [e["attempt_id"] for e in self.capture.events]
        self.assertEqual(len(set(emitted_attempts)), 4, "attempt_ids were reused")
        for event in self.capture.events:
            self.assertEqual(event["report_id"], "rep-1")
            self.assertEqual(event["target_listing_id"], "777")
            self.assertEqual(event["checkin"], "2026-06-01")
            self.assertEqual(event["offset"], 0)
            self.assertTrue(event["trace_id"])
            self.assertTrue(event["worker_instance_id"])

    def test_nested_search_scopes_do_not_leak(self):
        with scrape_trace.trace_scope(report_id="rep-2"):
            with scrape_trace.search_scope(operation="a") as outer:
                with scrape_trace.search_scope(operation="b") as inner:
                    self.assertNotEqual(outer.search_id, inner.search_id)
                self.assertEqual(scrape_trace.current_search().search_id, outer.search_id)
            self.assertIsNone(scrape_trace.current_search())

    def test_context_crosses_the_day_query_thread_pool(self):
        """contextvars do not cross threads on their own.

        Without explicit propagation every concurrent day query would emit
        events with no report attached — which is what makes a report's
        fallback chain unreconstructable.
        """
        seen: List[Any] = []
        lock = threading.Lock()

        def query(value: int) -> Dict[str, Any]:
            trace = scrape_trace.current_trace()
            with lock:
                seen.append(trace.report_id if trace else None)
            return {"median_price": value}

        with scrape_trace.trace_scope(report_id="rep-3"):
            execute_day_queries_concurrently(query, [1, 2, 3, 4], max_workers=4)

        self.assertEqual(set(seen), {"rep-3"})

    def test_one_propagated_callable_serves_overlapping_threads(self):
        """A reused wrapper must not fail once tasks genuinely overlap.

        A ``Context`` cannot be entered twice at once, so a wrapper that closed
        over one copy raised ``RuntimeError: cannot enter context`` and killed
        the whole report. Short tasks rarely overlap, which is why this needs a
        barrier: without it the bug hides behind scheduling luck.
        """
        workers = 8
        barrier = threading.Barrier(workers, timeout=10)

        def query(value: int) -> Dict[str, Any]:
            # Nobody leaves until every worker is inside the context at once.
            barrier.wait()
            trace = scrape_trace.current_trace()
            return {"median_price": value, "report": trace.report_id if trace else None}

        with scrape_trace.trace_scope(report_id="rep-overlap"):
            results, _state = execute_day_queries_concurrently(
                query, list(range(workers)), max_workers=workers
            )

        self.assertEqual(len(results), workers)
        self.assertEqual({r["report"] for r in results}, {"rep-overlap"})

    def test_propagated_scope_does_not_leak_between_sibling_tasks(self):
        """Each task gets its own context, so a nested scope stays task-local."""
        seen: List[Any] = []
        lock = threading.Lock()

        def query(value: int) -> Dict[str, Any]:
            with scrape_trace.search_scope(operation="day_query", checkin=f"d{value}"):
                with lock:
                    seen.append(scrape_trace.context_fields().get("checkin"))
            return {"median_price": value}

        with scrape_trace.trace_scope(report_id="rep-isolation"):
            execute_day_queries_concurrently(query, list(range(6)), max_workers=6)
            # A child task's scope must not follow the result back to the caller.
            self.assertIsNone(scrape_trace.current_search())

        self.assertEqual(sorted(seen), [f"d{i}" for i in range(6)])

    def test_events_outside_a_scope_still_emit(self):
        """Logging must never depend on a scope being open."""
        scrape_events.emit("orphan_probe", note="no scope")
        self.assertIn("orphan_probe", self.capture.names())
        self.assertNotIn("trace_id", self.capture.events[-1])


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, text: str = "", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers})
        return self._responses.pop(0)


def _search_template() -> Dict[str, Any]:
    return {
        "url": "https://www.airbnb.com/api/v3/StaysSearch/abc123?operationName=StaysSearch",
        "method": "POST",
        "headers": {"content-type": "application/json", "cookie": "sid=supersecretvalue"},
        "post_data": {
            "operationName": "StaysSearch",
            "variables": {"staysSearchRequest": {"rawParams": []}},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "deadbeef"}},
        },
    }


class FallbackTransitionTest(_EventCaptureCase):
    def setUp(self) -> None:
        super().setUp()
        from worker.core import admission

        admission.reset_admission_controller_for_tests()
        self.addCleanup(admission.reset_admission_controller_for_tests)

    def _scraper(self) -> PlaywrightScraper:
        scraper = PlaywrightScraper({"USE_HARDCODED_STAYSPDP_TEMPLATE": "0"})
        scraper.captured_search_req = _search_template()
        return scraper

    def test_direct_failure_then_playwright_emits_an_ordered_sequence(self):
        """One logical search, one search_id, an ordered chain of transitions."""
        scraper = self._scraper()
        challenge = {"errors": [{"extensions": {"code": "CAPTCHA"}}]}
        scraper.session = _FakeSession([_FakeResponse(200, challenge)])

        captured: Dict[str, Any] = {}

        def fake_browser_search(overrides, *, op_name):
            captured["search_id"] = scrape_trace.current_search().search_id
            captured["op_name"] = op_name
            return 200, {"data": {"presentation": {"staysSearch": {"results": {"searchResults": []}}}}}

        scraper._run_browser_search = fake_browser_search

        with scrape_trace.trace_scope(report_id="rep-fallback", target_listing_id="42"):
            scraper.search_listings_with_overrides(
                {"checkin": "2026-06-01", "checkout": "2026-06-02", "itemsOffset": 25}
            )

        names = self.capture.names()
        self.assertIn(scrape_events.DIRECT_HTTP_STARTED, names)
        self.assertIn(scrape_events.DIRECT_HTTP_BLOCKED, names)
        self.assertIn(scrape_events.FALLBACK_SELECTED, names)
        self.assertLess(
            names.index(scrape_events.DIRECT_HTTP_STARTED),
            names.index(scrape_events.DIRECT_HTTP_BLOCKED),
        )
        self.assertLess(
            names.index(scrape_events.DIRECT_HTTP_BLOCKED),
            names.index(scrape_events.FALLBACK_SELECTED),
        )

        # One logical search: every event shares the search_id the browser path saw.
        search_ids = {e["search_id"] for e in self.capture.events if "search_id" in e}
        self.assertEqual(search_ids, {captured["search_id"]})

        # Context needed to answer "which search was this?" is present.
        blocked = next(
            e for e in self.capture.events if e["event"] == scrape_events.DIRECT_HTTP_BLOCKED
        )
        self.assertEqual(blocked["report_id"], "rep-fallback")
        self.assertEqual(blocked["checkin"], "2026-06-01")
        self.assertEqual(blocked["offset"], 25)
        self.assertEqual(blocked["reason_code"], "graphql_auth_error")
        self.assertEqual(blocked["source"], scrape_trace.SOURCE_DIRECT_JSON)

        # The decision is auditable: the event points at the saved payload and
        # names the evidence path that matched, and the file really exists.
        self.assertIn("errors[0].extensions.code", blocked["evidence_paths"])
        self.assertIn("artifact_id", blocked)
        artifact_file = Path(self._artifact_dir.name) / blocked["artifact_path"]
        self.assertTrue(artifact_file.exists())
        replayed = json.loads(artifact_file.read_text(encoding="utf-8"))
        self.assertTrue(classify_search_payload(replayed, 200).is_blocked)

        fallback = next(
            e for e in self.capture.events if e["event"] == scrape_events.FALLBACK_SELECTED
        )
        self.assertEqual(fallback["fallback_from"], scrape_trace.SOURCE_DIRECT_JSON)
        self.assertEqual(fallback["source"], scrape_trace.SOURCE_PLAYWRIGHT_CAPTURE)
        self.assertEqual(fallback["fallback_reason"], "graphql_auth_error")

    def test_direct_success_records_no_fallback(self):
        scraper = self._scraper()
        ok = {
            "data": {
                "presentation": {
                    "staysSearch": {"results": {"searchResults": [{"listingId": "1"}]}}
                }
            }
        }
        scraper.session = _FakeSession([_FakeResponse(200, ok)])
        with scrape_trace.trace_scope(report_id="rep-ok"):
            status, _ = scraper.search_listings_with_overrides({"checkin": "2026-06-01"})
        self.assertEqual(status, 200)
        names = self.capture.names()
        self.assertIn(scrape_events.DIRECT_HTTP_SUCCEEDED, names)
        self.assertNotIn(scrape_events.FALLBACK_SELECTED, names)

    def test_overload_retries_reenter_the_limiter_and_are_bounded(self):
        """Retries must go back through admission, not around it."""
        scraper = self._scraper()
        ok = {"data": {"presentation": {"staysSearch": {"results": {"searchResults": []}}}}}
        scraper.session = _FakeSession(
            [
                _FakeResponse(503, text="slow down", headers={"Retry-After": "0"}),
                _FakeResponse(200, ok),
            ]
        )
        with scrape_trace.trace_scope(
            report_id="rep-503", retry_budget=scrape_trace.RetryBudget(5, 5)
        ):
            result = scraper.fetch_search_direct({"checkin": "2026-06-01"})

        self.assertIsNotNone(result)
        names = self.capture.names()
        self.assertEqual(names.count(scrape_events.DIRECT_HTTP_STARTED), 2)
        self.assertIn(scrape_events.DIRECT_HTTP_OVERLOADED, names)
        self.assertIn(scrape_events.COOLDOWN_STARTED, names)
        self.assertIn(scrape_events.RETRY_SCHEDULED, names)

        started = [
            e for e in self.capture.events if e["event"] == scrape_events.DIRECT_HTTP_STARTED
        ]
        self.assertEqual(len({e["attempt_id"] for e in started}), 2)
        self.assertEqual([e["attempt_number"] for e in started], [1, 2])

    def test_exhausted_report_budget_stops_retrying(self):
        scraper = self._scraper()
        scraper.session = _FakeSession(
            [_FakeResponse(503, text="a"), _FakeResponse(503, text="b"), _FakeResponse(503, text="c")]
        )
        with scrape_trace.trace_scope(
            report_id="rep-budget", retry_budget=scrape_trace.RetryBudget(5, 0)
        ):
            self.assertIsNone(scraper.fetch_search_direct({"checkin": "2026-06-01"}))
        names = self.capture.names()
        self.assertIn(scrape_events.RETRY_BUDGET_EXHAUSTED, names)
        self.assertEqual(names.count(scrape_events.DIRECT_HTTP_STARTED), 1)


class LogHygieneTest(_EventCaptureCase):
    def setUp(self) -> None:
        super().setUp()
        from worker.core import admission

        admission.reset_admission_controller_for_tests()
        self.addCleanup(admission.reset_admission_controller_for_tests)

    def test_no_secrets_or_bodies_reach_the_event_log(self):
        """Events are metadata. Bodies, cookies and query strings are not.

        The regression this guards is the 4,000-character HTML preview warning
        that used to be emitted on every suspicious page.
        """
        scraper = PlaywrightScraper({"USE_HARDCODED_STAYSPDP_TEMPLATE": "0"})
        template = _search_template()
        template["url"] = (
            "https://www.airbnb.com/api/v3/StaysSearch/"
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            "?operationName=StaysSearch&key=SUPERSECRETAPIKEY&query=123%20Elm%20Street"
        )
        scraper.captured_search_req = template
        secret_html = (
            "<html>Set-Cookie: sid=supersecretvalue; "
            "guest@example.com <script>var token='abcdefghijklmnopqrstuvwxyz0123456789ABCD';</script>"
            "</html>"
        )
        scraper.session = _FakeSession([_FakeResponse(403, text=secret_html)])

        with scrape_trace.trace_scope(report_id="rep-hygiene"):
            scraper.fetch_search_direct({"checkin": "2026-06-01"})

        blob = "\n".join(self.capture.lines)
        self.assertTrue(blob, "expected events")
        for forbidden in (
            "supersecretvalue",
            "SUPERSECRETAPIKEY",
            "guest@example.com",
            "abcdefghijklmnopqrstuvwxyz0123456789ABCD",
            "Set-Cookie",
            "123 Elm Street",
            "123%20Elm%20Street",
        ):
            self.assertNotIn(forbidden, blob, f"{forbidden!r} leaked into the event log")

        # The endpoint is still identifiable, minus the query and the hash.
        endpoint_events = [e for e in self.capture.events if e.get("endpoint_path")]
        self.assertTrue(endpoint_events)
        self.assertEqual(endpoint_events[0]["endpoint_host"], "www.airbnb.com")
        self.assertIn("StaysSearch", endpoint_events[0]["endpoint_path"])
        self.assertNotIn("aaaaaaaa", endpoint_events[0]["endpoint_path"])

    def test_every_line_is_valid_json_with_the_required_fields(self):
        with scrape_trace.trace_scope(report_id="rep-schema"):
            with scrape_trace.search_scope(operation="stays_search"):
                scrape_events.emit(
                    scrape_events.DIRECT_HTTP_STARTED,
                    source=scrape_trace.SOURCE_DIRECT_JSON,
                    status=200,
                )
        for line in self.capture.lines:
            parsed = json.loads(line)
            for required in ("ts", "schema_version", "event", "severity", "logger"):
                self.assertIn(required, parsed)
            self.assertTrue(parsed["ts"].endswith("Z"), "timestamp must be UTC ISO-8601")

    def test_a_broken_sink_does_not_raise_into_the_scraper(self):
        """Logging failures must never fail a report."""

        class _Exploding(logging.Handler):
            def emit(self, record):
                raise RuntimeError("sink is down")

        boom = _Exploding()
        logger = logging.getLogger("worker.events")
        logger.addHandler(boom)
        try:
            scrape_events.emit("probe_after_sink_failure", note="still fine")
        finally:
            logger.removeHandler(boom)


class EventSinkInstallationTest(unittest.TestCase):
    def _enable_sink_in_tempdir(self) -> None:
        """Turn the sink on, writing to a throwaway path.

        The test package disables it and redirects it away from
        ``worker/logs/worker.jsonl`` so a test run cannot inject fabricated
        events into the log an operator reads during an incident.
        """
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for key, value in (
            ("WORKER_EVENT_LOG_ENABLED", "1"),
            ("WORKER_EVENT_LOG_PATH", os.path.join(tmp.name, "events.jsonl")),
        ):
            previous = os.environ.get(key)
            os.environ[key] = value
            self.addCleanup(
                lambda k=key, p=previous: os.environ.__setitem__(k, p)
                if p is not None
                else os.environ.pop(k, None)
            )

    def test_sink_is_installed_at_most_once(self):
        self._enable_sink_in_tempdir()
        scrape_events.uninstall_event_sink()
        self.addCleanup(scrape_events.uninstall_event_sink)
        first = scrape_events.install_event_sink()
        second = scrape_events.install_event_sink()
        self.assertIsNotNone(first)
        self.assertIs(first, second)
        logger = logging.getLogger("worker.events")
        tagged = [h for h in logger.handlers if hasattr(h, "_airahost_event_handler")]
        self.assertEqual(len(tagged), 1)

    def test_sink_can_be_disabled_so_a_test_run_cannot_write_to_the_operator_log(self):
        previous = os.environ.get("WORKER_EVENT_LOG_ENABLED")
        os.environ["WORKER_EVENT_LOG_ENABLED"] = "0"
        self.addCleanup(
            lambda: os.environ.__setitem__("WORKER_EVENT_LOG_ENABLED", previous)
            if previous is not None
            else os.environ.pop("WORKER_EVENT_LOG_ENABLED", None)
        )
        scrape_events.uninstall_event_sink()
        self.addCleanup(scrape_events.uninstall_event_sink)
        self.assertIsNone(scrape_events.install_event_sink())

    def test_sink_only_writes_structured_events(self):
        """Ordinary console log records must not end up in the JSONL stream."""
        capture = _EventCapture()
        handler_filter = scrape_events._EventOnlyFilter()
        record = logging.LogRecord("worker.events", logging.INFO, __file__, 1, "plain", None, None)
        self.assertFalse(handler_filter.filter(record))
        setattr(record, scrape_events.EVENT_ATTR, {"event": "x"})
        self.assertTrue(handler_filter.filter(record))
        del capture


if __name__ == "__main__":
    unittest.main()
