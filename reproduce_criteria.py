import json
import logging
import sys
import time
from datetime import datetime, timedelta
from worker.scraper.price_estimator import run_criteria_search

def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    logger = logging.getLogger("reproduce_criteria")
    
    checkin_date = datetime.now() + timedelta(days=14)
    checkout_date = checkin_date + timedelta(days=2)
    
    checkin_str = checkin_date.strftime("%Y-%m-%d")
    checkout_str = checkout_date.strftime("%Y-%m-%d")

    logger.info(f"Running criteria search for Austin, TX ({checkin_str} to {checkout_str})")
    
    try:
        daily_results, transparent_result = run_criteria_search(
            address="Austin, TX",
            attributes={"city": "Austin", "state": "TX", "accommodates": 2},
            checkin=checkin_str,
            checkout=checkout_str,
            top_k=5,
            max_scroll_rounds=1,
            max_cards=10,
            max_runtime_seconds=60,
            rate_limit_seconds=0.5,
            cdp_url="http://127.0.0.1:9222"
        )
    except Exception as e:
        logger.exception("Criteria search failed")
        return
        
    print("\n--- RESULTS ---")
    comps = transparent_result.get("comparableListings", [])
    for comp in comps:
        print(f"ID: {comp.get('id')}, Title: {comp.get('title')}, URL: {comp.get('url')}")
        
    print(f"\nTotal comps collected: {transparent_result.get('compsCollected')}")

if __name__ == "__main__":
    main()
