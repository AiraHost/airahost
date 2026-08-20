import { comparablesFound, type PricingReportRow } from "@/lib/adminReports";
import { StatusBadge } from "./StatusBadge";

function formatGenerationTime(ms: number | null): string {
  if (ms === null) return "—";
  return `${(ms / 1000).toFixed(2)}s`;
}

function formatCreatedAt(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function ReportsTable({ reports }: { reports: PricingReportRow[] }) {
  return (
    <div className="admin-panel overflow-hidden rounded-2xl">
      <div className="admin-scrollbar overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-base-700/60 text-xs uppercase tracking-wide text-base-400">
              <th className="px-5 py-4 font-medium">Listing</th>
              <th className="px-5 py-4 font-medium">Comparables Found</th>
              <th className="px-5 py-4 font-medium">Generation Time</th>
              <th className="px-5 py-4 font-medium">Status</th>
              <th className="px-5 py-4 font-medium">Created At</th>
            </tr>
          </thead>
          <tbody>
            {reports.map((report) => (
              <tr
                key={report.id}
                className="border-b border-base-800/80 transition-colors last:border-0 hover:bg-base-800/40"
              >
                <td className="px-5 py-4 font-medium text-base-100">
                  {report.input_address ?? <span className="text-base-400">Unknown</span>}
                </td>
                <td className="px-5 py-4 text-base-200">
                  {comparablesFound(report) ?? <span className="text-base-400">—</span>}
                </td>
                <td className="px-5 py-4 text-base-200">
                  {formatGenerationTime(report.generation_time_ms)}
                </td>
                <td className="px-5 py-4">
                  <StatusBadge status={report.status} />
                </td>
                {/* Server and browser time zones differ — the browser value wins. */}
                <td className="px-5 py-4 text-base-400" suppressHydrationWarning>
                  {formatCreatedAt(report.created_at)}
                </td>
              </tr>
            ))}
            {reports.length === 0 && (
              <tr>
                <td colSpan={5} className="px-5 py-10 text-center text-base-400">
                  No reports yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
