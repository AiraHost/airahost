# Scraping optimization investigation

- Status: partially fixed; further work required
- Date: 2026-06-22
- Listing tested: `1124054679241449795`
- Window tested: `2026-06-23` through `2026-06-30`

## Success criteria

- At least 20 comparable listings per day.
- At least 20 comparable listings per day with similarity greater than 0.50.
- Seven-night scrape completes in less than 45 seconds.
- Comparable details remain available for visible comps.

## Changes made

- Added funnel diagnostics in `collect_search_comps()`:
  - page ids, context rows, priced rows, offsets, query nights, and elapsed time
  - self exclusions, structural exclusions, unavailable rows, minimum-stay blocks, no-price rows, and final priced rows
- Added day-level diagnostics in `estimate_base_price_for_date()`:
  - structural filter stage and counts
  - similarity buckets for `>=0.75`, `>=0.50`, `>=SIMILARITY_FLOOR`, and below floor
  - elapsed time for each day query
- Added fixed-pool scoring diagnostics.
- Restored `priceByDateDetails` for top comparable listings.
- Fixed decimal trip-total normalization so API totals like `1110.36` are ceiled to Airbnb's displayed whole-dollar total before deriving nightly price.
- Added a guard that skips bounded map search when fallback geocoding resolves a city query to a county-level result.

## Reproduction results

### Before county-geocode guard

Command path: direct `run_scrape()` with local CDP, `max_scroll_rounds=2`, `max_cards=30`, `rate_limit_seconds=0`.

Result:

```json
{
  "elapsed_seconds": 50.85,
  "per_day_visible": {
    "2026-06-23": 2,
    "2026-06-24": 2,
    "2026-06-25": 2,
    "2026-06-26": 2,
    "2026-06-27": 2,
    "2026-06-28": 2,
    "2026-06-29": 2
  },
  "per_day_gt_50": {
    "2026-06-23": 1,
    "2026-06-24": 1,
    "2026-06-25": 1,
    "2026-06-26": 1,
    "2026-06-27": 1,
    "2026-06-28": 1,
    "2026-06-29": 1
  },
  "timingsMs": {
    "day_queries_ms": 26542,
    "fixed_comp_pool_ms": 1356,
    "total_ms": 47557
  }
}
```

Pinpointed drop:

- `Santa Clara, California` geocoded to `Santa Clara County` at `37.23333,-121.68463`.
- The 5-mile map-bounded search was centered on that county-level point.
- One-night searches returned zero listing ids.
- Two-night searches returned two listing ids.
- Structural filtering retained one listing per day.

### After county-geocode guard

Result:

```json
{
  "elapsed_seconds": 148.79,
  "per_day_visible": {
    "2026-06-23": 27,
    "2026-06-24": 22,
    "2026-06-25": 20,
    "2026-06-26": 20,
    "2026-06-27": 20,
    "2026-06-28": 20,
    "2026-06-29": 20
  },
  "per_day_gt_50": {
    "2026-06-23": 24,
    "2026-06-24": 18,
    "2026-06-25": 12,
    "2026-06-26": 20,
    "2026-06-27": 20,
    "2026-06-28": 20,
    "2026-06-29": 20
  },
  "timingsMs": {
    "day_queries_ms": 111870,
    "fixed_comp_pool_ms": 2184,
    "total_ms": 137264
  }
}
```

The guard fixes the bad map-center bottleneck and reaches 20 visible comps per day, but it does not fully satisfy the prompt because two days still have fewer than 20 comps above 0.50 and the unbounded daily searches are too slow.

## Remaining bottlenecks

- The current pipeline still performs live daily searches for every date.
- When map bounds are skipped, source inventory improves, but unbounded text searches are much slower.
- The fixed pool is built from a single anchor date for a seven-day window with the default stride of 7 days, so it may not contain enough high-similarity priced comps for every day.
- Target PDP price capture and post-run comparable repair add runtime after day queries; this listing returned no target price for the tested dates, causing slow 1-night and 2-night retries.

## v2 implementation results

Changes:

- Enforced exact guest capacity before similarity scoring.
- Stopped using request-level guest count as a search-card `accommodates` fallback.
- Added PDP structural enrichment for exact capacity, with a room-level structural cache.
- Added a fast detail path that uses DeepBnb details when available and avoids warmed Playwright pools for comparable structural enrichment.
- Added a fixed-pool fast path: build exact-capacity structure once, then daily searches price only known-good room IDs.
- Disabled default target-price capture and comparable PDP repair on the fast path.
- Fixed unavailable PDP booking sections so explicit `available: false` cannot surface stale prices.

Measured live run after v2:

