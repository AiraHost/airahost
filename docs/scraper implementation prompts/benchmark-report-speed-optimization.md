# Benchmark Report Generation Speed Optimization

**Date:** 2026-07-16
**Result:** Reports with a benchmark listing went from **349.8s → ~26-40s** (goal: <50s).

| Scenario | Before | After |
|---|---|---|
| Bookable benchmark (short min-stay) | ~350s | **~32s** (25.8s repeat run) |
| Long-min-stay benchmark (30-night, e.g. `rooms/1027131080547966672`) | ~350s, pipeline failed → fell back | **~29s**, benchmark priced on all days |
| Unbookable benchmark (calendar closed, e.g. `rooms/720212490867839664`) | 83s (double pipeline) | **~40s**, market-only pricing, no fallback re-run |
| No benchmark (unchanged baseline) | 26.3s | 26.3s |

Wall times above include ~11s of Python/Playwright startup that the long-running worker does not pay per job; pipeline-internal times were 16-26s.

---

## Where the 350 seconds actually went

Traced from `worker/logs/worker.log.1` (job `22c43344`, 2026-07-15 23:29):

1. **Discount probe: ~31s wasted up front.** `probe_benchmark_discounts` did 2 browser PDP navigations (~13-15s each, both flagged "challenge/login detected"), then still failed to get a price.
2. **Comp starvation → double pipeline (the biggest cost).** Airbnb search cards ("skinny" rows) usually carry `accommodates=None`. The exact-capacity filter in `_matches_structural_filters` therefore dropped **15/15 comps every day** (`structural_excluded=15, priced=0`), which triggered deep-offset paging, then the 2-night fallback sweep, per day. With zero priced comps everywhere, the benchmark pipeline returned empty — and the worker **fell back to the full criteria-search pipeline**, re-running everything on top.
3. **Per-day benchmark price via browser PDP.** Direct PDP replays were frequently challenged, so each sampled day paid 1-2 browser navigations (10-15s each) — often just to fail anyway.

## The fixes

### 1. Benchmark price from a micro-radius map stays-search (never the browser)

StaysSearch over direct HTTP is rarely challenged, unlike PDP fetches. New per-day price source (`fetch_benchmark_price_via_micro_search`, `worker/core/benchmark.py`): a stays-search zoomed to a **±250m map box around the benchmark's own coordinates** — one ~1s HTTP request that returns the benchmark's card with its live discounted price.

- Coordinates come from one listing-page HTML GET + regex (`extract_listing_coords_from_html`, `worker/scraper/target_extractor.py`). The PDP GraphQL payload does *not* carry coords.
- 250m (not 50m) because Airbnb fuzzes public listing coords by up to ~150m.
- Per-day price priority is now: benchmark's card in the market search (free) → micro-search → direct-HTTP PDP replay. **Browser PDP navigation is fully out of the hot path.**
- Multi-night micro-search cards sometimes mislabel the trip total as a 1-night price (`nightly == total, price_nights == 1`); the helper re-normalizes by the query window.

### 2. Stay-window discovery for long-min-stay benchmarks

Some benchmarks (e.g. 30-night-minimum furnished rentals) have **no 1-2 night price anywhere** — not in search, not in PDP payloads, not in the rendered DOM. The old pipeline could never price them.

`discover_benchmark_stay_nights` probes windows **(1, 2, 3, 7, 30)** once per job (~1s each) to find the shortest bookable window; each day then issues exactly one micro-search with it. A nightly rate derived from a long window embeds weekly/monthly discounts, so it is flagged `benchmark_long_stay_rate` and gets `medium` fetch confidence (market pulls more).

### 3. Comp starvation fix

`collect_search_comps(..., enforce_exact_capacity=False, min_priced_target=8)` in benchmark mode only:

- The local exact-`accommodates` filter is skipped — the search request itself already filters server-side via `guests=<target capacity>` (capacity ≥ target guaranteed), and similarity scoring still ranks the pool.
- Paging stops once 8 priced comps exist (the pricing formula needs 5 for full market weight) instead of sweeping deep offsets toward a full 15-card page.
- The standard (no-benchmark) pipeline is untouched (`enforce_exact_capacity` defaults to `True`).

### 4. Browser-free, off-critical-path discount probe

The weekly-discount probe now uses the micro-search (1-night base vs 7-night effective nightly), runs on a **background thread concurrent with the day queries**, is skipped when the benchmark's min-stay exceeds 7 nights (no weekly signal exists), and is skipped when stay-window discovery already proved no bookable window at the start date.

