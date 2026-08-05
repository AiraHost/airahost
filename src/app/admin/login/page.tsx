import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { ADMIN_COOKIE_NAME, isAdminConfigured, verifyAdminSessionToken } from "@/lib/adminAuth";
import { AdminLoginForm } from "@/components/admin/AdminLoginForm";

export const dynamic = "force-dynamic";

export default async function AdminLoginPage() {
  const cookieStore = await cookies();
  if (verifyAdminSessionToken(cookieStore.get(ADMIN_COOKIE_NAME)?.value)) {
    redirect("/admin");
  }

  return (
    <div className="flex h-full items-center justify-center bg-[radial-gradient(circle_at_top,_var(--color-base-900),_var(--color-base-950)_65%)] p-6">
      {isAdminConfigured() ? (
        <AdminLoginForm />
      ) : (
        <div className="admin-panel max-w-sm rounded-2xl p-6 text-sm text-base-200">
          <p className="font-display text-lg font-semibold text-base-100">Admin not configured</p>
          <p className="mt-2 text-base-400">
            Set <code className="text-base-200">ADMIN_PASSWORD</code> in the environment and restart
            the server.
          </p>
        </div>
      )}
    </div>
  );
}
