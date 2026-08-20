"""Artifact-store tests.

The store exists so an operator can answer "was that block real?" — which means
the saved payload has to be (a) the exact structure the classifier acted on, so
a replay reproduces the verdict, and (b) free of anything that must not be
written to disk. Both halves are load-bearing; a store that leaks a session
cookie is worse than no store.
"""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from worker.core import scrape_artifacts
from worker.scraper.search_result_contract import (
    auth_error_evidence_paths,
    classify_search_payload,
)


class _ArtifactCase(unittest.TestCase):
    """Runs each test against an isolated artifact root with capture enabled."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "SCRAPER_ARTIFACT_CAPTURE_ENABLED",
                "SCRAPER_ARTIFACT_CAPTURE_FULL_PAYLOADS",
                "SCRAPER_ARTIFACT_DIR",
                "SCRAPER_ARTIFACT_MAX_BYTES",
                "SCRAPER_ARTIFACT_MAX_PER_REPORT",
                "SCRAPER_ARTIFACT_MAX_TOTAL_BYTES",
                "SCRAPER_ARTIFACT_RETENTION_DAYS",
            )
        }
        os.environ["SCRAPER_ARTIFACT_CAPTURE_ENABLED"] = "1"
        os.environ["SCRAPER_ARTIFACT_DIR"] = str(self.root)
        os.environ.pop("SCRAPER_ARTIFACT_CAPTURE_FULL_PAYLOADS", None)
        scrape_artifacts.reset_report_quota()
        self.addCleanup(self._restore_env)
        self.addCleanup(scrape_artifacts.reset_report_quota)

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _capture(self, body, **kwargs):
        params = dict(
            capture_reason="test_capture",
            reason_code="graphql_auth_error",
            source="direct_json",
            report_id="rep-1",
            root=self.root,
        )
        params.update(kwargs)
        return scrape_artifacts.capture_artifact(body, **params)


BLOCKED_PAYLOAD = {
    "errors": [
        {
            "message": "You must be logged in to continue",
            "extensions": {"code": "UNAUTHORIZED", "response": {"status": 403}},
        }
    ],
    "data": None,
}


class BlockedPayloadReplayTest(_ArtifactCase):
    def test_blocked_json_is_saved_as_json_and_replays_to_the_same_verdict(self):
        """A Python repr on disk would make the artifact unreplayable.

        The point of storing the body is that feeding it back through the
        classifier reproduces the decision; that only works if it is still JSON.
        """
        reason = classify_search_payload(BLOCKED_PAYLOAD, 200).reason_code
        reference = self._capture(BLOCKED_PAYLOAD, reason_code=reason)
        self.assertIsNotNone(reference)
        self.assertEqual(reference["artifact_format"], "json")
        self.assertTrue(reference["artifact_path"].endswith(".json"))

        replayed = scrape_artifacts.load_artifact(reference["artifact_path"], root=self.root)
        self.assertIsInstance(replayed, dict)
        self.assertEqual(classify_search_payload(replayed, 200).reason_code, reason)
        self.assertTrue(classify_search_payload(replayed, 200).is_blocked)

    def test_reference_carries_everything_needed_to_find_and_trust_the_file(self):
        import hashlib

        reference = self._capture(BLOCKED_PAYLOAD)
        target = self.root / reference["artifact_path"]
        self.assertTrue(target.exists())
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        self.assertEqual(reference["artifact_sha256"], digest)
        self.assertEqual(reference["artifact_stored_bytes"], target.stat().st_size)
        self.assertFalse(reference["artifact_truncated"])
        self.assertEqual(reference["capture_reason"], "test_capture")

    def test_evidence_paths_name_the_match_without_copying_its_value(self):
        paths = auth_error_evidence_paths(BLOCKED_PAYLOAD)
        self.assertIn("errors[0].extensions.code", paths)
        reference = self._capture(BLOCKED_PAYLOAD, evidence_paths=paths)
        self.assertIn("errors[0].extensions.code", reference["evidence_paths"])
        self.assertNotIn("UNAUTHORIZED", json.dumps(reference["evidence_paths"]))


class RedactionTest(_ArtifactCase):
    SECRETS = {
        "cookie": "sid=supersecretvalue",
        "authorization": "Bearer abcdefghijklmnopqrstuvwxyz",
        "x-airbnb-api-key": "d306zoyjsyarp7ifhu67rjxn52tv0t20",
        "sessionId": "sess-1234567890",
        "nested": {
            "refresh_token": "rt-should-not-persist",
            "user": {"email": "guest@example.com", "phone": "+1 415 555 0199"},
            "signedUrl": "https://cdn.example.com/a.jpg?sig=SIGNATUREVALUE&expires=99",
        },
        "listingId": "12345",
    }

    def test_secrets_and_pii_are_absent_from_the_stored_file(self):
        reference = self._capture(self.SECRETS)
        stored = (self.root / reference["artifact_path"]).read_text(encoding="utf-8")
        for forbidden in (
            "supersecretvalue",
            "abcdefghijklmnopqrstuvwxyz",
            "d306zoyjsyarp7ifhu67rjxn52tv0t20",
            "sess-1234567890",
            "rt-should-not-persist",
            "guest@example.com",
            "415 555 0199",
            "SIGNATUREVALUE",
        ):
            self.assertNotIn(forbidden, stored, f"{forbidden!r} was persisted")
        # Non-secret diagnostic content survives, or the artifact is useless.
        self.assertIn("12345", stored)
        self.assertIn("listingId", stored)

    def test_html_bodies_are_redacted_too(self):
        html = (
            "<html><head>Set-Cookie: sid=supersecretvalue</head>"
            "<body>guest@example.com <a href='/x?token=TOKENVALUE'>x</a>"
            "<div data-testid='card-container'>listing</div></body></html>"
        )
        reference = self._capture(html, content_type="text/html", reason_code="visible_captcha")
        stored = (self.root / reference["artifact_path"]).read_text(encoding="utf-8")
        self.assertNotIn("supersecretvalue", stored)
        self.assertNotIn("guest@example.com", stored)
        self.assertNotIn("TOKENVALUE", stored)
        self.assertIn("card-container", stored)
        self.assertEqual(reference["artifact_format"], "html")

    def test_deeply_nested_secrets_are_reached(self):
        deep = {"a": {"b": {"c": {"d": {"e": {"apiKey": "leaky-key-value"}}}}}}
        reference = self._capture(deep)
        stored = (self.root / reference["artifact_path"]).read_text(encoding="utf-8")
        self.assertNotIn("leaky-key-value", stored)


class BoundsTest(_ArtifactCase):
    def test_oversized_bodies_are_truncated_and_flagged(self):
        os.environ["SCRAPER_ARTIFACT_MAX_BYTES"] = "2048"
        reference = self._capture("x" * 50000, content_type="text/plain")
        self.assertTrue(reference["artifact_truncated"])
        self.assertLessEqual(reference["artifact_stored_bytes"], 2048)
        self.assertGreater(reference["artifact_original_bytes"], 2048)

    def test_per_report_quota_stops_one_report_flooding_the_store(self):
        os.environ["SCRAPER_ARTIFACT_MAX_PER_REPORT"] = "3"
        captured = [self._capture({"i": i}) for i in range(10)]
        self.assertEqual(sum(1 for c in captured if c is not None), 3)
        # A different report still has its own allowance.
        self.assertIsNotNone(self._capture({"i": 0}, report_id="rep-2"))

    def test_retention_removes_artifacts_past_the_age_limit(self):
        os.environ["SCRAPER_ARTIFACT_RETENTION_DAYS"] = "1"
        reference = self._capture({"keep": True})
        stale = self.root / reference["artifact_path"]
        old = time.time() - 3 * 86400
        os.utime(stale, (old, old))
        result = scrape_artifacts.enforce_retention(self.root)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(stale.exists())

    def test_total_byte_quota_evicts_oldest_first(self):
        os.environ["SCRAPER_ARTIFACT_MAX_PER_REPORT"] = "50"
        refs = []
        for i in range(6):
            refs.append(self._capture({"payload": "y" * 500, "i": i}))
            time.sleep(0.01)
        os.environ["SCRAPER_ARTIFACT_MAX_TOTAL_BYTES"] = "1500"
        scrape_artifacts.enforce_retention(self.root)
        surviving = [r for r in refs if (self.root / r["artifact_path"]).exists()]
        self.assertLess(len(surviving), len(refs))
        # Eviction is oldest-first: the newest artifact is the one kept.
        self.assertTrue((self.root / refs[-1]["artifact_path"]).exists())

    def test_undecodable_json_falls_back_to_bounded_raw_with_the_error(self):
        reference = self._capture(
            "<html>not json at all</html>",
            content_type="application/json",
            reason_code="json_decode_failed",
        )
        self.assertIn("artifact_decode_error", reference)
        self.assertIn("application/json", reference["artifact_content_type"])
        stored = (self.root / reference["artifact_path"]).read_text(encoding="utf-8")
        self.assertIn("not json at all", stored)


class DisabledAndFailureTest(_ArtifactCase):
    def test_capture_disabled_returns_none_and_writes_nothing(self):
        os.environ["SCRAPER_ARTIFACT_CAPTURE_ENABLED"] = "0"
        self.assertIsNone(self._capture(BLOCKED_PAYLOAD))
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_successful_payloads_need_an_explicit_opt_in(self):
        """Production must not accumulate a copy of every healthy response."""
        self.assertIsNone(self._capture({"ok": True}, is_error_outcome=False))
        os.environ["SCRAPER_ARTIFACT_CAPTURE_FULL_PAYLOADS"] = "1"
        self.assertIsNotNone(self._capture({"ok": True}, is_error_outcome=False))

    def test_write_failure_is_swallowed_and_reported_as_none(self):
        """A failed capture must not change scraper behaviour."""
        unwritable = self.root / "nope"
        unwritable.write_text("I am a file, not a directory", encoding="utf-8")
        self.assertIsNone(self._capture(BLOCKED_PAYLOAD, root=unwritable))

    def test_no_partial_artifact_is_left_behind_on_a_failed_write(self):
        import worker.core.scrape_artifacts as module

        original = os.replace

        def exploding_replace(src, dst):
            raise OSError("simulated replace failure")

        module.os.replace = exploding_replace
        try:
            self.assertIsNone(self._capture(BLOCKED_PAYLOAD))
        finally:
            module.os.replace = original
        leftovers = [p.name for p in self.root.rglob("*") if p.is_file()]
        self.assertEqual(leftovers, [], f"partial artifacts left behind: {leftovers}")


if __name__ == "__main__":
    unittest.main()
