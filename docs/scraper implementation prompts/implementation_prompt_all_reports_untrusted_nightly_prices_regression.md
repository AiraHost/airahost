# Implementation Prompt: Fix the All-Reports Untrustworthy Nightly Prices Regression

## Objective

Diagnose and fix the regression introduced by the changes currently contained in commit `485d842be3d34b884d4e5229a1fec99220f2eec9` (the large scraper/runtime change made after parent `3f0d374bbf0abb0f6a947412f7e33e51095c21ed`). At present, listing report requests complete without enough accepted nightly prices and the report UI displays:

> We couldn't collect enough trustworthy nightly prices to build this report. Please try again in a moment.

Reproduce the failure end to end with Airbnb room ID `47273102` (`https://www.airbnb.com/rooms/47273102`), identify the exact causal change, implement the smallest production-safe correction, and add or modify deterministic unit tests that fail before the fix and pass afterward.

Do not merely hide or weaken the frontend error. The report must again contain trustworthy, date-specific nightly prices. Do not revert the entire commit or undo its unrelated browser-lifecycle, challenge-classification, observability, stale-date, or input-404 protections.

## Repository and regression boundary

- Work from the repository root.
- Treat `485d842` as the failing revision and `3f0d374` as its comparison parent unless repository history proves a different boundary.
- Inspect the complete diff with `git diff 3f0d374..485d842 -- worker worker/tests`, not just the commit title; the commit includes extensive production scraper changes in addition to tests.
- Preserve unrelated user changes in the worktree.
- Record the exact file, function, old behavior, new behavior, and runtime evidence for the causal change in the final implementation report.

## Leading hypothesis to prove or disprove

The most suspicious behavioral change is the new availability contract spanning:

- `worker/scraper/parsers.py::_extract_availability_context_from_search_result()`
- `worker/scraper/parsers.py::parse_search_listing_context()`
- `worker/scraper/comp_collection.py::collect_search_comps()`
- `worker/scraper/search_result_contract.py`

Before `485d842`, a search card with no explicit `available` field defaulted to `is_available=True`. After the change, it produces `is_available=None`. The collector then rejects every such priced row as `unknown-availability`:

```python
elif is_available is None:
    unknown_availability_count += 1
    ...
elif not bool(c.nightly_price and c.nightly_price > 0):
    ...
elif c.url:
    priced.append(c)
```

At the same time, the parser deliberately retains an observed structured price when availability is unknown (`is_available is not False`). This creates a likely internal contradiction: a normal card can have an authoritative displayed date-specific price but no explicit `available` boolean, the parser records its price, and the collector discards it solely because the optional boolean is absent. If the current Airbnb StaysSearch payload for room `47273102`'s market has priced cards without explicit `available`, this change explains why all reports now reach the trustworthiness fallback.

This is a hypothesis, not permission to patch blindly. Prove it using a captured/sanitized payload or bounded diagnostics and an A/B replay against the parent behavior. Also rule out other cross-cutting changes in `485d842`, especially the process-wide shared Playwright runtime/lease lifecycle, response capture, search-result classification, and browser recovery. A global failure across all listings could also result from shared state being prematurely closed, poisoned, or circuit-broken.

## Required investigation and reproduction

### 1. Trace the user-facing failure to its backend invariant

Start at `src/app/r/[shareId]/page.tsx` and identify the precise result fields/conditions that select the quoted message. Trace those fields backward through report persistence and worker orchestration to the nightly-price collection/acceptance decision. Document the actual failing invariant (for example, number of priced comparable-days, coverage ratio, or missing calendar prices); do not infer the cause from the UI string alone.

### 2. Build a repeatable live reproduction for room `47273102`

- Use listing ID `47273102`, not a substitute listing.
- Exercise the same production worker/report path and configuration used by listing report requests, including target extraction, comparable search, nightly-price collection, result assembly, and trust/coverage validation.
- Use a valid future date range relative to the execution date and the project's authoritative business timezone. Do not reuse the stale August 2026 dates in old logs.
- Prefer an existing CLI, test helper, or worker entry point. If none exists, add a small opt-in diagnostic or live integration test rather than a parallel implementation of report generation.
- Gate network/CDP-dependent tests behind an explicit environment variable such as `RUN_AIRBNB_LIVE_REPORT_E2E=1` so the ordinary unit suite remains deterministic.
- Do not persist a production report or mutate Supabase merely to reproduce if a dry-run/in-memory path can exercise the same logic. If persistence is unavoidable, clearly identify and clean up only the test record.
- Capture bounded, sanitized evidence for each search page/day: payload classification, parsed row count, rows with positive price, explicit `available=true`, explicit `available=false`, missing availability, accepted priced rows, and rejection reason counts. Never store cookies, tokens, raw full URLs, or full Airbnb payloads in the repository.
- Confirm the failure before changing production code. The reproduction must show where valid observed prices disappear or why they were never captured.

### 3. Pinpoint the exact regression by A/B comparison

Use the same sanitized captured response/artifact to replay parsing and collection behavior on both sides of the regression boundary. If necessary, use temporary worktrees or focused extraction/replay; do not destructively reset the user's working tree.

The evidence must answer:

