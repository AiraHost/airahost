# Nightly Missing Price Days Bug

## Problem

Nightly self-listing price capture occasionally missed one or two dates even
though the surrounding dates scraped successfully. A transient blank Airbnb
widget, backend hiccup, or browser timing issue could return no price for that
date, and the worker accepted the first missing result.

## Way To Reproduce

1. Run `_capture_user_listing_prices_for_range` for a multi-day range.
2. Simulate `capture_target_live_price` returning no price for one date on the
   first attempt, then returning a valid price for the same date on retry.
3. Before the fix, the final `priceByDate` omitted that date.

## Final Fix

The shared self-listing range capture now performs one retry pass for dates that
remain unpriced after the initial concurrent pass. Retried successful rows are
merged into `priceByDate`, and the start-date observed price prefers a later
successful retry over the first failed row for the same date.

Regression coverage:

- `test_self_price_capture_retries_missing_dates`
