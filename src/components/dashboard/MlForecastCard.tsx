import { useState } from "react";

import { Card } from "@/components/Card";
import type { MlForecastRun } from "@/lib/mlSidecar";

interface Props {
  run: MlForecastRun | null;
  loading: boolean;
  running: boolean;
  error: string | null;
  onRun: () => void;
}

function formatMetric(value: number | null | undefined, digits = 3): string {
  return typeof value === "number" ? value.toFixed(digits) : "-";
}

function formatPercent(value: number | null | undefined, digits = 1): string {
  return typeof value === "number" ? `${(value * 100).toFixed(digits)}%` : "-";
}

function formatCurrency(value: number | null | undefined): string {
  return typeof value === "number" ? `$${value.toFixed(0)}` : "-";
}

function formatConfidenceScore(value: number | null | undefined): string {
  return typeof value === "number" ? `${Math.round(value)}/100` : "-";
}

function formatBand(value: string | null | undefined): string {
  if (!value) return "-";
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function confidenceTone(value: string | null | undefined): string {
  if (value === "high") return "border-emerald-200 bg-emerald-50 text-emerald-700";
  if (value === "medium") return "border-amber-200 bg-amber-50 text-amber-700";
  return "border-rose-200 bg-rose-50 text-rose-700";
}

export function MlForecastCard({
  run,
  loading,
  running,
  error,
  onRun,
}: Props) {
  const [visibleDays, setVisibleDays] = useState<7 | 30>(7);
  const isBusy = running || run?.status === "queued" || run?.status === "running";
  const tone =
    run?.status === "error"
      ? "border-rose-200 bg-rose-50/40"
      : "border-sky-200 bg-sky-50/30";

  return (
    <Card className={tone}>
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-sky-700/80">
            Experimental ML Forecast
          </p>
          <h2 className="mt-1 text-lg font-semibold text-foreground/85">
            Raw-market model preview for this listing
          </h2>
          <p className="mt-1 text-sm text-foreground/55">
            Trains from Supabase <code>market_price_observations</code>. This does not
            overwrite the nightly market board or your formal suggested price.
          </p>
          {run?.explanation?.summary && (
            <div className="mt-3 rounded-2xl border border-sky-100 bg-white/85 px-4 py-3">
              <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-sky-700/75">
                Why These Prices
              </p>
              <p className="mt-1 text-sm leading-6 text-foreground/65">
                {run.explanation.summary}
              </p>
              {run.explanation.topDrivers.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {run.explanation.topDrivers.map((driver) => (
                    <span
                      key={driver}
                      className="rounded-full border border-sky-200 bg-sky-50 px-2.5 py-1 text-[11px] font-medium text-sky-700"
                    >
                      {driver}
                    </span>
                  ))}
                </div>
              )}
              {run.explanation.featureHighlights.length > 0 && (
                <div className="mt-2 space-y-1">
                  {run.explanation.featureHighlights.map((highlight) => (
                    <p key={highlight} className="text-xs text-foreground/50">
                      {highlight}
                    </p>
                  ))}
                </div>
              )}
              {run.explanation.displayNote && (
                <p className="mt-2 text-xs text-foreground/45">
                  {run.explanation.displayNote}
                </p>
              )}
            </div>
          )}
        </div>

        <button
          type="button"
          onClick={onRun}
          disabled={isBusy || loading}
          className="shrink-0 rounded-xl bg-sky-700 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-sky-800 disabled:opacity-40"
        >
          {isBusy ? "Queued..." : "Run ML forecast"}
        </button>
      </div>

      {error && (
        <div className="mt-4 rounded-xl border border-rose-200 bg-white px-4 py-3 text-sm text-rose-700">
          {error}
        </div>
      )}

      {!run && !loading && !error && (
        <div className="mt-4 rounded-xl border border-dashed border-border bg-white px-4 py-5 text-sm text-foreground/50">
          No ML run yet. Start one to generate a 30-day experimental forecast from the stored market observations.
        </div>
      )}

      {run && (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <span className="rounded-full border border-current/10 bg-white px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-foreground/60">
              {run.status}
            </span>
            {run.trainingScope && (
              <span className="rounded-full border border-sky-200 bg-white px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-sky-700">
                {run.trainingScope}
              </span>
            )}
            {run.modelMode && (
              <span className="rounded-full border border-sky-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-sky-700">
                {run.modelMode}
              </span>
            )}
            {run.generatedAt && (
              <span className="text-xs text-foreground/45">
                Generated {new Date(run.generatedAt).toLocaleString()}
              </span>
            )}
          </div>

          {run.status === "error" && run.errorMessage && (
            <div className="mt-4 rounded-xl border border-rose-200 bg-white px-4 py-3 text-sm text-rose-700">
              {run.errorMessage}
            </div>
          )}

          {run.metrics?.modelConfidenceScore != null && (
            <div className="mt-4 rounded-2xl border border-border/70 bg-white p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-[11px] uppercase tracking-[0.14em] text-foreground/35">
                    Model confidence
                  </p>
                  <p className="mt-1 text-2xl font-semibold text-foreground/85">
                    {formatConfidenceScore(run.metrics.modelConfidenceScore)}
                  </p>
                </div>
                {run.metrics.modelConfidenceBand && (
                  <span
                    className={`rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-[0.14em] ${confidenceTone(run.metrics.modelConfidenceBand)}`}
                  >
                    {formatBand(run.metrics.modelConfidenceBand)}
                  </span>
                )}
              </div>
              {run.metrics.modelConfidenceReasons.length > 0 && (
                <p className="mt-2 text-xs text-foreground/50">
                  {run.metrics.modelConfidenceReasons.join(" | ")}
                </p>
              )}
            </div>
          )}

          {run.metrics && (
            <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-5">
              <div className="rounded-2xl border border-border/70 bg-white p-4">
                <p className="text-[11px] text-foreground/35">Q2</p>
                <p className="mt-1 text-lg font-semibold text-foreground/80">
                  {formatMetric(run.metrics.q2)}
                </p>
              </div>
              <div className="rounded-2xl border border-border/70 bg-white p-4">
                <p className="text-[11px] text-foreground/35">R2</p>
                <p className="mt-1 text-lg font-semibold text-foreground/80">
                  {formatMetric(run.metrics.r2)}
                </p>
              </div>
              <div className="rounded-2xl border border-border/70 bg-white p-4">
                <p className="text-[11px] text-foreground/35">MAPE</p>
                <p className="mt-1 text-lg font-semibold text-foreground/80">
                  {formatPercent(run.metrics.mape)}
                </p>
              </div>
              <div className="rounded-2xl border border-border/70 bg-white p-4">
                <p className="text-[11px] text-foreground/35">MAE</p>
                <p className="mt-1 text-lg font-semibold text-foreground/80">
                  {formatCurrency(run.metrics.mae)}
                </p>
              </div>
              <div className="rounded-2xl border border-border/70 bg-white p-4">
                <p className="text-[11px] text-foreground/35">Training rows</p>
                <p className="mt-1 text-lg font-semibold text-foreground/80">
                  {run.nSamples?.toLocaleString() ?? "-"}
                </p>
              </div>
            </div>
          )}

          {run.predictions.length > 0 && (
            <div className="mt-4 overflow-hidden rounded-2xl border border-border/70 bg-white">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/70 px-4 py-3">
                <div>
                  <p className="text-sm font-semibold text-foreground/80">
                    Forecast horizon
                  </p>
                  <p className="mt-0.5 text-xs text-foreground/45">
                    The model generates {run.explanation?.horizonDays ?? run.predictions.length} days.
                    This view defaults to 7 days for readability.
                  </p>
                </div>
                {run.predictions.length > 7 && (
                  <div className="inline-flex rounded-xl border border-border/70 bg-slate-50 p-1">
                    <button
                      type="button"
                      onClick={() => setVisibleDays(7)}
                      className={`rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors ${
                        visibleDays === 7
                          ? "bg-white text-foreground shadow-sm"
                          : "text-foreground/45 hover:text-foreground/70"
                      }`}
                    >
                      7 days
                    </button>
                    <button
                      type="button"
                      onClick={() => setVisibleDays(30)}
                      className={`rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors ${
                        visibleDays === 30
                          ? "bg-white text-foreground shadow-sm"
                          : "text-foreground/45 hover:text-foreground/70"
                      }`}
                    >
                      30 days
                    </button>
                  </div>
                )}
              </div>
              <div className="border-b border-border/70 px-4 py-3">
                <p className="mt-0.5 text-xs text-foreground/45">
                  Experimental ML output only. Keep using the nightly market board as the product source of truth.
                </p>
              </div>
              <div className="divide-y divide-border/70">
                {run.predictions.slice(0, visibleDays).map((prediction) => (
                  <div
                    key={prediction.date}
                    className="flex items-center justify-between px-4 py-3 text-sm"
                  >
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="font-medium text-foreground/80">
                          {new Date(prediction.date).toLocaleDateString(undefined, {
                            month: "short",
                            day: "numeric",
                            weekday: "short",
                          })}
                        </p>
                        {prediction.predictionConfidenceBand && (
                          <span
                            className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] ${confidenceTone(prediction.predictionConfidenceBand)}`}
                          >
                            {formatBand(prediction.predictionConfidenceBand)}
                          </span>
                        )}
                      </div>
                      <p className="mt-0.5 text-xs text-foreground/45">
                        {prediction.isHoliday
                          ? "Holiday"
                          : prediction.isWeekend
                            ? "Weekend"
                            : "Weekday"}
                        {prediction.supportCount != null
                          ? ` | ${prediction.supportCount} similar rows`
                          : ""}
                        {prediction.guardrailApplied ? " | guardrail applied" : ""}
                      </p>
                      {prediction.confidenceReasons.length > 0 && (
                        <p className="mt-0.5 text-xs text-foreground/40">
                          {prediction.confidenceReasons.join(" | ")}
                        </p>
                      )}
                    </div>
                    <div className="text-right">
                      <p className="font-semibold text-foreground/80">
                        {formatCurrency(prediction.predictedPrice)}
                      </p>
                      {prediction.interval80Low != null &&
                        prediction.interval80High != null && (
                          <p className="mt-0.5 text-xs text-foreground/45">
                            80% range {formatCurrency(prediction.interval80Low)}-
                            {formatCurrency(prediction.interval80High)}
                          </p>
                        )}
                      {prediction.predictionConfidenceScore != null && (
                        <p className="mt-0.5 text-xs text-foreground/45">
                          confidence {formatConfidenceScore(prediction.predictionConfidenceScore)}
                        </p>
                      )}
                      {prediction.predictedPriceRaw !== prediction.predictedPrice && (
                        <p className="mt-0.5 text-xs text-foreground/45">
                          raw {formatCurrency(prediction.predictedPriceRaw)}
                        </p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </Card>
  );
}
