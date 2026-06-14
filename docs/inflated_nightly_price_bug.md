# Inflated User Listing Nightly Price Bug

## Problem

The 30-day pricing calendar showed the host's own listing price as a fee-inclusive
booking total instead of the listing's one-night base rate. In the reported case,
the dashboard showed `Your $1048` for June 17, 2026, while Airbnb showed `$430 USD
for 1 night` for check-in June 17, 2026 and checkout June 18, 2026.

Two backend behaviors caused this:

1. `_capture_user_listing_prices_for_range` tried longer booking windows before a
   one-night window. If a multi-night request returned a price, the exact one-night
   request was never attempted.
2. `parse_pdp_response` trusted `structuredDisplayPrice.primaryLine` first. Airbnb
   can put a fee-inclusive booking total there, while the true nightly base rate is
   only available in `structuredDisplayPrice.explanationData.priceDetails`.

The result was that `summary.priceByDate` and `calendar[].userListingPrice`
received inflated values, so the dashboard rendered inflated `Your $...` labels.

## Way To Reproduce

1. Use a listing/date where Airbnb shows a one-night price in the booking widget,
   for example check-in `2026-06-17`, checkout `2026-06-18`, with `$430 USD for
   1 night`.
2. Run a report with that listing URL and a 30-day date range containing
   `2026-06-17`.
3. Before the fix, inspect the completed report payload:
   `summary.priceByDate["2026-06-17"]` or the matching calendar day's
   `userListingPrice` can show the multi-night booking total, for example `1048`,
   instead of the true one-night price `430`.
4. The same parser failure can be reproduced with a PDP payload where
   `primaryLine.price` is `$1,048 USD` and
   `explanationData.priceDetails[0].items[0].description` is
   `2 nights x $430 USD`.

## Final Solution

The user-listing price capture now attempts the exact one-night window first.
Longer windows are only fallbacks when Airbnb does not return a one-night price.

The PDP parser now checks Airbnb's booking breakdown before falling back to
`primaryLine`. It extracts the base nightly amount from breakdown rows shaped like
`1 night x $430 USD`, `2 nights x $430 USD`, or `$430 USD x 2 nights`, and marks the
result as `nightly_from_pdp_breakdown`. If no breakdown is present, the parser keeps
the legacy primary-line normalization behavior.

Regression coverage:

- `test_parse_pdp_response_prefers_nightly_breakdown_over_fee_inclusive_primary_total`
- `test_parse_pdp_response_supports_amount_before_nights_breakdown_shape`
- `test_self_price_capture_prefers_one_night_before_minimum_stay_fallback`
