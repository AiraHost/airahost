import { randomUUID } from "node:crypto";

import { NextRequest, NextResponse } from "next/server";

import { normalizeMlForecastRunRow } from "@/lib/mlSidecar";
import { getSupabaseAdmin } from "@/lib/supabase";
import { getSupabaseServer } from "@/lib/supabaseServer";

type PricingReportRow = {
  id: string;
  created_at: string | null;
  completed_at: string | null;
  result_summary: Record<string, unknown> | null;
};

function asObject(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function buildMlForecastPayload(params: {
  reportId: string;
  jobId?: string;
  createdAt?: string | null;
  completedAt?: string | null;
  status: "queued" | "running" | "ready" | "error";
  trainingScope?: string | null;
  modelMode?: string | null;
  nSamples?: number | null;
  generatedAt?: string | null;
  errorMessage?: string | null;
  metrics?: unknown;
  explanation?: unknown;
  predictions?: unknown;
}) {
  const {
    reportId,
    jobId = reportId,
    createdAt,
    completedAt,
    status,
    trainingScope = null,
    modelMode = null,
    nSamples = null,
    generatedAt = null,
    errorMessage = null,
    metrics = null,
    explanation = null,
    predictions = [],
  } = params;

  return {
    id: jobId,
    jobId,
    reportId,
    status,
    trainingScope,
    modelMode,
    nSamples,
    generatedAt,
    createdAt,
    completedAt,
    errorMessage,
    metrics,
    explanation,
    predictions,
  };
}

function mergeSummaryWithMlForecast(
  summary: Record<string, unknown> | null,
  mlForecast: Record<string, unknown>
) {
  return {
    ...(summary ?? {}),
    mlForecast,
  };
}

async function getAuthorizedListing(id: string) {
  const supabase = await getSupabaseServer();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) {
    return {
      user: null,
      listing: null,
      response: NextResponse.json({ error: "Unauthorized" }, { status: 401 }),
    };
  }

  const { data: listing, error } = await supabase
    .from("saved_listings")
    .select("id, user_id, name")
    .eq("id", id)
    .eq("user_id", user.id)
    .single();

  if (error || !listing) {
    return {
      user,
      listing: null,
      response: NextResponse.json({ error: "Listing not found" }, { status: 404 }),
    };
  }

  return { user, listing, response: null };
}

async function getLatestWritableReport(
  savedListingId: string
): Promise<PricingReportRow | null> {
  const admin = getSupabaseAdmin();
  const { data } = await admin
    .from("listing_reports")
    .select(
      "created_at, pricing_reports:pricing_report_id(id, status, report_type, created_at, completed_at, result_summary)"
    )
    .eq("saved_listing_id", savedListingId)
    .order("created_at", { ascending: false })
    .limit(20);

  for (const row of data ?? []) {
    const relation = Array.isArray(row.pricing_reports)
      ? row.pricing_reports[0]
      : row.pricing_reports;
    if (
      relation &&
      relation.status === "ready" &&
      (relation.report_type ?? "live_analysis") === "live_analysis"
    ) {
      return {
        id: relation.id as string,
        created_at:
          typeof relation.created_at === "string" ? relation.created_at : null,
        completed_at:
          typeof relation.completed_at === "string"
            ? relation.completed_at
            : null,
        result_summary: asObject(relation.result_summary),
      };
    }
  }

  return null;
}

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    const { id } = await params;
    const auth = await getAuthorizedListing(id);
    if (auth.response) {
      return auth.response;
    }

    const report = await getLatestWritableReport(id);
    const mlForecast = asObject(report?.result_summary?.mlForecast);

    return NextResponse.json({
      run:
        report && mlForecast
          ? normalizeMlForecastRunRow({
              ...mlForecast,
              id: mlForecast.jobId ?? mlForecast.id ?? report.id,
              reportId: report.id,
              createdAt: mlForecast.createdAt ?? report.created_at,
              completedAt: mlForecast.completedAt ?? report.completed_at,
            })
          : null,
    });
  } catch (error) {
    return NextResponse.json(
      {
        error: "Failed to load ML forecast.",
        details: error instanceof Error ? error.message : null,
      },
      { status: 500 }
    );
  }
}

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const auth = await getAuthorizedListing(id);
  if (auth.response || !auth.user) {
    return auth.response!;
  }

  let trainingScope: "global" | "listing_local" = "global";
  try {
    const body = await req.json();
    if (body?.trainingScope === "listing_local") {
      trainingScope = "listing_local";
    }
  } catch {
    // Empty body is fine; default to global.
  }

  const admin = getSupabaseAdmin();
  const report = await getLatestWritableReport(id);

  if (!report) {
    return NextResponse.json(
      {
        error: "ML forecast is unavailable.",
        details:
          "This listing does not have a ready live-analysis report yet. Run a nightly or custom analysis first.",
      },
      { status: 409 }
    );
  }

  const existingMlForecast = asObject(report.result_summary?.mlForecast);
  const existingStatus =
    typeof existingMlForecast?.status === "string" ? existingMlForecast.status : null;
  if (
    existingMlForecast &&
    (existingStatus === "queued" || existingStatus === "running")
  ) {
    return NextResponse.json({
      run: normalizeMlForecastRunRow({
        ...existingMlForecast,
        id: existingMlForecast.jobId ?? existingMlForecast.id ?? report.id,
        reportId: report.id,
        createdAt: existingMlForecast.createdAt ?? report.created_at,
        completedAt: existingMlForecast.completedAt ?? report.completed_at,
      }),
    });
  }

  const startedAt = new Date().toISOString();
  const jobId = randomUUID();
  const queuedPayload = buildMlForecastPayload({
    reportId: report.id,
    jobId,
    createdAt: startedAt,
    completedAt: null,
    status: "queued",
    trainingScope,
    modelMode: null,
    nSamples: null,
    generatedAt: null,
    errorMessage: null,
    metrics: null,
    predictions: [],
  });

  const { data: updated, error: updateError } = await admin
    .from("pricing_reports")
    .update({
      result_summary: mergeSummaryWithMlForecast(
        report.result_summary,
        queuedPayload
      ),
    })
    .eq("id", report.id)
    .select("id, created_at, completed_at, result_summary")
    .single();

  if (updateError || !updated) {
    return NextResponse.json(
      {
        error: "Failed to persist queued ML forecast.",
        details: updateError?.message ?? null,
      },
      { status: 500 }
    );
  }

  const updatedSummary = asObject(updated.result_summary);
  const updatedMlForecast = asObject(updatedSummary?.mlForecast);

  return NextResponse.json({
    run: normalizeMlForecastRunRow({
      ...(updatedMlForecast ?? queuedPayload),
      id: updatedMlForecast?.jobId ?? jobId,
      jobId,
      reportId: updated.id,
      createdAt:
        updatedMlForecast?.createdAt ??
        (typeof updated.created_at === "string" ? updated.created_at : null) ??
        startedAt,
      completedAt: updatedMlForecast?.completedAt ?? null,
    }),
  });
}
