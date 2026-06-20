# Discounted Scraped Price Bug

## Problem

The scraper could capture the original strikethrough total instead of Airbnb's
current discounted total. In the observed case, the app showed `$2824 / night`
and `From $5648 total for 2 nights`, while Airbnb showed a discounted current
price of `$4,659 USD for 2 nights` with `$5,648 USD` struck through.

The per-night value was not mathematically wrong for the original total:
`$5,648 / 2 = $2,824`. The bug was that the parser chose the original
strikethrough total instead of the discounted current total.

## Way To Reproduce

1. Use a PDP/search payload where `primaryLine.price` is `$5,648 USD`.
2. Include a discounted current price in either:
   - `primaryLine.accessibilityLabel`, such as
     `$4,659 USD for 2 nights, originally $5,648 USD`, or
   - `explanationData.priceDetails[].items[]` with
     `description = "Price after discount"` and `priceString = "$4,659 USD"`.
3. Run the parser for a 2-night stay.
4. Before the fix, the worker could emit `$2,824 / night` and `$5,648 total`
   instead of `$2,329.50 / night` and `$4,659 total`.

## Final Fix

Search parsing now prefers `accessibilityLabel` when it contains an original
price marker and `discountedPrice` is missing. That lets the current discounted
amount win over `primaryLine.price`.

PDP parsing now also prioritizes discount accessibility labels before
`primaryLine.price`. The booking-breakdown parser accepts `Price after discount`
rows for any explicit stay length, not only one-night stays, and normalizes
multi-night discounted totals by dividing by the explicit night count.

Regression coverage:

- `test_parse_search_context_prefers_discount_accessibility_label_when_discounted_price_missing`
- `test_parse_pdp_response_prefers_two_night_discount_breakdown_over_original_primary_price`
