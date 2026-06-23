# Implementation Prompt: Scraping Optimization for Comparable Listings (v4)

**Context:**
The v3 hybrid methodology successfully fixed the ghost pricing bugs and maintained capacity limits, but failed the <45s runtime threshold (taking 107s). Furthermore, the initial fixed pool only found 13 listings instead of 15, and the dynamic fallback failed to find the 10 minimum daily comps. 

The core bottleneck is the sequential fetching of Property Detail Pages (PDP) to verify exact guest capacities, combined with a limited geographic pool. 

To resolve the volume and performance issues while strictly maintaining exact capacity matching, implement the following v4 requirements.

**Your Tasks:**

### Task 1: Pagination & Geographic Expansion for Initial Comps
The baseline fixed pool failed to find 15 listings. To fix this volume issue:
1. **Search Pagination:** Verify if the scraper is paginating and checking *multiple pages* of search results during the initial comparable listing gathering. If it is only checking the first page, implement multi-page parsing.
2. **Geo-Expansion:** If it is already checking multiple pages (or once multi-page checking is implemented) and it *still* yields fewer than 15 exact-capacity comps, you must automatically widen the geographic restriction by **+2 km in diameter** to find the remaining matches. 

### Task 2: Advanced Performance Optimizations (<45s)
You must bring the overall runtime strictly under the 45s limit by optimizing the PDP capacity verification process:
1. **Capacity Caching (Fast Path):** Implement a persistent database cache (or robust global local cache) mapping `listing_id -> capacity`. Before fetching a PDP to check guest capacity, query this cache. Never fetch the PDP for capacity validation if the listing ID has already been verified previously.
2. **Concurrent PDP Validation:** Do not validate fallback dynamic candidates sequentially. You must use `asyncio.gather` (or a thread pool) to fetch and validate the capacities of multiple candidate PDPs concurrently.

### Task 3: Pre-filter with Search Parameters
- To reduce the number of invalid PDP pages you have to check, ensure the search/fallback query explicitly includes `adults={target_capacity}` (or the equivalent guest parameter). While this may still return larger properties, it immediately filters out properties with *less* than the target capacity.

### Task 4: Strict Capacity Enforcement (Non-Negotiable)
- **Crucial:** The number of guests for all comparable listings must still be the **exact same** as the user-owned listing. Despite the pagination, caching, and geographic expansion, you cannot relax the capacity matching rule under any circumstances.

### Task 5: Maintain Baseline Rules
- **Fixed Pool:** Target 15 fixed baseline listings initially.
- **Daily Minimum:** Ensure a strict minimum of 10 priced comparable listings per day using the dynamic day-query fallback.
- **Ghost Pricing:** Ensure the previous fix for unavailable dates/ghost pricing is maintained.
- **Execution Logging:** Ensure the start time, end time, and total time elapsed log is still correctly printed upon completion.
