# Daily comp coverage fixed-pool reuse

- Status: fixed
- Date recorded: 2026-06-20
- Area: `worker/scraper`
- Goal: expose at least 20 comparable listings per sampled day when the fresh
  daily scrape plus reusable fixed pool has enough candidates.

## Problem

Daily search could scrape enough raw priced cards, but the final
`comparableListings` output often showed only a small subset per date, such as
4 comps per day and 7 total visible comps.

Root cause:

- `DayResult.comp_prices` contained all priced daily cards.
- The final report only had full metadata for `top_comps`.
- Non-top `comp_prices` were only hydrated into `comparableListings` when a
  `fixed_comp_pool` entry existed.
- The standard daily path was not building or passing a fixed comp pool, so
  many valid daily prices were dropped from the final visible comps list.

## Reproduction

Unit-level reproduction:

1. Create sampled daily results with 4 `top_comps` and 4 `comp_prices`.
2. Create a reusable fixed pool with at least 20 listings and anchor prices.
3. Build the transparent result.
4. Count comparable listings with `priceByDate[date]`.

Before this fix, each date only exposed the 4 top comps. After this fix, each
date is filled to at least 20 priced comparable rows when the fixed pool has
enough reusable entries.

E2E-style reproduction:

```bash
python -m pytest worker/tests/test_daily_comp_coverage_e2e.py -q
```

## Fix

- Build a reusable fixed comp pool before the per-day scrape loop in
  `run_scrape()`.
- Keep the fixed pool fast:
  - search only bounded anchor dates using `FIXED_COMP_POOL_STRIDE_DAYS`
  - do not run PDP structural enrichment for fixed-pool setup
  - reuse search-card metadata and anchor nightly prices
- During transparent result assembly, if a sampled date has fewer than
  `MIN_DAILY_COMPS_PER_DAY` priced comps, fill missing daily comp prices from
  the fixed pool.
- Mark reused prices in `priceByDateDetails` with:

```json
{
  "source": "fixed_pool_reuse",
  "reused": true
}
```

- Skip `fixed_pool_reuse` rows when writing `market_comp_observations`, so
  reused display prices are not stored as fresh live observations.

## Configuration

- `MIN_DAILY_COMPS_PER_DAY`: default `20`
- `FIXED_COMP_POOL_STRIDE_DAYS`: default `7`
- `FIXED_COMP_POOL_GLOBAL_LIMIT`: default `30`

## Verification

Passed:

```bash
python -m pytest worker/tests/test_price_by_date_aggregation.py::test_fixed_pool_reuse_fills_each_sampled_day_to_twenty_comps -q
python -m pytest worker/tests/test_daily_comp_coverage_e2e.py -q
```

The e2e-style test mocks network/browser calls and verifies the full
`run_scrape()` orchestration returns at least 20 comparable listings for every
date in the requested range.
