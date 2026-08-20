"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Sparkles } from "lucide-react";

export function AdminLoginForm() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const response = await fetch("/api/admin/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (!response.ok) {
        const body = (await response.json()) as { error?: string };
        setError(body.error ?? "Sign in failed.");
        return;
      }
      router.replace("/admin");
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="admin-panel w-full max-w-sm rounded-2xl p-7">
      <div className="mb-6 flex items-center gap-2.5">
        <div className="admin-glow-brand flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-brand-500 to-accent-500">
          <Sparkles className="h-5 w-5 text-white" />
        </div>
        <span className="font-display text-lg font-semibold tracking-tight">AiraHost Admin</span>
      </div>

      <label htmlFor="admin-password" className="text-sm font-medium text-base-200">
        Password
      </label>
      <input
        id="admin-password"
        type="password"
        autoComplete="current-password"
        autoFocus
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        className="mt-2 w-full rounded-xl border border-base-700 bg-base-900/70 px-3 py-2.5 text-sm text-base-100 outline-none transition-colors focus:border-brand-500"
      />

      {error && <p className="mt-3 text-sm text-danger-400">{error}</p>}

      <button
        type="submit"
        disabled={loading || !password}
        className="mt-5 w-full rounded-xl bg-gradient-to-r from-brand-500 to-brand-600 px-4 py-2.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
      >
        {loading ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
