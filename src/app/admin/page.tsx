import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { getSupabaseAdmin } from "@/lib/supabase";
import { ADMIN_COOKIE_NAME, verifyAdminSessionToken } from "@/lib/adminAuth";
import {
  ADMIN_REPORT_COLUMNS,
  ADMIN_REPORT_LIMIT,
  type PricingReportRow,
} from "@/lib/adminReports";
import { AdminDashboard } from "@/components/admin/AdminDashboard";

export const dynamic = "force-dynamic";

export default async function AdminPage() {
  const cookieStore = await cookies();
  if (!verifyAdminSessionToken(cookieStore.get(ADMIN_COOKIE_NAME)?.value)) {
    redirect("/admin/login");
  }

  let reports: PricingReportRow[] = [];
  let error: string | null = null;
  try {
    const supabase = getSupabaseAdmin();
    const { data, error: fetchError } = await supabase
      .from("pricing_reports")
      .select(ADMIN_REPORT_COLUMNS)
      .order("created_at", { ascending: false })
      .limit(ADMIN_REPORT_LIMIT);
    if (fetchError) error = fetchError.message;
    else reports = (data as PricingReportRow[] | null) ?? [];
  } catch (err) {
    error = err instanceof Error ? err.message : "Failed to load reports.";
  }

  return <AdminDashboard initialReports={reports} initialError={error} />;
}
