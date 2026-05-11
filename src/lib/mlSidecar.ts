function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asObject(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function asStringArray(value: unknown): string[] {
  return asArray<unknown>(value).filter(
    (item): item is string => typeof item === "string" && item.trim().length > 0
  );
}

function readNumber(
  source: Record<string, unknown> | null,
  ...keys: string[]
): number | null {
  if (!source) return null;
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
  }
  return null;
}

function readString(
  source: Record<string, unknown> | null,
  ...keys: string[]
): string | null {
  if (!source) return null;
  for (const key of keys) {
    const value = asString(source[key]);
    if (value) {
      return value;
    }
  }
  return null;
}

export interface MlForecastMetrics {
  mae: number | null;
  maeStd: number | null;
  mape: number | null;
  q2: number | null;
  r2: number | null;
  r2Std: number | null;
  aeP50: number | null;
  aeP80: number | null;
  aeP95: number | null;
  apeP50: number | null;
  apeP80: number | null;
  apeP95: number | null;
  trainingTimeSeconds: number | null;
  nSamples: number | null;
  cvStrategy: string | null;
  modelConfidenceScore: number | null;
  modelConfidenceBand: string | null;
  modelConfidenceReasons: string[];
}

export interface MlForecastExplanation {
  summary: string | null;
  topDrivers: string[];
  featureHighlights: string[];
  horizonDays: number | null;
  displayNote: string | null;
}

export interface MlForecastPrediction {
  date: string;
  predictedPrice: number | null;
  predictedPriceRaw: number | null;
  guardrailApplied: boolean;
  isWeekend: boolean;
  isHoliday: boolean;
  predictionConfidenceScore: number | null;
  predictionConfidenceBand: string | null;
  supportCount: number | null;
  interval80Low: number | null;
  interval80High: number | null;
  interval95Low: number | null;
  interval95High: number | null;
  confidenceReasons: string[];
}

export interface MlForecastManifest {
  generatedAt: string | null;
  listingId: string;
  listingName: string | null;
  trainingScope: string | null;
  trainedNow: boolean | null;
  modelMode: string | null;
  nSamples: number | null;
  startDate: string | null;
  horizon: number | null;
  metrics: MlForecastMetrics | null;
  explanation: MlForecastExplanation | null;
  predictions: MlForecastPrediction[];
  raw: Record<string, unknown>;
}

export interface MlForecastRun {
  id: string;
  status: "queued" | "running" | "ready" | "error";
  trainingScope: string | null;
  modelMode: string | null;
  nSamples: number | null;
  generatedAt: string | null;
  createdAt: string | null;
  completedAt: string | null;
  errorMessage: string | null;
  metrics: MlForecastMetrics | null;
  explanation: MlForecastExplanation | null;
  predictions: MlForecastPrediction[];
}

function normalizeExplanation(raw: unknown): MlForecastExplanation | null {
  const explanation = asObject(raw);
  if (!explanation) return null;

  return {
    summary: readString(explanation, "summary"),
    topDrivers: asStringArray(
      explanation.topDrivers ?? explanation.top_drivers
    ),
    featureHighlights: asStringArray(
      explanation.featureHighlights ?? explanation.feature_highlights
    ),
    horizonDays: readNumber(explanation, "horizonDays", "horizon_days"),
    displayNote: readString(explanation, "displayNote", "display_note"),
  };
}

function normalizeMetrics(raw: unknown): MlForecastMetrics | null {
  const metrics = asObject(raw);
  if (!metrics) return null;

  return {
    mae: readNumber(metrics, "mae"),
    maeStd: readNumber(metrics, "maeStd", "mae_std"),
    mape: readNumber(metrics, "mape"),
    q2: readNumber(metrics, "q2"),
    r2: readNumber(metrics, "r2"),
    r2Std: readNumber(metrics, "r2Std", "r2_std"),
    aeP50: readNumber(metrics, "aeP50", "ae_p50"),
    aeP80: readNumber(metrics, "aeP80", "ae_p80"),
    aeP95: readNumber(metrics, "aeP95", "ae_p95"),
    apeP50: readNumber(metrics, "apeP50", "ape_p50"),
    apeP80: readNumber(metrics, "apeP80", "ape_p80"),
    apeP95: readNumber(metrics, "apeP95", "ape_p95"),
    trainingTimeSeconds: readNumber(
      metrics,
      "trainingTimeSeconds",
      "training_time_seconds"
    ),
    nSamples: readNumber(metrics, "nSamples", "n_samples"),
    cvStrategy: readString(metrics, "cvStrategy", "cv_strategy"),
    modelConfidenceScore: readNumber(
      metrics,
      "modelConfidenceScore",
      "model_confidence_score"
    ),
    modelConfidenceBand: readString(
      metrics,
      "modelConfidenceBand",
      "model_confidence_band"
    ),
    modelConfidenceReasons: asStringArray(
      metrics.modelConfidenceReasons ?? metrics.model_confidence_reasons
    ),
  };
}

