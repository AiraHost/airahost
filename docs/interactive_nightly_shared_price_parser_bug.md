# Interactive And Nightly Shared Price Parser Bug

## Problem

Interactive live price capture could disagree with nightly live price capture
and report the wrong value. In the observed screenshot, the worker surfaced
`$769/night` while Airbnb's booking widget showed `$684 USD for 1 night`.

The wrong value was not a multiplication of the visible Airbnb price. Airbnb can
place the visible current price in a booking breakdown row such as
`Price after discount`, while `primaryLine.price` carries another nearby price.
The parser treated that current-price breakdown row as non-price metadata and
fell back to `primaryLine.price`.

## Way To Reproduce

1. Use a PDP payload where `primaryLine.price` is `$769 USD`.
2. Include `explanationData.priceDetails[].items[]` with
   `description = "Price after discount"` and `priceString = "$684 USD"`.
3. Include one-night context such as `accessibilityLabel = "$684 USD for 1 night"`.
4. Call the client-backed live extraction path for a one-night report window.
5. Before the fix, the worker could surface `$769` instead of `$684`.

## Final Fix

`parse_pdp_response` now recognizes one-night `Price after discount` style
breakdown rows as the current nightly price before falling back to
`primaryLine.price`.

`price_normalizer.nightly_price_from_parsed_pdp` remains the shared decision
point for PDP parsed prices. It trusts `parse_pdp_response()["nightly_price"]`
when present and only falls back to deriving from `total_price` when the parser
did not return a nightly value.

Interactive live capture, nightly self-listing capture, day-query PDP
revalidation, and benchmark direct-page extraction now use that shared helper.

Regression coverage:

- `test_client_pdp_extraction_uses_price_after_discount_not_primary_price`
- `test_parse_pdp_response_prefers_one_night_price_after_discount_over_primary_price`
- `test_parse_pdp_response_prefers_nightly_breakdown_over_fee_inclusive_primary_total`
