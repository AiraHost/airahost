import { LayoutDashboard, Sparkles } from "lucide-react";

export function Sidebar() {
  return (
    <aside className="glass-panel flex h-screen w-64 shrink-0 flex-col justify-between rounded-none border-y-0 border-l-0 p-5">
      <div>
        <div className="mb-8 flex items-center gap-2.5 px-1">
          <div className="glow-brand flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-brand-500 to-accent-500">
            <Sparkles className="h-5 w-5 text-white" />
          </div>
          <span className="font-display text-lg font-semibold tracking-tight">AiraHost</span>
        </div>

        <nav className="space-y-1">
          <div className="flex items-center gap-3 rounded-xl bg-gradient-to-r from-brand-500/20 to-transparent px-3 py-2.5 text-sm font-medium text-base-100 ring-1 ring-inset ring-brand-500/20">
            <LayoutDashboard className="h-4 w-4 text-brand-400" />
            Dashboard
          </div>
        </nav>
      </div>
    </aside>
  );
}
