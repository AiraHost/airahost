# Implementation Prompt: Scraping Optimization for Comparable Listings

**Context:**
Currently, our comparable listings scraping methodology yields very few listings (specifically, those with a similarity score above 50%) per day. We need to investigate this issue, implement comprehensive logging to identify the bottleneck, and propose highly performant solutions to ensure a sufficient number of comparable listings are gathered.

**Your Tasks:**

### Task 1: Investigate the Current Scraping Methodology
- Analyze the current implementation for scraping comparable listings and calculating similarity scores.
- Identify and document all possible causes for why very few comparable listings (with > 50% similarity score) are being scraped per day. 
- *Hints to check:* Search radius limits, pagination limits, API rate limiting, overly strict filtering criteria applied before similarity calculation, or potential bugs/edge cases in the similarity scoring logic itself.

### Task 2: Implement Diagnostic Logging & Reproduce the Issue
- Implement detailed logging within the scraping and similarity calculation pipeline. These logs should track:
  - Total properties fetched initially from the source.
  - Number of properties filtered out at each processing stage.
  - The calculated similarity scores for properties (especially those failing the 50% threshold).
  - Execution time for each major step of the scraping process.
- **Reproduction:** Using the newly implemented logs, run a 7-day scraping test for the room with **ID `1124054679241449795`**. 
- Document the test results and pinpoint the exact step where potential comparable listings are being dropped.

### Task 3: Propose Solutions
Based on your investigation, list out all possible solutions that strictly satisfy the following requirements:
1. **Quantity & Quality:** At least **20 comparable listings** with a **> 50% similarity score** must be scraped for *every day*. Listings can be reused across multiple days as long as they have an available price for that specific day (i.e., ensure >20 listings are available each day).
2. **Performance:** The total execution time for a 7-day scraping run must be **under 45 seconds**. Brute force approaches (like scraping an entire city's listings on every run) are strictly prohibited.
3. **Data Integrity:** All necessary property details (amenities, reviews, ratings, etc.) must still be successfully scraped and preserved after your modifications.

### Task 4: Refactoring Authorization
- You are fully authorized to rewrite the entire price and comparable listings scraping methodology to achieve these goals.
- *Recommendation:* If rewriting, consider efficient strategies such as fetching a pool of comparables once and then only fetching their prices for the specific 7 days, using batched API requests, or localized geographic searches.
