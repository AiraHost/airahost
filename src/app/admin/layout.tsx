import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "AiraHost Admin",
  robots: { index: false, follow: false },
};

/**
 * The admin dashboard is a full-screen dark app — it covers the marketing
 * header/footer rendered by the root layout instead of nesting inside them.
 */
export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="admin-root fixed inset-0 z-50 overflow-hidden bg-base-950 text-base-100">
      {children}
    </div>
  );
}
