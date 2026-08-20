"""
Workload tester for the Airbnb PDP crawler.

Hammers a configured list of listings with PDP requests (round-robin) until it
sees N *consecutive* anti-bot responses, then prints a report of how long /
how many requests it took to get there.

This deliberately runs with auto-recovery OFF -- the whole point is to measure
when Airbnb's anti-bot wall kicks in, so we must NOT clear it via Playwright.

Usage:
    python workload_tester.py --listings listings.txt
    python workload_tester.py --listings listings.txt --threshold 3 --delay 0.5
    python workload_tester.py 47273102 20669368 --threshold 3
    python workload_tester.py --listings listings.txt --report report.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from airbnb_crawler import (
    AirbnbPdpClient,
    AntiBotError,
    StaleHashError,
    parse_listing_id,
)

# Outcome labels for each request.
OK = "ok"
ANTIBOT = "antibot"
STALE = "stale"
ERROR = "error"


@dataclass
class Attempt:
    index: int
    listing_id: str
    outcome: str
    elapsed: float
    detail: Optional[str] = None


@dataclass
class Report:
    threshold: int
    listings: list[str]
    reached_streak: bool = False
    total_requests: int = 0
    ok_count: int = 0
    antibot_count: int = 0
    stale_count: int = 0
    error_count: int = 0
    requests_to_streak: Optional[int] = None
    time_to_streak_s: Optional[float] = None
    total_time_s: float = 0.0
    first_antibot_at: Optional[int] = None
    attempts: list[Attempt] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["attempts"] = [a.__dict__ for a in self.attempts]
        return d


def load_listings(path: str) -> list[str]:
    """Read listing ids/URLs from a file (one per line; # comments allowed)."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(parse_listing_id(line))
    return out


def run(
    listings: list[str],
    threshold: int = 3,
    delay: float = 0.5,
    max_requests: Optional[int] = None,
    verbose: bool = True,
) -> Report:
    """Fire requests round-robin until `threshold` consecutive anti-bots."""
    client = AirbnbPdpClient()
    report = Report(threshold=threshold, listings=list(listings))
    consecutive = 0
    streak_started_at: Optional[int] = None  # request index streak began
    t0 = time.monotonic()

    cycle = itertools.cycle(listings)
    idx = 0
    count = 0
    while count <= 100:
        count += 1
        idx += 1
        listing_id = next(cycle)

        req_start = time.monotonic()
        outcome, detail = _one_request(client, listing_id)
        elapsed = time.monotonic() - req_start

        report.total_requests += 1
        report.attempts.append(
            Attempt(idx, listing_id, outcome, round(elapsed, 3), detail)
        )

        if outcome == ANTIBOT:
            report.antibot_count += 1
            if consecutive == 0:
                streak_started_at = idx
            consecutive += 1
            if report.first_antibot_at is None:
                report.first_antibot_at = idx
        else:
            # Any non-antibot outcome breaks the consecutive streak.
            consecutive = 0
            streak_started_at = None
            if outcome == OK:
                report.ok_count += 1
            elif outcome == STALE:
                report.stale_count += 1
            else:
                report.error_count += 1

        if verbose:
            marker = {OK: "OK ", ANTIBOT: "BOT", STALE: "STALE", ERROR: "ERR"}[
                outcome
            ]
            streak = f" (streak {consecutive})" if consecutive else ""
            line = f"[{idx:>4}] {marker} {listing_id}{streak}"
            if detail and outcome != OK:
                line += f" - {detail}"
            print(line, file=sys.stderr)

        if consecutive >= threshold:
            report.reached_streak = True
            # The streak's first request is where the wall effectively began.
            report.requests_to_streak = streak_started_at
            report.time_to_streak_s = round(time.monotonic() - t0, 3)
            break

        if max_requests is not None and idx >= max_requests:
            break

        if delay:
            time.sleep(delay)

    report.total_time_s = round(time.monotonic() - t0, 3)
    print("finished 100 counts")
    return report


def _one_request(
    client: AirbnbPdpClient, listing_id: str
) -> tuple[str, Optional[str]]:
    """Make a single PDP request; classify the outcome. No recovery."""
    try:
        client.fetch(listing_id)
        return OK, None
    except AntiBotError as exc:
        return ANTIBOT, str(exc)
    except StaleHashError as exc:
        return STALE, str(exc)
    except requests.HTTPError as exc:
        return ERROR, f"HTTP {getattr(exc.response, 'status_code', '?')}"
    except (RuntimeError, ValueError, requests.RequestException) as exc:
        return ERROR, str(exc)[:120]


def print_summary(report: Report) -> None:
    print("\n" + "=" * 52)
    print("WORKLOAD TEST REPORT")
    print("=" * 52)
    print(f"Listings tested        : {len(report.listings)}")
    print(f"Anti-bot streak target : {report.threshold} consecutive")
    print(f"Total requests sent    : {report.total_requests}")
    print(f"  successful           : {report.ok_count}")
    print(f"  anti-bot             : {report.antibot_count}")
    print(f"  stale-hash           : {report.stale_count}")
    print(f"  other errors         : {report.error_count}")
    print(f"Total time             : {report.total_time_s}s")
    if report.first_antibot_at is not None:
        print(f"First anti-bot at req  : #{report.first_antibot_at}")
    print("-" * 52)
    if report.reached_streak:
        print(
            f"ANSWER: took {report.total_requests} requests "
            f"({report.time_to_streak_s}s) to reach {report.threshold} "
            f"consecutive anti-bots."
        )
        print(
            f"  (the winning streak began at request "
            f"#{report.requests_to_streak})"
        )
    else:
        print(
            f"Did NOT reach {report.threshold} consecutive anti-bots "
            f"within {report.total_requests} requests."
        )
    print("=" * 52)


def main(argv: Optional[list[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Airbnb PDP workload tester")
    parser.add_argument(
        "ids", nargs="*", help="Listing ids/URLs (or use --listings)"
    )
    parser.add_argument(
        "--listings", help="File with one listing id/URL per line"
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=3,
        help="Stop after this many consecutive anti-bot responses (default 3)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait between requests (default 0.5)",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="Safety cap; stop after this many requests even without a streak",
    )
    parser.add_argument("--report", help="Write the full JSON report to a file")
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress per-request logging"
    )
    args = parser.parse_args(argv)

    listings: list[str] = []
    if args.listings:
        listings.extend(load_listings(args.listings))
    listings.extend(parse_listing_id(x) for x in args.ids)
    if not listings:
        parser.error("Provide listings via --listings FILE or positional ids.")

    report = run(
        listings,
        threshold=args.threshold,
        delay=args.delay,
        max_requests=args.max_requests,
        verbose=not args.quiet,
    )

    print_summary(report)

    if args.report:
        Path(args.report).write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8"
        )
        print(f"\nWrote {args.report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
