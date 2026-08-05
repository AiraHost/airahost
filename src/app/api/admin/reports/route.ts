/**
 * GET /api/admin/reports
 *
 * Recent pricing_reports across all users, for the /admin dashboard refresh
 * button.  Requires the admin session cookie; reads with the service role key
 * so RLS does not hide other users' reports.
 */

import { NextRequest, NextResponse } from "next/server";
import { getSupabaseAdmin } from "@/lib/supabase";
import { ADMIN_COOKIE_NAME, verifyAdminSessionToken } from "@/lib/adminAuth";
import { ADMIN_REPORT_COLUMNS, ADMIN_REPORT_LIMIT } from "@/lib/adminReports";

export async function GET(request: NextRequest) {
  if (!verifyAdminSessionToken(request.cookies.get(ADMIN_COOKIE_NAME)?.value)) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const supabase = getSupabaseAdmin();
  const { data, error } = await supabase
    .from("pricing_reports")
    .select(ADMIN_REPORT_COLUMNS)
    .order("created_at", { ascending: false })
    .limit(ADMIN_REPORT_LIMIT);

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ reports: data ?? [] });
}
