# Implementation Prompt: Enhanced Filtering & Amenity Caching

You are tasked with implementing three related features to improve the efficiency and accuracy of comparable listing generation in the AiraHost worker.

## Feature 1: Enhanced Filter Options for Listing Search
**Goal:** Modify the search request payload to include structural filter boundaries (like bedrooms or beds). This will ensure most returned listings have a high probability of exceeding the 50% similarity threshold, without being so strict that valid borderline comps are filtered out.

**Instructions:**
1. **Target File:** `worker/scraper/comp_collection.py` (specifically `collect_search_comps` and the `overrides` dictionary).
2. **Implementation:** 
   - When building the `overrides` dictionary, map `target_bedrooms`, `target_beds`, and `target_baths` to their corresponding Airbnb API filter keys (e.g., `minBedrooms`, `maxBedrooms`, `minBeds`, `minBathrooms`).
   - Calculate safe boundaries based on the tolerances defined in `worker/core/similarity.py`. For example, since the "Medium" tier allows a bedroom tolerance of +/- 2, set `minBedrooms = max(0, target_bedrooms - 2)` and `maxBedrooms = target_bedrooms + 2`.
   - Ensure the query remains broad enough so listings that might pass the >50% threshold on other merits (like amenities or exact location) are not prematurely excluded.

## Feature 2: Cache Unknown Listing Amenities in Supabase
**Goal:** Currently, structural details (like amenities, exact baths, and property type) for unknown listings are cached locally in `_PDP_STRUCTURAL_CACHE.json`. Move this cache to Supabase so it persists globally and across worker runs.

**Instructions:**
1. **Target Files:** Create a new migration in `supabase/migrations/`, and modify `worker/scraper/comp_collection.py`.
2. **Implementation:**
   - **Database:** Create a new Supabase migration (e.g., `007_listing_structural_cache.sql`) with a table named `listing_structural_cache`. It should contain columns for `airbnb_listing_id` (Primary Key), `accommodates`, `baths`, `property_type`, `amenities` (JSONB), and `updated_at`.
   - **Worker Logic:** In `comp_collection.py`, update `_enrich_comps_baths_and_property_type_from_pdp` (and related cache functions) to query the Supabase `listing_structural_cache` table before falling back to scraping the Property Detail Page (PDP).
   - After a successful PDP scrape for an unknown listing, perform an upsert into the Supabase table with the newly fetched amenities and structural data.

## Feature 3: Read Reviews & Ratings from Search API (Skip PDP)
**Goal:** Avoid scraping the PDP solely to retrieve review counts and ratings if they are already available in the search response.

**Instructions:**
1. **Target Files:** `worker/scraper/comp_collection.py` and `worker/scraper/parsers.py`.
2. **Implementation:**
   - Verify that `parse_search_listing_context` is correctly extracting `rating` and `reviews` from the search response.
   - Ensure that `rating` and `reviews` populate the `ListingSpec` directly from the search phase.
   - Refactor the PDP enrichment phase so it *only* triggers if essential structural fields (like amenities or exact property type) are missing and not present in the Supabase cache. 
   - Confirm that the PDP parser does not overwrite valid ratings/reviews from the search API with `None` if the PDP fetch fails or omits them.
