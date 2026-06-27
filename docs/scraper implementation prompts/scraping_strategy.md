# Scraping Strategy

## Overview

The scraper collects Airbnb comparable listings (comps) for a target property using
a two-phase approach: a fixed comp pool built once per report, followed by per-day
availability checks with a fallback to fresh daily searches when coverage is thin.

**Primary method:** Playwright browser (Chrome CDP) — intercepts live Airbnb API
responses (StaysSearch GraphQL, StaysPdpSections GraphQL).

**Fallback method:** Rendered HTML parsing — when Playwright captures a
challenge/login page instead of JSON, listing IDs are extracted from the hydrated
DOM and prices from the booking widget via JavaScript evaluation.

---

## Phase 1: Fixed Comp Pool

**Goal:** Gather 20+ comparable listings within 8 miles of the target.

**Module:** `worker/scraper/price_estimator.py` → `_build_fixed_comp_pool_by_stride`

### Steps

1. **Anchor search** — Run a 1-night and a 2-night Playwright StaysSearch centered
   on the target's coordinates with a map-radius cap of 8 miles (≈ 12.87 km).
   Config: `MAP_RADIUS_CAP_KM = 8.0 * 1.609344` in `day_query.py`.

2. **Geo expansion** — If the initial radius yields fewer than `FIXED_COMP_BASELINE_SIZE`
   (default 20, env `FIXED_COMP_BASELINE_SIZE`) scored comps, the radius is expanded
   by 1 km and the search is repeated. Results are merged and de-duplicated by listing ID.

3. **Structural filtering** — `filter_similar_candidates()` applies hard gates
   (accommodates match, room type). Listings that fail structural filters are dropped.

4. **Similarity scoring** — Each candidate is scored against the target via
   `similarity_score()` (property type, capacity, bedrooms, baths, amenities, distance).
   Only listings scoring > 0.50 enter the pool.

5. **PDP enrichment** — For every comp in the pool, a full Playwright PDP visit
   (`get_listing_details`) is made to populate: amenities, baths, property type,
   accommodates, rating, reviews. Results are written to
   `.airbnb_pdp_structural_cache.json` so subsequent runs skip re-fetching unchanged
   listings.

6. **Pool assembly** — Top-N comps (N = `FIXED_COMP_BASELINE_SIZE`, default 20) ranked
   by similarity × appearance frequency are stored as the fixed pool, keyed by room ID.

**Config env vars:**

| Variable | Default | Description |
|---|---|---|
| `FIXED_COMP_BASELINE_SIZE` | `20` | Target pool size |
| `FIXED_COMP_POOL_STRIDE_DAYS` | `7` | Stride between anchor dates |
| `FIXED_COMP_POOL_GLOBAL_LIMIT` | `FIXED_COMP_BASELINE_SIZE` | Hard pool size cap |

---

## Phase 2: Per-Day Pricing

**Goal:** For each night in the report date range, collect ≥ 10 priced comps and
compute a market price.

**Module:** `worker/scraper/day_query.py` → `estimate_base_price_for_date`

### Steps

1. **Pool hydration search** — Run a concurrent 1-night + 2-night Playwright
   StaysSearch for the specific night (checkin = day, checkout = day+1 or day+2).
   Both searches are issued in parallel via `ThreadPoolExecutor(max_workers=2)`.

2. **Pool member filtering** — `_apply_known_pool_fields()` retains only search
   results whose listing ID appears in the fixed comp pool and backfills metadata
   (baths, property type, amenities, rating, reviews) from the pool cache.

3. **Per-day fallback (fresh search)** — If pool filtering yields fewer than
   `MIN_DAILY_COMPS_PER_DAY` (default 10, env `MIN_DAILY_COMPS_PER_DAY`) priced
   listings, the raw daily-search results (all listings found regardless of pool
   membership) are merged into the comp set. These fresh comps use whatever
   structural data the Airbnb search response provides directly and may have
   incomplete amenity data.

4. **Excluded comps filter** — Listings in `excluded_room_ids` (user blacklist or
   the target listing itself) are dropped before similarity/pricing math.

5. **Geographic distance filter** — Comps without coordinates pass through; comps
   with coordinates outside `MAP_RADIUS_CAP_KM` (8 miles) are dropped.

6. **Similarity scoring & floor** — Each comp is scored via `similarity_score()`.
   Only comps ≥ `SIMILARITY_FLOOR` (default 0.40) enter pricing. If no comps pass
   the strict floor, the relaxed floor (`SIMILARITY_FLOOR_FALLBACK = 0.25`) is used
   and the day is tagged `selection_mode = "fallback_relaxed"`.

7. **Price sanity & band filters** — Outlier prices (> 3× median) are excluded or
   down-weighted. A price-band filter is applied before the final pricing pass.

8. **Pricing** — `recommend_price()` computes a weighted median from the filtered
   comp pool. Preferred/pinned comps (user-designated benchmarks) receive a display
   score boost but do not inflate the market price.

**Config env vars:**