```json
{
  "elapsed_seconds": 33.43,
  "target_accommodates": 6,
  "fixedCompPoolSize": 14,
  "capacity_mismatch_count": 0,
  "per_day_visible": {
    "2026-06-23": 14,
    "2026-06-24": 14,
    "2026-06-25": 0,
    "2026-06-26": 14,
    "2026-06-27": 14,
    "2026-06-28": 14,
    "2026-06-29": 14
  },
  "per_day_gt_50": {
    "2026-06-23": 14,
    "2026-06-24": 14,
    "2026-06-25": 0,
    "2026-06-26": 14,
    "2026-06-27": 14,
    "2026-06-28": 14,
    "2026-06-29": 14
  },
  "timingsMs": {
    "extract_ms": 1344,
    "fixed_comp_pool_ms": 19321,
    "day_queries_ms": 12746,
    "total_ms": 33418
  }
}
```

Status against v2 prompt:

- Pass: seven-night runtime is strictly under 45 seconds.
- Pass: capacity mismatches are zero in the measured run.
- Pass: unavailable PDP sections with stale prices now return no price; regression coverage includes room `797454009048233847`.
- Fail: the live exact-capacity pool for this listing/date window contains 14 comparable listings, not 20. A second fixed-pool offset page did not increase the pool size.

The remaining conflict is between strict exact-capacity matching and the 20-comps/day display requirement for this live search result set. Relaxing exact capacity would restore count, but that would violate v2 task 2. Reusing fixed-pool prices for dates where search did not return those listings would improve display count, but risks reintroducing unavailable-date price leakage.

## v3 hybrid implementation results

Changes:

- Changed the default daily coverage target from 20 to 10 and added a fixed baseline target of 15.
- Replaced fixed-pool stale price fill with a hybrid fixed-then-dynamic path.
- The fixed pool is used first for date-specific prices only.
- If a day yields fewer than 10 strict priced comps from the fixed pool, a dynamic day query runs for that date.
- Dynamic fallback keeps exact capacity matching and disables relaxed similarity fallback, so returned comps must remain above 0.50 similarity.
- DeepBnb comparable search/detail failures now fail closed on the fast path rather than falling back to browser work.
- Fixed `PlaywrightScraper.fork()` so browser fallback clones preserve locale/currency state.

Measured live run after v3:

```json
{
  "elapsed_seconds": 107.84,
  "target_accommodates": 6,
  "fixedCompBaselineTarget": 15,
  "fixedCompPoolSize": 13,
  "dailyCompCoverageTarget": 10,
  "dynamic_days": [
    "2026-06-24",
    "2026-06-25",
    "2026-06-26",
    "2026-06-27",
    "2026-06-28",
    "2026-06-29"
  ],
  "capacity_mismatch_count": 0,
  "per_day_visible": {
    "2026-06-23": 10,
    "2026-06-24": 4,
    "2026-06-25": 5,
    "2026-06-26": 7,
    "2026-06-27": 6,
    "2026-06-28": 7,
    "2026-06-29": 10
  },
  "per_day_gt_50": {
    "2026-06-23": 10,
    "2026-06-24": 4,
    "2026-06-25": 5,
    "2026-06-26": 7,
    "2026-06-27": 6,
    "2026-06-28": 7,
    "2026-06-29": 10
  },
  "timingsMs": {
    "extract_ms": 1414,
    "fixed_comp_pool_ms": 18585,
    "day_queries_ms": 87818,
    "total_ms": 107826
  }
}
```

Status against v3 prompt:

- Pass: hybrid fixed-then-dynamic methodology is implemented.
- Pass: no stale fixed-pool prices are injected across unavailable dates.
- Pass: capacity mismatches are zero in the measured run.
- Fail: fixed baseline produced 13 strict comps, not 15.
- Fail: dynamic fallback did not guarantee 10 priced strict comps for every day.
- Fail: seven-night runtime was 107.84 seconds, above the 45-second target.

The remaining blocker is not report assembly; it is source inventory plus the cost of proving exact capacity for dynamic candidates. A faster implementation would need a cheaper authoritative capacity source in the search response or a batched PDP/detail endpoint. Without that, each dynamic candidate still requires detail validation, and failing closed preserves correctness but reduces yield.

## Proposed next changes

1. Build the comparable identity pool once from 2-3 bounded anchor searches, but query exact-date prices only for that pool.
2. Require the reusable display pool to prefer `similarity > 0.50`; only fall back below 0.50 for pricing continuity with a low-confidence flag.
3. Replace unbounded city search after county-level geocode with a better city centroid resolver, so searches stay localized without using the wrong county center.
4. Skip target price capture when all day-level market prices are valid and target PDP has already signaled unavailable for the requested window.
5. Run fixed-pool anchor searches concurrently and cap per-day price refreshes to the selected pool, not fresh city-wide search pages.

The performant target design is: build a high-similarity reusable comp pool once, then refresh per-date availability/prices for that pool in parallel.
