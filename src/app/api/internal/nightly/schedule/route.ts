/**
 * POST /api/internal/nightly/schedule
 *
 * Railway cron endpoint — call this once per day (e.g. 00:00 UTC).
 *
 * What it does:
 *   For every saved_listing with a valid Airbnb listing URL, creates a
 *   live_analysis pricing_report covering the next 30 days (tomorrow →
 *   tomorrow + 29).  trigger = 'scheduled' on listing_reports distinguishes
 *   these from manual runs so the dashboard can label them "Nightly Market Report".
 *
 * Auth:
 *   Authorization: Bearer <INTERNAL_API_SECRET>
 *
 * Optional body (for local / single-listing testing):
 *   { "listingId": "<uuid>" }              — schedule one listing only
 *   { "listingIds": ["<uuid>", ...] }      — schedule a subset of listings
 *   (omit body entirely to schedule all eligible listings — default behaviour)
 *
 * Local-dev force bypass:
 *   { "listingId": "<uuid>", "force": true }
 *   Skips the 23-hour dedup check for the specified listing(s).
 *   Only allowed when WORKER_TARGET_ENV === "local".  Returns 400 in production.
 *   Requires listingId / listingIds — "force: true" without a filter is rejected.
 *
 * Response (always 200 for scheduler-safe retries):
 *   { filter, force, scheduled, skipped, total, results[] }
 *   filter: "all" | { listingIds: string[] }
 *   force: boolean
 */

import { NextRequest, NextResponse } from "next/server";
import { getSupabaseAdmin } from "@/lib/supabase";
import { generateShareId } from "@/lib/shareId";
import { computeCacheKey } from "@/lib/cacheKey";
import { executionPolicyMeta } from "@/lib/reportPolicy";

const INTERNAL_API_SECRET = process.env.INTERNAL_API_SECRET;

/** Skip if a scheduled job already exists within this window (23 h). */
const DEDUP_HOURS = 23;

/** How many days of coverage each nightly report provides (always 30). */
const NIGHTLY_DAYS = 30;

/** Fallback timezone for listings that have no resolved timezone yet. */
const FALLBACK_TIMEZONE = "America/Los_Angeles";

function extractAirbnbRoomId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const match = value.match(/\/rooms\/(\d+)/i);
  return match?.[1] ?? null;
}

/**
 * Compute the nightly date range for a specific IANA timezone.
 * startDate = local tomorrow in that timezone
 * endDate   = local tomorrow + 29 days
 *
 * Uses Intl.DateTimeFormat to get the correct local calendar date,
 * avoiding UTC truncation errors for listings outside UTC.
 */
function nightlyDateRangeForTimezone(timezone: string): { startDate: string; endDate: string } {
  let tz = timezone;
  // Validate the timezone — fall back if Intl rejects it
  try {
    Intl.DateTimeFormat(undefined, { timeZone: tz });
  } catch {
    tz = FALLBACK_TIMEZONE;
  }

  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });

  // Get today's local date string (YYYY-MM-DD) in the listing's timezone
  const todayParts = fmt.formatToParts(new Date());
  const todayStr = `${todayParts.find(p => p.type === "year")!.value}-${todayParts.find(p => p.type === "month")!.value}-${todayParts.find(p => p.type === "day")!.value}`;

  // Advance by 1 day (tomorrow) and by NIGHTLY_DAYS days (end) using UTC math
  // anchored on the local date — avoids DST drift within the window.
  const todayDate = new Date(`${todayStr}T12:00:00Z`); // noon UTC = safe midday anchor
  const startMs = todayDate.getTime() + 1 * 24 * 60 * 60 * 1000;
  const endMs   = todayDate.getTime() + NIGHTLY_DAYS * 24 * 60 * 60 * 1000;

  const fmtDate = (ms: number) => {
    const parts = fmt.formatToParts(new Date(ms));
    return `${parts.find(p => p.type === "year")!.value}-${parts.find(p => p.type === "month")!.value}-${parts.find(p => p.type === "day")!.value}`;
  };

  return { startDate: fmtDate(startMs), endDate: fmtDate(endMs) };
}

type SkipReason =
  | "duplicate_pending"
  | "duplicate_recent_ready"
  | "no_attributes"
  | "missing_listing_url"
  | "invalid_listing_url"
  | "api_error"
  | "not_found";