| Variable | Default | Description |
|---|---|---|
| `MIN_DAILY_COMPS_PER_DAY` | `10` | Minimum priced comps before fallback triggers |
| `MAP_RADIUS_CAP_KM` | 8 mi (~12.87 km) | Map search radius cap |
| `DAY_ONE_NIGHT_COMP_TARGET` | `25` | Target cards for 1-night daily search |
| `DAY_TWO_NIGHT_COMP_TARGET` | `25` | Target cards for 2-night daily search |
| `DAY_QUERY_SCROLL_ROUNDS` | `2` | Extra pagination rounds per daily search |
| `DAY_QUERY_MAX_CARDS` | `30` | Max search result cards per page |

---

## Primary vs Fallback Method

### Primary: Playwright API interception

**Module:** `worker/scraper/playwright_scraper.py`

- Connects to an existing Chrome instance via CDP (`CDP_URL`, default `127.0.0.1:9222`).
- **Search:** Navigates to an Airbnb search URL; intercepts the `StaysSearch` GraphQL
  POST response to get listing IDs + prices in structured JSON.
- **PDP:** Navigates to `/rooms/<id>?check_in=...`; intercepts `StaysPdpSections`
  POST response to get booking price, amenities, and listing metadata.
- Tab concurrency is capped by `AIRBNB_PLAYWRIGHT_MAX_TABS` (default 4).

### Fallback: Rendered HTML parsing

Triggered automatically when the primary path fails (anti-bot challenge, auth redirect,
or no JSON captured after the navigation):

- **Search fallback:** After the Playwright navigation, if `StaysSearch` JSON was
  never captured, JavaScript is evaluated in the page to extract listing IDs from
  `<a href="/rooms/...">` anchors and `data-*` attributes in the hydrated DOM.
  Returns a minimal payload shaped like the normal StaysSearch response.

- **PDP fallback:** If `StaysPdpSections` JSON is missing or its `BOOK_IT_*` sections
  contain no price, a DOM evaluation script (`_read_dom_price_text`) reads the booking
  widget price text directly from the rendered page, filtering for elements inside
  `[data-testid="book-it-*"]` / `[data-section-id*="BOOK_IT"]` containers.

- **Amenities fallback:** When the captured PDP JSON lacks amenity groups,
  `_extract_pdp_amenities_from_rendered_html` parses `application/json` script tags in
  the rendered HTML to recover `data.node.pdpPresentation.amenities`.

Challenge detection (captcha/login/checkpoint URL) is detected before the fallback and
triggers a warning log, but processing continues with the DOM-based path.

---

## Module Responsibilities

| Module | Role |
|---|---|
| `playwright_scraper.py` | Chrome CDP connection, browser navigation, API response capture, HTML fallback |
| `airbnb_client.py` | Thin facade over `PlaywrightScraper`; provides `search_listings_with_overrides`, `get_listing_details`, `browse_url_html` |
| `target_extractor.py` | Extracts listing spec (location, capacity, amenities, price) from a PDP visit |
| `comp_collection.py` | Runs a single search page, parses results, optionally does PDP enrichment on candidates |
| `parsers.py` | Parses `StaysSearch` → listing IDs + context; `StaysPdpSections` → price, amenities, metadata |
| `price_normalizer.py` | Normalises raw price text (nightly vs total, multi-night) to a canonical nightly amount |
| `day_query.py` | Per-day orchestration: concurrent 1-night + 2-night search, pool filtering, similarity scoring, pricing |
| `price_estimator.py` | Report-level orchestration: fixed pool build, day-query fan-out, transparent result assembly |
| `browser_runtime.py` | Browser client pool management for concurrent PDP enrichment |

---

## Data Flow

```
run_scrape()                          (price_estimator.py)
│
├─ extract_target_spec()              (target_extractor.py)
│   └─ get_listing_details()          (airbnb_client → playwright_scraper)
│       PDP: StaysPdpSections JSON ──► parse_pdp_response()
│                                      fallback: HTML amenities + DOM price
│
├─ _build_fixed_comp_pool_by_stride() (price_estimator.py)
│   └─ collect_search_comps()         (comp_collection.py)
│       search_listings_with_overrides() ──► StaysSearch JSON
│                                           fallback: DOM listing IDs
│       _enrich_comps_baths_and_property_type_from_pdp()
│           get_listing_details() × N ──► StaysPdpSections JSON (PDP enrichment)
│
└─ execute_day_queries_concurrently() (concurrent_runner.py)
    └─ estimate_base_price_for_date() (day_query.py) × sampled days
        ├─ collect_search_comps() 1-night  ─┐ concurrent
        ├─ collect_search_comps() 2-night  ─┘
        ├─ _apply_known_pool_fields()      (filter to pool, backfill metadata)
        ├─ [fallback] merge raw comps      (if pool_comps < MIN_DAILY_COMPS_PER_DAY)
        ├─ filter_similar_candidates()
        ├─ similarity_score() × comps
        ├─ apply_price_sanity()
        └─ recommend_price()              (pricing_engine.py)
```
