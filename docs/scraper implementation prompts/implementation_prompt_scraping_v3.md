# Implementation Prompt: Scraping Optimization for Comparable Listings (v3)

**Context:**
The previous implementation successfully met the performance boundary (<45s), enforced strict guest capacity matching, and fixed the ghost pricing bug. However, enforcing a static pool of reusable listings that must have available pricing every single day proved too restrictive—resulting in only a 14-listing pool, and dropping to 0 available comps on certain days.

To ensure consistent data volume while maintaining speed and strict filtering, we need to implement a **hybrid "Fixed + Dynamic" scraping methodology**.

**Your Tasks:**

### Task 1: Hybrid Compset Methodology
You must refactor the methodology to use a two-step approach:
1. **Fixed Compset (Baseline):** First, establish a fixed pool of **15 comparable listings** (all must match the target's capacity exactly and have a >50% similarity score). Attempt to parse prices for this fixed compset across the entire 7-day period.
2. **Dynamic Day Query (Fallback):** Evaluate the yield for each day. For any specific day where the fixed compset yields **fewer than 10 available prices**, you must trigger a day-specific query. This dynamic day query must fetch additional comparable listings just for that specific day to guarantee a **minimum of 10 priced comparable listings per day**.

### Task 2: Strict Capacity & Similarity Integrity
- The strict guest matching logic must be maintained entirely. Every comparable listing—whether sourced from the baseline Fixed Compset or a Dynamic Day Query—must accommodate the **exact same number of guests** as the target property.
- All listings must continue to pass the >50% similarity threshold.

### Task 3: Defend Against Ghost Pricing
- Continue to ensure that unavailable dates are handled accurately. Do not reuse or inject stale prices from other dates if a property is booked. 
- If a listing in the fixed compset has no price for Day X, it simply fails to count toward Day X's total (which is exactly what should trigger the fallback day query if the count drops below 10). 

### Task 4: Maintain the Performance Boundary
- The absolute execution time for the entire 7-day hybrid run (including any triggered dynamic day queries) must remain **under 45 seconds**.
- Ensure the dynamic day query is highly optimized so it executes quickly when invoked.

### Task 5: Data Integrity
- All necessary property details (amenities, reviews, ratings, etc.) must still be successfully scraped and preserved for all listings gathered, regardless of whether they came from the fixed pool or the dynamic fallback query.

### Task 6: Execution Time Logging
- Implement a final log that prints once the entire analysis and report generation is completely finished.
- This log must explicitly record and output:
  - The absolute **starting time** of the run.
  - The absolute **finishing time** of the run.
  - The **total time used** (in seconds) to easily verify the <45s requirement.
