"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { Card } from "@/components/Card";
import { Button } from "@/components/Button";
import { getSupabaseBrowser } from "@/lib/supabase";

export default function ResetPasswordPage() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [sessionReady, setSessionReady] = useState<boolean | null>(null);

  useEffect(() => {
    // Supports both direct recovery links and callback-based flows.
    const supabase = getSupabaseBrowser();
    const init = async () => {
      const url = new URL(window.location.href);
      const code = url.searchParams.get("code");
      const tokenHash = url.searchParams.get("token_hash");
      const type = url.searchParams.get("type");

      if (code) {
        const { error: exchangeError } = await supabase.auth.exchangeCodeForSession(code);
        if (exchangeError) {
          // Some recovery links arrive as ?code=... without type.
          // In that case, code may actually be a recovery token hash.
          await supabase.auth.verifyOtp({
            token_hash: code,
            type: "recovery",
          });
        }
      } else if (tokenHash && type) {
        await supabase.auth.verifyOtp({
          token_hash: tokenHash,
          type: type as "recovery",
        });
      }

      const { data: { session } } = await supabase.auth.getSession();
      if (!session) {
        router.replace("/login?error=auth");
        return;
      }
      setSessionReady(true);
    };

    init();
  }, [router]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }
    setLoading(true);
    setError("");

    const supabase = getSupabaseBrowser();
    const { error: updateError } = await supabase.auth.updateUser({ password });

    if (updateError) {
      setError(updateError.message);
    } else {
      setMessage("Password updated. Taking you to your dashboard…");
      setTimeout(() => {
        router.push("/dashboard");
        router.refresh();
      }, 1500);
    }
    setLoading(false);
  }

  if (sessionReady === null) {
    return (
      <div className="mx-auto max-w-md px-6 py-16 text-center text-sm text-muted">
        Verifying link…
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-md px-6 py-16">
      <h1 className="mb-2 text-center text-3xl font-bold">Set new password</h1>
      <p className="mb-8 text-center text-muted">
        Choose a strong password for your AiraHost account.
      </p>

      <Card>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="mb-1.5 block text-sm font-medium">New password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="At least 6 characters"
              required
              minLength={6}
              autoComplete="new-password"
              className="input w-full"
            />
          </div>

          <div>
            <label className="mb-1.5 block text-sm font-medium">Confirm password</label>
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="Repeat your new password"
              required
              minLength={6}
              autoComplete="new-password"
              className="input w-full"
            />
          </div>

          {error && (
            <p className="rounded-xl bg-red-50 p-3 text-sm text-warning">{error}</p>
          )}
          {message && (
            <p className="rounded-xl bg-green-50 p-3 text-sm text-success">{message}</p>
          )}

          <Button type="submit" disabled={loading || !!message} className="w-full">
            {loading ? "Updating…" : "Update password"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