type ResultEntry = {
  listingId: string;
  listingName: string | null;
  scheduled: boolean;
  reason?: SkipReason | "created";
  reportId?: string;
  shareId?: string;
};

export async function POST(req: NextRequest) {
  // ── Auth ─────────────────────────────────────────────────────────────────
  if (!INTERNAL_API_SECRET) {
    console.error("[nightly/schedule] INTERNAL_API_SECRET not configured");
    return NextResponse.json(
      { error: "Scheduler not configured — INTERNAL_API_SECRET missing" },
      { status: 500 }
    );
  }
  const authHeader = req.headers.get("authorization") ?? "";
  const token = authHeader.startsWith("Bearer ") ? authHeader.slice(7) : "";
  if (token !== INTERNAL_API_SECRET) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  // ── Parse optional listing filter from body ─────────────────────────────
  // Always read the raw text first — don't rely on content-type alone.
  // Empty string → no body → full schedule (default).
  let filterIds: string[] | null = null; // null = schedule all (default)
  let filterAirbnbRoomId: string | null = null;
  let forceDedup = false;                 // bypass 23-hour dedup — local-only
  const rawBody = (await req.text()).trim();

  if (rawBody.length > 0) {
    let body: unknown;
    try {
      body = JSON.parse(rawBody);
    } catch {
      return NextResponse.json(
        { error: "Invalid JSON body" },
        { status: 400 }
      );
    }

    if (body !== null && typeof body === "object" && !Array.isArray(body)) {
      const b = body as Record<string, unknown>;

      if ("listingId" in b) {
        if (typeof b.listingId !== "string" || !b.listingId) {
          return NextResponse.json(
            { error: "listingId must be a non-empty string" },
            { status: 400 }
          );
        }
        filterIds = [b.listingId];
      } else if ("listingIds" in b) {
        if (!Array.isArray(b.listingIds) || (b.listingIds as unknown[]).length === 0) {
          return NextResponse.json(
            { error: "listingIds must be a non-empty array" },
            { status: 400 }
          );
        }
        if (!(b.listingIds as unknown[]).every((x) => typeof x === "string" && x.length > 0)) {
          return NextResponse.json(
            { error: "listingIds must contain non-empty strings" },
            { status: 400 }
          );
        }
        filterIds = b.listingIds as string[];
      } else if ("airbnbRoomId" in b || "listingAirbnbRoomId" in b) {
        const rawRoomId = b.airbnbRoomId ?? b.listingAirbnbRoomId;
        if (typeof rawRoomId !== "string" || !/^\d+$/.test(rawRoomId)) {
          return NextResponse.json(
            { error: "airbnbRoomId must be a numeric string" },
            { status: 400 }
          );
        }
        filterAirbnbRoomId = rawRoomId;
      }

      // ── force: true — local-dev dedup bypass ───────────────────────────────
      if (b.force === true) {
        // Must have a listing filter — force on all listings is never allowed.
        if (filterIds === null && filterAirbnbRoomId === null) {
          return NextResponse.json(
            { error: "force:true requires listingId, listingIds, or airbnbRoomId — cannot force-run all listings" },
            { status: 400 }
          );
        }
        // Only allowed when WORKER_TARGET_ENV === "local".
        const targetEnvCheck = process.env.WORKER_TARGET_ENV ?? "production";
        if (targetEnvCheck !== "local") {
          return NextResponse.json(
            {
              error: `force:true is only allowed when WORKER_TARGET_ENV=local (current: ${targetEnvCheck})`,
              hint: "Set WORKER_TARGET_ENV=local in .env.local to use the force bypass.",
            },
            { status: 400 }
          );
        }
        forceDedup = true;
      }
      // body present but neither listingId/listingIds nor force → treat as full run
    } else if (body !== null) {
      return NextResponse.json(
        { error: "Body must be a JSON object" },
        { status: 400 }
      );
    }
  }

  const isFiltered = filterIds !== null || filterAirbnbRoomId !== null;

  const admin = getSupabaseAdmin();
  const dedupCutoff = new Date(
    Date.now() - DEDUP_HOURS * 60 * 60 * 1000
  ).toISOString();
  const now = new Date().toISOString();
  const targetEnv = process.env.WORKER_TARGET_ENV ?? "production";
  console.log(
    `[nightly/schedule] target_env=${targetEnv} job_lane=nightly` +
    (filterIds ? ` filter=partial listingIds=${filterIds.join(",")}` : "") +
    (filterAirbnbRoomId ? ` filter=airbnbRoomId(${filterAirbnbRoomId})` : "") +
    (!isFiltered ? " filter=all" : "") +
    (forceDedup ? " force=true (dedup bypassed)" : "")
  );

  // ── Fetch listings (all, or filtered subset) ─────────────────────────────
  let listingsQuery = admin
    .from("saved_listings")
    .select(
      "id, name, user_id, input_address, input_attributes, default_discount_policy, listing_timezone"
    )
    .order("created_at", { ascending: false });

  if (filterIds) {
    listingsQuery = listingsQuery.in("id", filterIds);
  }

  const { data: fetchedListings, error: listingsErr } = await listingsQuery;

  if (listingsErr) {
    console.error("[nightly/schedule] listings fetch failed", listingsErr);
    return NextResponse.json(
      { error: "Failed to fetch listings", detail: listingsErr.message },
      { status: 500 }
    );
  }

  let listings = fetchedListings ?? [];
  if (filterAirbnbRoomId) {
    listings = listings.filter((listing) => {
      const attrs = (listing.input_attributes ?? {}) as Record<string, unknown>;
      const listingUrl =
        (attrs.listingUrl as string | null | undefined) ??
        (attrs.listing_url as string | null | undefined) ??
        null;
      return extractAirbnbRoomId(listingUrl) === filterAirbnbRoomId;
    });
  }

  if (!listings || listings.length === 0) {
    return NextResponse.json({
      filter: filterAirbnbRoomId
        ? { airbnbRoomId: filterAirbnbRoomId }
        : isFiltered
          ? { listingIds: filterIds }
          : "all",
      scheduled: 0,
      skipped: isFiltered ? 1 : 0,
      total: isFiltered ? 1 : 0,
      results: filterAirbnbRoomId
        ? [
            {
              listingId: filterAirbnbRoomId,
              listingName: null,
              scheduled: false,
              reason: "not_found",
            },
          ]
        : [],
    });
  }

  // ── Report not-found IDs when a filter was requested ────────────────────
  const results: ResultEntry[] = [];
  if (filterIds) {
    const foundIds = new Set(listings.map((l) => l.id));
    for (const reqId of filterIds) {
      if (!foundIds.has(reqId)) {
        results.push({
          listingId: reqId,
          listingName: null,
          scheduled: false,
          reason: "not_found",
        });
      }
    }
  }

  // ── Per-listing dedup check: any scheduled job in last DEDUP_HOURS hours ──
  const listingIds = listings.map((l) => l.id);
  const { data: recentLinks } = await admin
    .from("listing_reports")
    .select(
      "saved_listing_id, trigger, pricing_reports:pricing_report_id(id, status)"
    )
    .in("saved_listing_id", listingIds)
    .eq("trigger", "scheduled")
    .gte("created_at", dedupCutoff);

  // Build a set of listing IDs that already have a recent scheduled job
  type PricingReportRow = { id: string; status: string } | { id: string; status: string }[] | null;
  const recentlyScheduled = new Set<string>();
  for (const link of recentLinks ?? []) {
    const rr = link.pricing_reports as PricingReportRow;
    const r = Array.isArray(rr) ? rr[0] : rr;
    if (r && (r.status === "queued" || r.status === "running" || r.status === "ready")) {
      recentlyScheduled.add(link.saved_listing_id);
    }
  }

  // ── Create jobs ───────────────────────────────────────────────────────────
  for (const listing of listings) {
    if (recentlyScheduled.has(listing.id) && !forceDedup) {
      results.push({
        listingId: listing.id,
        listingName: listing.name,
        scheduled: false,
        reason: "duplicate_recent_ready",
      });
      continue;
    }

    // Per-listing timezone-aware date range
    const listingTz = (listing.listing_timezone as string | null | undefined) ?? FALLBACK_TIMEZONE;
    const { startDate, endDate } = nightlyDateRangeForTimezone(listingTz);

    const attrs = (listing.input_attributes ?? {}) as Record<string, unknown>;
    const address: string = listing.input_address ?? "";

    if (!address) {
      results.push({
        listingId: listing.id,
        listingName: listing.name,
        scheduled: false,
        reason: "no_attributes",
      });
      continue;
    }

    // Nightly eligibility gate: only listings with a valid Airbnb room URL.
    // Address-only listings fall through to criteria mode in the worker and
    // produce stable errors — exclude them here instead.
    const listingUrl =
      (attrs.listingUrl as string | null | undefined) ??
      (attrs.listing_url as string | null | undefined) ??
      null;
    const isValidListingUrl = !!(
      listingUrl &&
      /airbnb\.(com|ca)\/rooms\//i.test(listingUrl)
    );
    if (!isValidListingUrl) {
      results.push({
        listingId: listing.id,
        listingName: listing.name,
        scheduled: false,
        reason: listingUrl ? "invalid_listing_url" : "missing_listing_url",
      });
      continue;
    }

    // inputMode is always "url" here — ineligible listings are skipped above.
    const inputMode = "url";

    const discountPolicy = (listing.default_discount_policy ?? {}) as Record<string, unknown>;

    // Build full input_attributes (same shape as manual runs)
    const inputAttributes: Record<string, unknown> = {
      ...attrs,
      inputMode,
      listingUrl: listingUrl ?? null,
    };

    const cacheKey = computeCacheKey(
      address,
      inputAttributes,
      startDate,
      endDate,
      discountPolicy,
      listingUrl ?? undefined,
      inputMode
    );

    const reportId = crypto.randomUUID();
    const shareId = generateShareId();

    const report = {
      id: reportId,
      user_id: listing.user_id,
      share_id: shareId,
      listing_id: listing.id,
      report_type: "live_analysis",
      input_address: address,
      target_env: targetEnv,
      job_lane: "nightly",
      input_attributes: inputAttributes,
      input_date_start: startDate,
      input_date_end: endDate,
      discount_policy: discountPolicy,
      input_listing_url: listingUrl,
      cache_key: cacheKey,
      status: "queued",
      core_version: "pending",
      result_summary: null,
      result_calendar: null,
      completed_at: null,
      market_captured_at: null,
      error_message: null,
      result_core_debug: {
        ...executionPolicyMeta("nightly_board_refresh"),
        // Force fresh scrape — nightly reports must always have current market data.
        force_rerun: true,
        cache_hit: false,
        cache_key: cacheKey,
        nightly: true,
        nightly_date_range: { startDate, endDate },
        request_source: "api/internal/nightly/schedule",
        input_mode: inputMode,
        listing_id: listing.id,
        created_at: now,
      },
    };

    const { error: insertErr } = await admin.from("pricing_reports").insert(report);

    if (insertErr) {
      console.error(
        `[nightly/schedule] insert failed for listing ${listing.id}:`,
        insertErr
      );
      results.push({
        listingId: listing.id,
        listingName: listing.name,
        scheduled: false,
        reason: "api_error",
      });
      continue;
    }

    // Link to listing with trigger='scheduled'
    await admin.from("listing_reports").insert({
      saved_listing_id: listing.id,
      pricing_report_id: reportId,
      trigger: "scheduled",
    });

    results.push({
      listingId: listing.id,
      listingName: listing.name,
      scheduled: true,
      reason: "created",
      reportId,
      shareId,
    });
  }

  const scheduled = results.filter((r) => r.scheduled).length;
  const skipped = results.length - scheduled;
  const filter = filterAirbnbRoomId
    ? { airbnbRoomId: filterAirbnbRoomId }
    : isFiltered
      ? { listingIds: filterIds }
      : "all";

  console.log(
    `[nightly/schedule] done: filter=${
      filterAirbnbRoomId
        ? `airbnbRoomId(${filterAirbnbRoomId})`
        : filterIds
          ? `partial(${filterIds.join(",")})`
          : "all"
    } ` +
      `force=${forceDedup} ` +
      `${scheduled} scheduled, ${skipped} skipped of ${results.length} total`
  );

  return NextResponse.json({
    filter,
    force: forceDedup,
    scheduled,
    skipped,
    total: results.length,
    results,
  });
}
