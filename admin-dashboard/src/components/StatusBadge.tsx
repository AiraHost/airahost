import type { ReportStatus } from "../lib/types";

const STYLES: Record<ReportStatus, string> = {
  ready: "bg-success-500/15 text-success-400 ring-success-500/30",
  queued: "bg-warning-500/15 text-warning-400 ring-warning-500/30",
  error: "bg-danger-500/15 text-danger-400 ring-danger-500/30",
};

export function StatusBadge({ status }: { status: ReportStatus }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset ${STYLES[status]}`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {status}
    </span>
  );
}
