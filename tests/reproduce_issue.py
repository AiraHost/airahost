import json
import logging
import sys
import time
from datetime import datetime, timedelta
from worker.scraper.price_estimator import run_scrape

def setup_logging():
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('reproduce_issue_debug.log', mode='w')
        ]
    )

def main():
    setup_logging()
    logger = logging.getLogger("tests.reproduce_issue")
    logger.info("Starting reproduction test")

    listing_id = "1124054679241449795"
    url = f"https://www.airbnb.com/rooms/{listing_id}"
    
    # Use tomorrow's date
    checkin_date = datetime.now() + timedelta(days=1)
    checkout_date = checkin_date + timedelta(days=7)
    
    checkin_str = checkin_date.strftime("%Y-%m-%d")
    checkout_str = checkout_date.strftime("%Y-%m-%d")

    logger.info(f"Test window: {checkin_str} to {checkout_str}")

    start_time = time.time()
    
    # We may need a dummy client if real Chrome is not running, but run_scrape 
    # handles CDP connections. If it fails, we'll see it.
    try:
        daily_results, transparent_result = run_scrape(
            listing_url=url,
            checkin=checkin_str,
            checkout=checkout_str,
            cdp_url="http://127.0.0.1:9222",
            adults=2,
            top_k=20,
            max_scroll_rounds=2,
            max_cards=30,
            max_runtime_seconds=300
        )
    except Exception as e:
        logger.exception("run_scrape failed")
        return

    elapsed_seconds = time.time() - start_time
    
    # Compute stats
    per_day_visible = {}
    per_day_gt_50 = {}
    
    comps = transparent_result.get("comparableListings", [])
    
    # The transparent_result contains comparableListings which has priceByDate
    for comp in comps:
        sim = comp.get("similarity", 0)
        prices = comp.get("priceByDate", {})
        
        for date_str, price in prices.items():
            if price and price > 0:
                per_day_visible[date_str] = per_day_visible.get(date_str, 0) + 1
                if sim > 0.50:
                    per_day_gt_50[date_str] = per_day_gt_50.get(date_str, 0) + 1

    summary = {
        "elapsed_seconds": round(elapsed_seconds, 2),
        "per_day_visible": per_day_visible,
        "per_day_gt_50": per_day_gt_50,
        "timingsMs": transparent_result.get("timingsMs", {}),
        "fixedCompPoolSize": transparent_result.get("fixedCompPoolSize", 0)
    }
    
    print(json.dumps(summary, indent=2))

    # Validations
    print("\n--- VALIDATION ---")
    
    # Criterion 1: At least 20 comps per day with >50% similarity
    all_days_gt_20 = True
    for i in range(7):
        d_str = (checkin_date + timedelta(days=i)).strftime("%Y-%m-%d")
        count = per_day_gt_50.get(d_str, 0)
        if count < 20:
            all_days_gt_20 = False
            break
            
    crit1 = "PASS" if all_days_gt_20 else "FAIL"
    print(f"Criterion 1 (20+ comps/day with >50% similarity): {crit1}")
    
    # Criterion 2: Total runtime under 45 seconds
    crit2 = "PASS" if elapsed_seconds < 45 else "FAIL"
    print(f"Criterion 2 (Total runtime under 45 seconds): {crit2} ({round(elapsed_seconds, 2)}s)")
    
    # Criterion 3: All comp details preserved (rough check)
    has_details = True
    for comp in comps:
        if "rating" not in comp and "amenities" not in comp:
            has_details = False
            break
            
    crit3 = "PASS" if has_details and len(comps) > 0 else "FAIL"
    print(f"Criterion 3 (All comp details preserved): {crit3}")

if __name__ == "__main__":
    main()
