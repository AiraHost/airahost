import type { ReactNode } from "react";

interface KpiCardProps {
  label: string;
  value: string;
  icon: ReactNode;
  accent?: "brand" | "accent" | "success";
}

const ACCENT_STYLES: Record<NonNullable<KpiCardProps["accent"]>, string> = {
  brand: "from-brand-500/25 to-brand-600/5 text-brand-400",
  accent: "from-accent-500/25 to-accent-500/5 text-accent-400",
  success: "from-success-500/25 to-success-500/5 text-success-400",
};

export function KpiCard({ label, value, icon, accent = "brand" }: KpiCardProps) {
  return (
    <div className="glass-panel relative overflow-hidden rounded-2xl p-5 transition-transform duration-300 hover:-translate-y-0.5">
      <div
        className={`absolute -right-6 -top-6 h-28 w-28 rounded-full bg-gradient-to-br opacity-40 blur-2xl ${ACCENT_STYLES[accent]}`}
      />
      <div className="relative flex items-start justify-between">
        <div>
          <p className="text-sm font-medium text-base-400">{label}</p>
          <p className="mt-2 font-display text-3xl font-semibold tracking-tight text-base-100">
            {value}
          </p>
        </div>
        <div className={`rounded-xl bg-gradient-to-br p-2.5 ${ACCENT_STYLES[accent]}`}>{icon}</div>
      </div>
    </div>
  );
}