1. Does the StaysSearch response contain positive date-specific displayed prices?
2. Do affected rows omit the explicit `available` property, or is it located in a shape the new parser fails to recognize?
3. Does `parse_search_listing_context()` preserve those prices and set `is_available=None`?
4. Does `collect_search_comps()` reject those same rows as `unknown-availability`?
5. Does the parent behavior accept enough of those rows to build the report?
6. If not, which exact change in `485d842` first causes the divergence?

Use `git blame`, focused diffing, and a unit-level replay/bisect of the changed functions. Do not state that a commit or line caused the issue unless reverting only that semantic difference (or simulating the old behavior) makes the captured reproduction pass.

## Fix requirements

Implement the narrowest fix justified by the evidence. Preserve these safety rules:

- Explicit `available=False`, sold-out markers, and minimum-stay violations must remain rejected for the requested dates.
- ID-only, malformed, challenge, login, and unrelated DOM-anchor rows must remain rejected.
- A positive price must be parsed from authoritative StaysSearch/card data for the exact requested check-in/check-out window. Do not invent, impute, or silently reuse a stale/default price.
- Do not accept arbitrary unpriced rows into nightly pricing merely to increase coverage.
- Keep `None` as “not explicitly stated” if that distinction is useful; do not globally turn all unknown availability into `True` at the parser boundary unless the captured Airbnb contract proves that is correct for every consumer.
- Prefer modeling bookability from a combination of authoritative evidence. For example, a valid search result returned for the exact dates with a positive structured displayed price and no explicit unavailable/min-stay signal may be accepted even when an optional `available` boolean is absent. Centralize this rule in a named predicate or typed contract so parser classification and collector acceptance cannot contradict each other again.
- If Airbnb moved the availability field rather than omitting it, update the parser for the real payload shape and keep the stricter collector semantics.
- If the root cause is instead shared runtime/session state, fix ownership/reset/lease behavior without returning to per-request Playwright driver leaks, and add a multi-report sequential/concurrent regression test.
- Preserve price normalization, currency handling, one-night versus multi-night total semantics, and existing protections against total-stay prices being mislabeled as nightly prices.
- Preserve all typed error behavior. A genuinely blocked or malformed search must not be presented as valid empty inventory.

Add a concise diagnostic summary at the acceptance boundary so a future incident distinguishes `priced_and_accepted`, `priced_but_unknown_availability`, `explicitly_unavailable`, `min_stay_blocked`, `missing_price`, and `malformed`. Keep logs bounded and non-duplicated.

## Mandatory regression tests

Add or modify deterministic unit tests using a minimal sanitized payload shaped like the live response for `47273102`'s comparable market. The test fixture must retain the relevant structure and field absence/presence that triggered the bug; do not mock the result after the faulty decision point.

At minimum, cover:

1. A well-formed search row for the exact dates with a positive structured price but no explicit `available` field follows the corrected, evidence-backed policy and can contribute a trusted nightly price.
2. The same row with explicit `available=False` is rejected even if a price string is present.
3. A row blocked by `min_nights > query_nights` is rejected.
4. An ID-only row is rejected.
5. A row with neither a positive price nor authoritative availability evidence is not promoted into the priced pool.
6. Parser classification and collector acceptance agree for all above cases; avoid separate contradictory truthiness rules.
7. The report-level aggregation/coverage gate receives enough accepted priced observations from the regression fixture and does not select the “couldn't collect enough trustworthy nightly prices” state.
8. If shared runtime state is causal or modified, two sequential reports and the relevant concurrent path do not contaminate, close, or circuit-break one another.

The primary regression test must fail on unmodified `485d842` for the same reason observed in the live reproduction and pass with the fix. Avoid a test that only asserts an implementation detail or simply changes the expected value of an existing test.

Keep an opt-in live smoke test for room `47273102` if practical. It should assert contract-level outcomes (report succeeds, has nonzero trustworthy nightly-price coverage, no explicit unavailable rows were accepted), not exact prices or exact comparable IDs, which are volatile.

## Verification

Run, at minimum, from the repository root:

```powershell
python -m pytest worker/tests/test_search_context_price_availability.py -q
python -m pytest worker/tests/test_collect_search_comps_integrity.py -q
python -m pytest worker/tests/test_search_result_contract.py -q
```

Also run the new/modified regression test directly, then the broader relevant scraper suite. If environment time permits, run all worker unit tests. Run the opt-in live room `47273102` reproduction after the deterministic suite, using the configured local Chrome/CDP setup.

For every command, report pass/fail/skip counts. A live test skipped because CDP, credentials, or network is unavailable does not replace the deterministic regression test. Do not weaken existing assertions or exclude failing tests without explaining why they are unrelated.

## Definition of done

The work is complete only when:

- Room `47273102` reproduces the original failure before the fix using the real report path.
- The exact regression in `485d842` is demonstrated with before/after evidence against `3f0d374`.
- The production fix restores trustworthy nightly-price coverage without accepting unavailable, malformed, ID-only, stale, or fabricated price rows.
- A deterministic regression test fails on the bad behavior and passes on the fix.
- Relevant existing tests pass.
- A post-fix live smoke run for room `47273102` succeeds, or any external blocker is explicitly documented along with the deterministic replay evidence.
- The final implementation report lists the root cause, changed files, why the fix is safe, tests run and results, and any remaining operational risks.
