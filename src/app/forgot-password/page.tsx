"use client";

import { useState } from "react";
import Link from "next/link";
import { Card } from "@/components/Card";
import { Button } from "@/components/Button";
import { getSupabaseBrowser } from "@/lib/supabase";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError("");

    const supabase = getSupabaseBrowser();
    const { error: resetError } = await supabase.auth.resetPasswordForEmail(email.trim(), {
      redirectTo: `${window.location.origin}/reset-password`,
    });

    if (resetError) {
      setError(resetError.message);
    } else {
      setSent(true);
    }
    setLoading(false);
  }

  return (
    <div className="mx-auto max-w-md px-6 py-16">
      <h1 className="mb-2 text-center text-3xl font-bold">Reset your password</h1>
      <p className="mb-8 text-center text-muted">
        Enter your account email and we&apos;ll send you a reset link.
      </p>

      <Card>
        {sent ? (
          <div className="space-y-4 text-center">
            <p className="rounded-xl bg-green-50 p-4 text-sm text-success">
              If an account exists for <strong>{email}</strong>, a reset link is on its way. Check your inbox (and spam folder).
            </p>
            <p className="text-sm text-muted">
              Didn&apos;t get it?{" "}
              <button
                onClick={() => setSent(false)}
                className="font-medium text-accent hover:underline"
              >
                Try again
              </button>
            </p>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="mb-1.5 block text-sm font-medium">Email</label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                required
                autoComplete="email"
                className="input w-full"
              />
            </div>

            {error && (
              <p className="rounded-xl bg-red-50 p-3 text-sm text-warning">{error}</p>
            )}

            <Button type="submit" disabled={loading} className="w-full">
              {loading ? "Sending..." : "Send reset link"}
            </Button>
          </form>
        )}

        <div className="mt-4 text-center text-sm text-muted">
          <Link href="/login" className="font-medium text-accent hover:underline">
            Back to sign in
          </Link>
        </div>
      </Card>
    </div>
  );
}
