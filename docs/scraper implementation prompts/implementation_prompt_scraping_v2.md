# Implementation Prompt: Scraping Optimization for Comparable Listings (v2)

**Context:**
Building upon the initial investigation, we need to implement specific bug fixes and optimization strategies. The absolute highest priority is execution speed, while ensuring strict accuracy in guest counts and resolving pricing data anomalies.

**Your Tasks:**

### Task 1: Prioritize Performance & Speed Fix (Highest Priority)
- **Absolute Requirement:** You must optimize the scraping methodology so that a full 7-day scraping run completes in **strictly under 45 seconds**. 
- Refactor the code aggressively if necessary to achieve this performance boundary. Do not use brute force. Caching, batched API requests, or eliminating redundant iterations should be your primary focus.

### Task 2: Strict Guest Count Matching
- Ensure that comparable listings have the exact same capacity as the target property.
- **Requirement:** The number of guests for comparable listings **must exactly match**. You cannot include comparable listings that accommodate a different number of guests. Update the initial filtering logic to enforce this before similarity scoring occurs.

### Task 3: Bug Fix - Ghost Pricing
- **Issue:** Room ID **`797454009048233847`** currently shows a scraped price for **July 2nd**, but it should have *no price* (unavailable) on that date.
- **Action:** Investigate why a price is being incorrectly scraped or hallucinated for this unavailable date. Implement a fix to ensure unavailable dates are correctly identified and return no price data. Add a test case to prevent regressions on this.

### Task 4: Verify Previous Requirements
Ensure that the original baseline requirements are still met alongside these new constraints:
- At least **20 comparable listings** with a **> 50% similarity score** must be scraped for *every day* (listings can be reused if they are available).
- All necessary property details (amenities, reviews, ratings, etc.) must still be successfully scraped and preserved.
- You remain fully authorized to rewrite the entire price and comparable listings scraping methodology to meet these strict speed and accuracy requirements.
