# Investigation Report: Comparable Listings Optimization

## Executive Summary
The current comparable listings scraping pipeline struggles to meet the requirements of finding at least 20 comps per day with a >50% similarity score in under 45 seconds. The codebase currently experiences a strong tension between accuracy (enforced via exact capacity matching and PDP enrichment) and speed/volume. The `v2` implementation achieves the 45-second target but yields too few comps (max 14/day). The `v3` hybrid approach attempts to fix the volume issue but balloons the runtime to ~108 seconds.

## Task 1: Root Cause Analysis

### 1. Exact Guest Capacity Hard Filter (Major Impact)
- **File**: [`worker/scraper/comp_collection.py`](file:///C:/Users/limue/Documents/Projects/AiraHost/airahost-main/worker/scraper/comp_collection.py#L158-L176) (`_matches_structural_filters`)
- **Issue**: The structural filter requires `comp.accommodates == target_accommodates` exactly. A 6-guest target listing will aggressively exclude all 4, 5, 7, and 8-guest properties.
- **Impact**: The fixed pool size is artificially constrained, resulting in only 14 listings for the test case (short of the 20+ requirement). Many structurally similar listings are excluded before pricing.

### 2. PDP Structural Enrichment Bottleneck (Major Performance Hit)
- **File**: [`worker/scraper/comp_collection.py`](file:///C:/Users/limue/Documents/Projects/AiraHost/airahost-main/worker/scraper/comp_collection.py#L259-L422)
- **Issue**: Before similarity scoring, every candidate requires a PDP detail fetch to extract exact `accommodates`, `baths`, `property_type`, and `amenities`. Search-derived values for these attributes are explicitly discarded (lines 280-283) to avoid false positives. 
- **Impact**: Each PDP fetch requires a network round-trip. For 30 candidates, this can take 15-20 seconds. This is the primary reason why `fixed_comp_pool_ms` takes ~18-19 seconds.

### 3. County-Level Geocoding Fallback (Critical)
- **Files**: [`worker/scraper/comp_collection.py`](file:///C:/Users/limue/Documents/Projects/AiraHost/airahost-main/worker/scraper/comp_collection.py#L466-L476) and [`worker/scraper/day_query.py`](file:///C:/Users/limue/Documents/Projects/AiraHost/airahost-main/worker/scraper/day_query.py#L153-L192)
- **Issue**: When a target listing's lat/lng is unavailable, the system geocodes the location text. In some cases (e.g., "Santa Clara, California"), the geocoder resolves to "Santa Clara County" center instead of the city center. The 5-mile map-bounded search is then miscentered. A guard was added to skip map bounds if a county is detected, but this falls back to slow, unbounded city-wide searches.
- **Impact**: Searches take significantly longer (148+ seconds in v1 after the guard) or return very few comps (1-2 per day in v1 before the guard).

### 4. Daily Search Duplication (Performance Hit)
- **Files**: [`worker/scraper/day_query.py`](file:///C:/Users/limue/Documents/Projects/AiraHost/airahost-main/worker/scraper/day_query.py#L358-L399) and [`worker/scraper/price_estimator.py`](file:///C:/Users/limue/Documents/Projects/AiraHost/airahost-main/worker/scraper/price_estimator.py#L1965-L1998)
- **Issue**: Each day executes BOTH a 1-night AND a 2-night search query. Furthermore, when the fixed pool yields `< MIN_DAILY_COMPS_PER_DAY`, a dynamic day query runs with `pdp_structural_enrichment_limit=12`, repeating search and enrichment.
- **Impact**: The `v3` hybrid mode took 107.84 seconds due to these dynamic fallback queries.

### 5. Fixed Pool Stride and Anchor Limitation (Moderate Impact)
- **File**: [`worker/scraper/price_estimator.py`](file:///C:/Users/limue/Documents/Projects/AiraHost/airahost-main/worker/scraper/price_estimator.py#L1036-L1092) (`_build_fixed_comp_pool_by_stride`)
- **Issue**: The default stride is 7 days, meaning for a 7-day window, only 1 anchor date is used to seed the pool. The pool size is capped by `FIXED_COMP_POOL_GLOBAL_LIMIT` (default 15).
- **Impact**: Combined with exact capacity matching, the pool often yields ~13-14 listings, falling short of the 20-comp requirement and triggering slow dynamic fallbacks.

### 6. Similarity Floor & Feature Weights (Moderate Impact)
- **File**: [`worker/core/similarity.py`](file:///C:/Users/limue/Documents/Projects/AiraHost/airahost-main/worker/core/similarity.py#L21-L25)
- **Issue**: The `_LOCATION_SIMILARITY_WEIGHT` = 5.0 is a strict binary match. If city names don't match exactly, the location contributes 0.0 to the score, making it difficult to pass the `SIMILARITY_FLOOR` of 0.40. Amenity scoring uses premium weights (e.g., beach_access=12.0) which can cause large score swings.
- **Impact**: Listings in the same geographical area but with slight text variations in location are heavily penalized.

### Metrics from Historical Runs

| Metric | v1 (Before Guard) | v1 (After Guard) | v2 | v3 Hybrid |
|---|---|---|---|---|
| Elapsed (s) | 50.85 | 148.79 | 33.43 | 107.84 |
| Fixed Pool Size | N/A | N/A | 14 | 13 |
| Min Day Visible | 2 | 20 | 0 | 4 |
| Min Day >50% Sim | 1 | 12 | 0 | 4 |
| Under 45s? | No | No | Yes | No |
| 20+ comps/day? | No | Partial | No (14 max) | No |


## Task 3: Proposed Solutions

Based on the investigation, here are the possible solutions to achieve >20 comps/day with >50% similarity in under 45 seconds:

### Solution 1: "Loose Pool, Strict Pricing" Strategy (Recommended)
1. **Relax Exact Capacity Matching for Pool Assembly**: Modify `_matches_structural_filters` to allow a tolerance for `accommodates` (e.g., ±2) during the initial pool gathering to dramatically increase candidate volume.
2. **Asynchronous/Batched PDP Enrichment**: Instead of blocking on PDP fetches for every candidate sequentially or in small batches, collect a large pool of structurally similar candidates from search and fetch their PDP details concurrently using a highly optimized, batched approach.
3. **Price Refreshes Only**: Build the high-similarity reusable comp pool *once* from 1-2 anchor searches. For the daily queries, skip city-wide searches entirely and only fetch the specific prices for the listings in the reusable pool.

### Solution 2: Trust Search Signals with Heuristics
1. **Eliminate PDP Enrichment Bottleneck**: Stop discarding search-derived `accommodates`, `baths`, and `property_type`. Trust the search card data.
2. **Heuristic Deduplication**: To handle the issue where search cards report request-level guest counts instead of true capacity, use the candidate's historical observations or ML heuristics (if available) to correct the search card capacity.
3. **Similarity Re-weighting**: Reduce the strictness of the location binary match to prevent geographically close comps from being penalized.

### Solution 3: Multi-Anchor Fixed Pool Expansion
1. **Increase Anchors**: Change the `FIXED_COMP_POOL_STRIDE_DAYS` to 2 or 3 to seed the pool from multiple dates within the 7-day window.
2. **Increase Pool Limit**: Raise `FIXED_COMP_POOL_GLOBAL_LIMIT` from 15 to 30 or 40.
3. **Concurrent Processing**: Execute the anchor searches and PDP enrichments completely in parallel before starting the day-by-day price fetching.
