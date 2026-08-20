"""Test package setup.

Importing ``worker.main`` installs the structured event sink, which by default
writes to ``worker/logs/worker.jsonl`` — the file an operator tails during an
incident. Several tests deliberately drive blocked payloads, 503s and circuit
openings, so a test run injects fabricated control events into that log. That
is not a cosmetic problem: a real investigation was slowed down by 108
test-generated ``circuit_opened`` entries that looked like production evidence.

Both runners (``pytest`` and ``unittest discover``) import this package before
any test module, so redirecting here covers both. Tests that need the sink
install it themselves against a temporary path.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("WORKER_EVENT_LOG_ENABLED", "0")
# Same reasoning for diagnostic artifacts: a test run must not deposit files in
# the operator's artifact store. Tests that assert on capture override this with
# a temporary directory of their own.
os.environ.setdefault(
    "SCRAPER_ARTIFACT_DIR", os.path.join(tempfile.gettempdir(), "airahost-test-artifacts")
)