### 5. Direct-only search plumbing

`AirbnbClient.search_listings_direct_only()`: micro-searches legitimately return empty pages (tiny box, unavailable listing), but `_try_direct_search` treats an empty page-1 as a soft-block and falls back to a ~30s browser navigation. The direct-only path returns the empty page as the answer.

### 6. Market-only rescue (no more double pipeline)

When the benchmark has **no bookable price on any sampled day** (calendar blocked/closed — verified with `rooms/720212490867839664`: PDP says `available: false` / "Those dates are not available" for every window, absent from all searches 12 weeks out), `apply_market_only_rescue` prices the sampled days from the **market medians the same day queries already collected** (flag `market_only_pricing`) instead of returning empty. The full standard-pipeline fallback — which used to double report time — no longer runs. Rescue never fires when at least one day has a real benchmark anchor.

The fallback reason surfaced to the report is now accurate: `benchmark_unavailable_for_dates` (detected from the PDP's explicit unavailability message) instead of the misleading "benchmark not found in url mode", with friendly copy in `HowWeEstimated.tsx`.

### 7. Sparse-market radius escalation

Shared per-job `RadiusEscalation` state: the first day query that finds **zero comps** doubles the map radius (one-shot 2× cap, never compounds), retries itself once at the doubled radius, and every later day query in the job starts doubled. Days that used the wider net carry the `map_radius_expanded` flag. Costs one extra search (~1-3s) only when it triggers.

---

## Files changed

| File | Change |
|---|---|
| `worker/core/benchmark.py` | Micro-search price source, stay-window discovery, direct-only PDP payload fetch, unavailability detection, market-only rescue, `RadiusEscalation`, search-first Stage 1 ordering, removed per-day sleeps and per-day secondary-comp PDP fetches |
| `worker/scraper/price_estimator.py` | Benchmark coords via HTML fetch, discovery + escalation wiring, background discount probe, market-only rescue call, accurate url-mode fallback reason |
| `worker/scraper/comp_collection.py` | `enforce_exact_capacity` parameter |
| `worker/scraper/airbnb_client.py` | `search_listings_direct_only()`, `fetch_listing_page_html()` |
| `worker/scraper/target_extractor.py` | `extract_listing_coords_from_html()` |
| `src/components/report/HowWeEstimated.tsx` | Friendly copy for `benchmark_unavailable_for_dates` |
| `worker/tests/test_benchmark_market_only_rescue.py` | New — rescue contract |
| `worker/tests/test_benchmark_radius_escalation.py` | New — radius-doubling contract |

## Verification

- Live E2E (target `1646256990163760678` + benchmark `1027131080547966672`, 7 nights): 28.9s / 29.1s / 34.3s / 25.8s across runs; all 7 days priced, `benchmarkUsed=True`, no fallback.
- Live E2E short-stay benchmark (`1657712309023493303`): 32.0s; 4/7 days high-confidence search hits, booked days interpolated, weekly probe functional.
- Live E2E unbookable benchmark (`720212490867839664` + target `904457150383964285`): 39.9s; all 7 days market-priced, `fallbackReason=benchmark_unavailable_for_dates`.
- Unit tests: all benchmark tests pass (15/15 in the touched area; 644 passed overall). The 7 failing suite tests (`test_price_extraction`, `test_search_context_price_availability`, `test_playwright_browser_recovery`, `test_listing_preflight`) fail identically on the unmodified tree — pre-existing, unrelated.

## Deliberate trade-offs

- **Exact-capacity comp filtering is off in benchmark mode** — capacity matching now relies on the server-side `guests` filter plus similarity scoring (approved feature cut for speed).
- **Search-card price is the primary anchor** (previously the listing-page booking widget was preferred). Card prices carry the same guest-facing discounted rate; confidence handling unchanged.
- **Long-stay-derived nightly rates** embed weekly/monthly discounts; down-weighted via `medium` confidence and flagged.

## Known follow-ups (not blocking)

- The browser PDP parser's "login/anti-bot detected" false positive (page renders fine, parser flags challenge) still exists but no longer affects benchmark generation time. Worth a separate fix for the standard pipeline.
- Radius escalation is benchmark-path only; the standard pipeline could adopt the same pattern.
- The unbookable-benchmark floor (~26s pipeline) could drop further by short-circuiting per-day benchmark probes after the first N unavailable days, at the cost of missing partially-blocked calendars.
