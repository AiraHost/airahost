"""
Fetch the current StaysPdpSections persisted-query sha256 hash via Playwright.

Airbnb pins each GraphQL query to a `sha256Hash` that changes whenever they ship
a new frontend build. Rather than copy it out of the browser Network tab by
hand, this drives a real Chromium page to a listing and intercepts the live
`StaysPdpSections` request to read the hash (and the api key) it actually uses.

Usage:
    python get_hash.py                 # uses a default listing
    python get_hash.py 47273102        # use a specific listing
    python get_hash.py --headed        # watch the browser
    python get_hash.py --update        # also patch airbnb_crawler.py

Requires: pip install playwright  &&  python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import Request, TimeoutError as PWTimeout, sync_playwright

DEFAULT_LISTING = "47273102"
OPERATION_NAME = "StaysPdpSections"
CRAWLER_FILE = Path(__file__).with_name("airbnb_crawler.py")

# .../api/v3/StaysPdpSections/<64-hex-hash>?...
_HASH_IN_URL = re.compile(rf"/api/v3/{OPERATION_NAME}/([0-9a-f]{{64}})")


def extract_hash(
    listing_id: str = DEFAULT_LISTING,
    headed: bool = False,
    timeout_ms: int = 45_000,
) -> dict[str, object]:
    """Sniff a live PDP load.

    Returns {'hash', 'api_key', 'cookies', 'user_agent'} where `cookies` is a
    list of Playwright cookie dicts. The cookies matter for anti-bot recovery:
    a real browser clears Airbnb's challenge and receives the session cookies
    (bev, _abck, ...) that let subsequent direct requests through.
    """
    url = f"https://www.airbnb.com/rooms/{listing_id}"
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    )
    result: dict[str, object] = {
        "hash": None,
        "api_key": None,
        "cookies": [],
        "user_agent": user_agent,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(user_agent=user_agent, locale="en-US")
        page = context.new_page()

        def on_request(req: Request) -> None:
            if result["hash"]:
                return
            m = _HASH_IN_URL.search(req.url)
            if m:
                result["hash"] = m.group(1)
                result["api_key"] = req.headers.get("x-airbnb-api-key")

        page.on("request", on_request)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # The PDP sections request fires shortly after load. Wait until our
            # handler has captured it (or time out).
            page.wait_for_event(
                "request",
                predicate=lambda r: bool(result["hash"]),
                timeout=timeout_ms,
            )
            # Give any anti-bot challenge a moment to settle, then let the page
            # go idle so the security cookies are fully set.
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PWTimeout:
                pass
        except PWTimeout:
            pass
        finally:
            # Grab cookies before tearing the context down.
            try:
                result["cookies"] = context.cookies()
            except Exception:
                result["cookies"] = []
            context.close()
            browser.close()

    return result


def update_crawler(new_hash: str) -> bool:
    """Patch PERSISTED_QUERY_HASH in airbnb_crawler.py; return True if changed."""
    text = CRAWLER_FILE.read_text(encoding="utf-8")

    # Replace only the value assigned to PERSISTED_QUERY_HASH, not any other
    # 64-hex string that might appear in the file.
    pattern = re.compile(
        r'(PERSISTED_QUERY_HASH\s*=\s*\(\s*\n?\s*")[0-9a-f]{64}(")'
    )
    new_text, n = pattern.subn(rf"\g<1>{new_hash}\g<2>", text)
    if n == 0:
        # Fall back to a single-line assignment form.
        pattern = re.compile(r'(PERSISTED_QUERY_HASH\s*=\s*")[0-9a-f]{64}(")')
        new_text, n = pattern.subn(rf"\g<1>{new_hash}\g<2>", text)

    if n and new_text != text:
        CRAWLER_FILE.write_text(new_text, encoding="utf-8")
        return True
    return False


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Grab the current PDP query hash")
    parser.add_argument("listing", nargs="?", default=DEFAULT_LISTING)
    parser.add_argument("--headed", action="store_true", help="Show the browser")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Patch PERSISTED_QUERY_HASH in airbnb_crawler.py",
    )
    args = parser.parse_args(argv)

    # Accept a URL or bare id.
    listing = args.listing
    if listing.startswith("http"):
        m = re.search(r"/rooms/(\d+)", urlparse(listing).path)
        listing = m.group(1) if m else listing

    print(
        f"Loading listing {listing} to sniff the {OPERATION_NAME} hash...",
        file=sys.stderr,
    )
    found = extract_hash(listing, headed=args.headed)

    if not found["hash"]:
        print(
            "Could not capture the hash (no matching request seen).",
            file=sys.stderr,
        )
        return 1

    print(found["hash"])  # stdout: just the hash, so it's pipeable
    if found["api_key"]:
        print(f"api_key: {found['api_key']}", file=sys.stderr)

    if args.update:
        if update_crawler(found["hash"]):
            print(
                "Updated PERSISTED_QUERY_HASH in airbnb_crawler.py",
                file=sys.stderr,
            )
        else:
            print(
                "Hash unchanged; airbnb_crawler.py left as-is", file=sys.stderr
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
