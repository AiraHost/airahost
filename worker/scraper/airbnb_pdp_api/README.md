# Airbnb PDP crawler

Fetches Airbnb listing detail-page data through the public `StaysPdpSections`
GraphQL persisted-query endpoint and parses it into a flat JSON record.

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
# By listing id
python airbnb_crawler.py 47273102

# By rooms URL (id is extracted automatically)
python airbnb_crawler.py https://www.airbnb.com/rooms/47273102

# Full unparsed GraphQL response
python airbnb_crawler.py 47273102 --raw

# Save to a file
python airbnb_crawler.py 47273102 --out listing.json

# Different currency / locale / dates
python airbnb_crawler.py 47273102 --currency EUR --locale fr \
    --check-in 2026-08-01 --check-out 2026-08-05
```

## Parsed output

The default (parsed) output includes: `title`, `property_type`, `room_type`,
`city`, `latitude`, `longitude`, `person_capacity`, `bedrooms`, `beds`,
`baths`, `rating`, `review_count`, per-category ratings, `host_name`,
`host_id`, `is_superhost`, `years_hosting`, `is_guest_favorite`, `house_rules`,
`safety_features`, `cancellation_policy`, `image_url`, `canonical_url`.

### Amenities

`amenities` — flat list of every **offered** amenity title (from the "What this
place offers" section). Items the listing doesn't offer are excluded.

### Pricing

Price fields are populated **only when check-in/check-out dates are supplied**
— without dates the API returns "Add dates for prices" and `price_available`
is `false`. Provide dates via `--check-in`/`--check-out`, or just pass a rooms
URL that already contains `check_in`/`check_out` query params (as Airbnb's
share URLs do) and they're used automatically:

```bash
python airbnb_crawler.py 1596737613274892756 --check-in 2026-10-04 --check-out 2026-10-06
python airbnb_crawler.py "https://www.airbnb.com/rooms/159...?check_in=2026-10-04&check_out=2026-10-06"
```

Price fields: `price_available`, `price_display` (`"$1,018 USD"`),
`price_qualifier` (`"for 2 nights"`), `price_label`, `price_total` (numeric,
from the breakdown), `price_per_night`, `price_original_total` (pre-discount,
if any), `price_nights`, `price_currency`, `price_style`, and `price_breakdown`
(the itemized line list). Non-breaking spaces in Airbnb's price strings are
normalized to plain spaces.

Use `--raw` to get every section (booking, calendar, policies, SEO, etc.) for
custom extraction.

## Use as a library

```python
from airbnb_crawler import AirbnbPdpClient, parse

client = AirbnbPdpClient()
raw = client.fetch("47273102")
listing = parse(raw, "47273102")
print(listing.rating, listing.host_name)
```

## How it works

- The listing id (e.g. `47273102`, from `/rooms/47273102`) is base64-encoded
  into the two GraphQL node ids: `StayListing:<id>` and
  `DemandStayListing:<id>`.
- The request uses Airbnb's public web `x-airbnb-api-key` and a **persisted
  query** identified by a `sha256Hash`. Persisted queries are validated against
  their stored document, so the full declared variable set (all the
  `include*Fragment` toggles) must be sent — see `FRAGMENT_TOGGLES`.

## Keeping the hash fresh (Playwright)

Airbnb pins the query to a `sha256Hash` that changes on new frontend builds.
`get_hash.py` grabs the current one by driving a real Chromium page and
sniffing the live `StaysPdpSections` request — no manual Network-tab copying.

```bash
# One-time browser setup
python -m playwright install chromium

# Print the current hash
python get_hash.py

# Print it AND patch PERSISTED_QUERY_HASH in airbnb_crawler.py
python get_hash.py --update

# Watch the browser do it
python get_hash.py --headed
```

**The crawler self-heals automatically.** If a fetch fails in a way that looks
like a stale hash (GraphQL `ValidationError`, or HTTP 400/404), it launches
Playwright, refreshes the hash, patches the file, and retries once — you don't
have to do anything. Disable with `--no-auto-hash` (e.g. in environments
without a browser).

## Anti-bot handling

The client detects anti-bot challenges (HTTP 403/429/503, a non-JSON/HTML
"airlock" page, or an `airlock` marker in the body) and raises `AntiBotError`.
When `fetch_with_recovery` sees one, it drives a real browser to the listing,
which **passes the challenge** and yields fresh session cookies; those cookies
(plus a refreshed hash) are applied to the requests session before retrying —
this is what actually gets the direct request through, not the hash alone.

## Workload tester

`workload_tester.py` measures how long the crawler runs before Airbnb's
anti-bot wall goes up. It fires PDP requests round-robin across a configured
list of listings **with auto-recovery off** (recovering would clear the wall
and defeat the measurement) and stops after N *consecutive* anti-bot responses.

```bash
# Config a listings file (one id/URL per line; see listings.txt), then:
python workload_tester.py --listings listings.txt --threshold 3 --delay 0.5

# Or pass ids inline:
python workload_tester.py 47273102 20669368 --threshold 3

# Save the full per-request report:
python workload_tester.py --listings listings.txt --report report.json

# Safety cap so it terminates even if the wall never appears:
python workload_tester.py --listings listings.txt --max-requests 500
```

The report shows total requests sent, the OK/anti-bot/stale/error breakdown,
when the first anti-bot appeared, and — the headline number — **how many
requests (and seconds) it took to reach the 3 consecutive anti-bots**, plus the
request index where that winning streak began. A single success anywhere resets
the consecutive counter.

## When it breaks

- **`PersistedQueryNotFound` / `ValidationError` / HTTP 400** — stale hash.
  Handled automatically (see above), or run `python get_hash.py --update`
  manually.
- **HTTP 429 / airlock** — you're being rate-limited or challenged. Slow down,
  space out requests, and reuse a session.

## Notes

This is for personal/research use. Respect Airbnb's Terms of Service and
`robots.txt`, keep request volume low, and don't redistribute scraped data.
