import { NextRequest, NextResponse } from "next/server";
import { rerunListingSchema, type PreferredComps } from "@/lib/schemas";
import { generateShareId } from "@/lib/shareId";
import { getSupabaseAdmin } from "@/lib/supabase";
import { getSupabaseServer } from "@/lib/supabaseServer";
import { computeCacheKey } from "@/lib/cacheKey";
import { executionPolicyMeta } from "@/lib/reportPolicy";

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const { id } = await params;
    const supabase = await getSupabaseServer();
    const {
      data: { user },
    } = await supabase.auth.getUser();

    if (!user) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    // Fetch the saved listing (RLS scopes to user)
    const { data: listing, error: listingError } = await supabase
      .from("saved_listings")
      .select("*")
      .eq("id", id)
      .eq("user_id", user.id)
      .single();

    if (listingError || !listing) {
      return NextResponse.json(
        { error: "Listing not found" },
        { status: 404 }
      );
    }

    // Parse rerun body (dashboard "Re-run" sends an empty POST body)
    let body: unknown = {};
    try {
      body = await req.json();
    } catch {
      body = {};
    }
    const parsed = rerunListingSchema.safeParse(body);

    if (!parsed.success) {
      return NextResponse.json(
        { error: "Invalid input", details: parsed.error.flatten() },
        { status: 400 }
      );
    }

    const attrs = (listing.input_attributes ?? {}) as Record<string, unknown>;
    const VALID_INPUT_MODES = ["url", "criteria", "criteria-by-city", "criteria-by-zip"];
    const fallbackInputMode = VALID_INPUT_MODES.includes(attrs.inputMode as string)
      ? (attrs.inputMode as string)
      : "criteria";
    const fallbackListingUrl =
      typeof attrs.listingUrl === "string" ? attrs.listingUrl : undefined;
    // Carry preferred comps from saved listing; body can override
    const savedPreferredComps = (attrs.preferredComps as PreferredComps | undefined) ?? null;

    const {
      dates,
      inputMode = fallbackInputMode,
      listingUrl = fallbackListingUrl,
      preferredComps = savedPreferredComps ?? undefined,
    } = parsed.data;
    const discountPolicy =
      parsed.data.discountPolicy ?? listing.default_discount_policy ?? {};
    const attributes = listing.input_attributes;
    const address = listing.input_address;

    // Merged attributes — mirrors what will be written to report.input_attributes
    const mergedAttributes = {
      ...(attributes as Record<string, unknown>),
      inputMode,
      ...(preferredComps?.length ? { preferredComps } : {}),
    };

    // Use admin client for cache + report creation (bypasses RLS for pricing_cache)
    const admin = getSupabaseAdmin();

    // Cache key uses merged attributes so it matches report.input_attributes exactly
    const cacheKey = computeCacheKey(
      address,
      mergedAttributes,
      dates.startDate,
      dates.endDate,
      discountPolicy as Record<string, unknown>,
      listingUrl,
      inputMode
    );

    // Check cache
    let cachedSummary = null;
    let cachedCalendar = null;
    try {
      const { data: cacheRows } = await admin
        .from("pricing_cache")
        .select("summary, calendar")
        .eq("cache_key", cacheKey)
        .gt("expires_at", new Date().toISOString())
        .limit(1);

      if (cacheRows && cacheRows.length > 0) {
        cachedSummary = cacheRows[0].summary;
        cachedCalendar = cacheRows[0].calendar;
      }
    } catch {
      // Cache miss — proceed as queued
    }

    // Future-only enforcement: custom analyses must not use past start dates.
    const todayStr = new Date().toISOString().split("T")[0];
    if (dates.startDate < todayStr) {
      return NextResponse.json(
        { error: "Start date must be today or later. Past-date analysis is not supported." },
        { status: 400 }
      );
    }
    if (dates.endDate < dates.startDate) {
      return NextResponse.json(
        { error: "End date must be on or after start date." },
        { status: 400 }
      );
    }
    // 30-day inclusive window limit
    const rangeDays =
      Math.round(
        (new Date(dates.endDate).getTime() - new Date(dates.startDate).getTime()) / 86400000
      ) + 1;
    if (rangeDays > 30) {
      return NextResponse.json(
        { error: "Custom analysis can cover up to 30 days." },
        { status: 400 }
      );
    }

    const isCacheHit = cachedSummary !== null;
    const shareId = generateShareId();
    const targetEnv = process.env.WORKER_TARGET_ENV ?? "production";

    const report = {
      id: crypto.randomUUID(),
      user_id: user.id,
      share_id: shareId,
      listing_id: id,
      input_address: address,
      target_env: targetEnv,
      job_lane: "interactive",
      input_attributes: {
        ...attributes,
        inputMode,
        ...(preferredComps?.length ? { preferredComps } : {}),
      },
      input_date_start: dates.startDate,
      input_date_end: dates.endDate,
      discount_policy: discountPolicy,
      input_listing_url: listingUrl || null,
      cache_key: cacheKey,
      status: isCacheHit ? "ready" : "queued",
      core_version: isCacheHit ? "cache-hit" : "pending",
      result_summary: cachedSummary,
      result_calendar: cachedCalendar,
      result_core_debug: {
        ...executionPolicyMeta("interactive_live_report"),
        cache_hit: isCacheHit,
        cache_key: cacheKey,
        request_source: "api/listings/[id]/rerun",
        input_mode: inputMode,
        listing_id: id,
        report_input: {
          mode: inputMode,
          listing_url: listingUrl || null,
          listing_address: address,
          listing_attributes: attributes,
          date_start: dates.startDate,
          date_end: dates.endDate,
          discount_policy: discountPolicy,
        },
        created_at: new Date().toISOString(),
      },
      error_message: null,
    };

    const { error: insertError } = await admin
      .from("pricing_reports")
      .insert(report);

    if (insertError) {
      console.error("Report insert error:", insertError);
      return NextResponse.json(
        { error: "Failed to create report" },
        { status: 500 }
      );
    }

    // Link report to listing
    await admin.from("listing_reports").insert({
      saved_listing_id: id,
      pricing_report_id: report.id,
      trigger: "rerun",
    });

    // Update last_used_at
    await supabase
      .from("saved_listings")
      .update({ last_used_at: new Date().toISOString() })
      .eq("id", id);

    return NextResponse.json({
      id: report.id,
      shareId: report.share_id,
      status: report.status,
    });
  } catch {
    return NextResponse.json(
      { error: "Internal server error" },
      { status: 500 }
    );
  }
}
