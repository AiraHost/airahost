-- Migration 023: Global listing structural cache
-- Replaces the local _PDP_STRUCTURAL_CACHE.json file with a Supabase table
-- so amenity/structural data persists across worker restarts and is shared
-- globally (not per-machine).

CREATE TABLE IF NOT EXISTS listing_structural_cache (
    airbnb_listing_id  TEXT        PRIMARY KEY,
    accommodates       INTEGER,
    baths              DOUBLE PRECISION,
    property_type      TEXT,
    amenities          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE listing_structural_cache IS
    'Cached structural details fetched from Airbnb PDP for comparable listings. '
    'Keyed by Airbnb numeric room ID. Avoids repeated PDP scrapes for known listings.';