function normalizePrediction(raw: unknown): MlForecastPrediction | null {
  const prediction = asObject(raw);
  if (!prediction) return null;

  const date = readString(prediction, "date");
  if (!date) return null;

  return {
    date,
    predictedPrice: readNumber(
      prediction,
      "predictedPrice",
      "predicted_price"
    ),
    predictedPriceRaw: readNumber(
      prediction,
      "predictedPriceRaw",
      "predicted_price_raw"
    ),
    guardrailApplied:
      prediction?.guardrailApplied === true ||
      prediction?.guardrail_applied === true,
    isWeekend:
      prediction?.isWeekend === true || prediction?.is_weekend === true,
    isHoliday:
      prediction?.isHoliday === true || prediction?.is_holiday === true,
    predictionConfidenceScore: readNumber(
      prediction,
      "predictionConfidenceScore",
      "prediction_confidence_score"
    ),
    predictionConfidenceBand: readString(
      prediction,
      "predictionConfidenceBand",
      "prediction_confidence_band"
    ),
    supportCount: readNumber(prediction, "supportCount", "support_count"),
    interval80Low: readNumber(prediction, "interval80Low", "interval80_low"),
    interval80High: readNumber(prediction, "interval80High", "interval80_high"),
    interval95Low: readNumber(prediction, "interval95Low", "interval95_low"),
    interval95High: readNumber(prediction, "interval95High", "interval95_high"),
    confidenceReasons: asStringArray(
      prediction?.confidenceReasons ?? prediction?.confidence_reasons
    ),
  };
}

export function parseMlForecastManifest(raw: unknown): MlForecastManifest {
  const manifest = asObject(raw);
  if (!manifest) {
    throw new Error("ML sidecar manifest is not a JSON object.");
  }

  const listingId = asString(manifest.listing_id);
  if (!listingId) {
    throw new Error("ML sidecar manifest is missing listing_id.");
  }

  return {
    generatedAt: asString(manifest.generated_at),
    listingId,
    listingName: asString(manifest.listing_name),
    trainingScope: asString(manifest.training_scope),
    trainedNow:
      typeof manifest.trained_now === "boolean" ? manifest.trained_now : null,
    modelMode: asString(manifest.model_mode),
    nSamples: asNumber(manifest.n_samples),
    startDate: asString(manifest.start_date),
    horizon: asNumber(manifest.horizon),
    metrics: normalizeMetrics(manifest.metrics),
    explanation: normalizeExplanation(manifest.explanation),
    predictions: asArray<unknown>(manifest.predictions)
      .map(normalizePrediction)
      .filter((prediction): prediction is MlForecastPrediction => prediction !== null),
    raw: manifest,
  };
}

export function normalizeMlForecastRunRow(row: Record<string, unknown>): MlForecastRun {
  const statusValue = readString(row, "status");
  const status =
    statusValue === "queued" ||
    statusValue === "running" ||
    statusValue === "ready" ||
    statusValue === "error"
      ? statusValue
      : "error";

  return {
    id: readString(row, "id", "reportId", "report_id") ?? "",
    status,
    trainingScope: readString(row, "trainingScope", "training_scope"),
    modelMode: readString(row, "modelMode", "model_mode"),
    nSamples: readNumber(row, "nSamples", "n_samples"),
    generatedAt: readString(row, "generatedAt", "generated_at"),
    createdAt: readString(row, "createdAt", "created_at"),
    completedAt: readString(row, "completedAt", "completed_at"),
    errorMessage: readString(row, "errorMessage", "error_message"),
    metrics: normalizeMetrics(
      row.metrics ?? row.metrics_json ?? null
    ),
    explanation: normalizeExplanation(row.explanation ?? null),
    predictions: asArray<unknown>(row.predictions ?? row.predictions_json)
      .map(normalizePrediction)
      .filter((prediction): prediction is MlForecastPrediction => prediction !== null),
  };
}

