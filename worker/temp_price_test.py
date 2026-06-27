"""
Diagnostic for listing 1596737613274892756 — 06/25 to 07/02.
Run: python -m worker.temp_price_test
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("diag")

LISTING_URL = "https://www.airbnb.com/rooms/1596737613274892756"
CHECKIN = "2026-06-25"
CHECKOUT = "2026-07-02"
ADULTS = 2
CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222")


def _run():
    from worker.scraper.airbnb_client import AirbnbClient
    from worker.scraper.target_extractor import extract_target_spec
    from worker.scraper.price_estimator import run_scrape

    client = AirbnbClient({"CHECKIN": CHECKIN, "CHECKOUT": CHECKOUT, "ADULTS": ADULTS})

    # ── Stage 1: target spec extraction ────────────────────────────
    logger.info("=" * 60)
    logger.info("STAGE 1: target spec extraction")
    logger.info("=" * 60)
    try:
        spec, warnings = extract_target_spec(client, LISTING_URL)
        logger.info("spec.title=%r", spec.title)
        logger.info("spec.location=%r", spec.location)
        logger.info("spec.city=%r", spec.city)
        logger.info("spec.state=%r", spec.state)
        logger.info("spec.lat=%r  spec.lng=%r", spec.lat, spec.lng)
        logger.info("spec.accommodates=%r", spec.accommodates)
        logger.info("spec.bedrooms=%r", spec.bedrooms)
        logger.info("spec.baths=%r", spec.baths)
        logger.info("spec.property_type=%r", spec.property_type)
        logger.info("warnings=%s", warnings)
        if not spec.location and spec.lat is None:
            logger.error("STAGE 1 FAILED: no location or coords — run_scrape would abort here")
            return
        logger.info("STAGE 1 PASSED — location=%r", spec.location or f"({spec.lat},{spec.lng})")
    except Exception as exc:
        logger.error("STAGE 1 EXCEPTION: %s", exc, exc_info=True)
        return

    # ── Stage 2: full run_scrape ────────────────────────────────────
    logger.info("")
    logger.info("=" * 60)
    logger.info("STAGE 2: run_scrape")
    logger.info("=" * 60)
    try:
        daily_results, transparent = run_scrape(
            listing_url=LISTING_URL,
            checkin=CHECKIN,
            checkout=CHECKOUT,
            adults=ADULTS,
            cdp_url=CDP_URL,
        )
        priced = [r for r in daily_results if r.get("median_price")]
        logger.info(
            "run_scrape complete: total_days=%s priced_days=%s",
            len(daily_results),
            len(priced),
        )
        if priced:
            logger.info("Sample prices: %s", [r["median_price"] for r in priced[:5]])
            logger.info("STAGE 2 PASSED")
        else:
            logger.error("STAGE 2 FAILED: no priced days")
            debug = (transparent or {}).get("debug") or {}
            logger.error("debug.error=%r", debug.get("error"))
            logger.error("debug.extractionWarnings=%s", debug.get("extractionWarnings"))
            logger.error("targetSpec=%s", (transparent or {}).get("targetSpec"))
    except Exception as exc:
        logger.error("STAGE 2 EXCEPTION: %s", exc, exc_info=True)


if __name__ == "__main__":
    _run()
