# Nightly Decimal Scraped Price Bug

## Problem

Nightly report payloads could expose direct Airbnb nightly prices with odd
cent-level artifacts, for example `40.19`, even though Airbnb displayed the
same card as `$41`.

This is different from valid fractional prices produced by dividing a whole
multi-night total, such as `$827 / 2 nights = 413.5`. Those `.5` values are
allowed and should remain available in scraped price fields.

## Way To Reproduce

1. Use a search/PDP payload where Airbnb returns a direct one-night value such
   as `$40.19 USD`, while the rendered Airbnb card shows `$41`.
2. Run the nightly search parser path for that listing/date.
3. Before the fix, `nightly_price`, `comparableListings[].nightlyPrice`, or
   `priceByDate` could show `40.19`.
4. Multi-night total division should still preserve valid fractional values,
   for example `413.5`.

## Final Fix

The shared price normalizer now supports source-aware whole-dollar display
normalization.
Search and PDP parser paths use it only for direct one-night API values,
matching Airbnb's rendered whole-dollar display by ceiling decimal artifacts
such as `40.19` to `41`.

The report assembler no longer rounds all scraped prices to integers.
Multi-night-total-derived prices such as `413.5` are intentionally preserved.
The comparable card also formats selected 1-night exact-date prices with the
same whole-dollar display rule so older payloads do not render cent artifacts.

Regression coverage:

- `test_parse_search_context_ceils_direct_nightly_api_decimal_to_display_price`
- `test_normalizer_ceils_one_night_api_decimal_but_keeps_divided_total_fraction`
- `test_price_by_date_details_keeps_day_level_url_and_query_nights`
