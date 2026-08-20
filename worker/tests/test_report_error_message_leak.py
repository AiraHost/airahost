"""
Internal scrape failures must never become the user-facing report error.

A production incident put the raw text "PDP individual listing extraction
failed (payload + rendered DOM): <url>" into pricing_reports.error_message,
because main.py caught every ValueError from the scrape stack and wrote
str(exc) straight through as the user-facing message. Only genuine input
problems are allowed to speak for themselves; everything else must degrade to
a safe message with the detail kept in result_core_debug.

These tests pin the contract that split makes: the exception *type* — not the
message text — decides what the user sees.
"""

import unittest

from worker.core.errors import ReportInputError
from worker.scraper.target_extractor import extract_target_spec


class ReportInputErrorContractTest(unittest.TestCase):
    def test_report_input_error_is_a_value_error(self):
        # Callers that predate the split still catch ValueError; narrowing the
        # type must not silently stop them from handling input errors.
        self.assertTrue(issubclass(ReportInputError, ValueError))

    def test_over_long_date_range_stays_user_facing(self):
        # The date-range limit is the one failure the user can actually act on,
        # so its message is meant to reach them verbatim.
        from worker.scraper.price_estimator import MAX_NIGHTS, run_scrape

        with self.assertRaises(ReportInputError) as ctx:
            run_scrape(
                listing_url="https://www.airbnb.com/rooms/998072976335840743",
                checkin="2026-01-01",
                checkout="2027-01-01",
            )
        self.assertIn(str(MAX_NIGHTS), str(ctx.exception))

    def test_unusable_pdp_does_not_raise_a_user_facing_error(self):
        # The incident path: every PDP method fails. Extraction must degrade to
        # an empty spec plus warnings so run_scrape's own fallbacks still get a
        # turn — raising here both skipped them and leaked internal text.
        class _DeadClient:
            config = {}

            def get_listing_details(self, *args, **kwargs):
                raise RuntimeError("anti-bot challenge")

        spec, warnings = extract_target_spec(
            _DeadClient(),
            "https://www.airbnb.com/rooms/998072976335840743",
            fail_on_unusable=True,
        )
        self.assertEqual(spec.accommodates, None)
        self.assertTrue(warnings, "extraction failure must be reported as warnings")


if __name__ == "__main__":
    unittest.main()
