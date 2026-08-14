# Runbook — Airbnb admission control, structured events, and diagnostic artifacts

Audience: whoever is on call when the worker starts producing 503s, empty
comparable pools, or unexplained Playwright escalations.

Three things changed and this runbook covers all of them:

1. **One admission policy** (`worker/core/admission.py`) governs every outbound
   Airbnb request and adapts to 429/503/blocks.
2. **One structured event stream** (`worker/logs/worker.jsonl`) records every
   attempt, fallback, retry, cooldown and limit change.
3. **A diagnostic artifact store** (`worker/logs/scraper-artifacts/`) keeps the
   redacted response body that a blocked/degraded decision was made on.

---

## 1. Configuration

All values are read once at process start, clamped to safe bounds, and logged
at startup as `[admission] initialized …` plus an `admission_configured` event.
Nothing here is a secret, so the startup line is safe to paste into a ticket.

**These are ceilings, not operating points.** What protects against overload is
the adaptive machinery — multiplicative decrease, cooldown, circuit — reacting
to what Airbnb actually returns. Lowering the ceiling instead is not extra
safety: it is a fixed tax paid even when every response is a 200. An earlier
build shipped 2/s and 4 concurrent and cost ~1,340s of cumulative limiter wait
across a run in which Airbnb returned HTTP 200 to every single request. If you
are tempted to lower these, check `direct_http_overloaded` in the event log
first — if it is empty, throughput is not the problem.

### Admission policy

| Variable | Default | Meaning |
|---|---|---|
| `AIRBNB_MAX_START_RATE_PER_SEC` | `4.0` | Ceiling on request *starts* per second, across all classes. Clamped to `[0.05, 20]`. |
| `AIRBNB_MIN_START_RATE_PER_SEC` | `0.2` | Floor the adaptive backoff will not go below. |
| `AIRBNB_MAX_INFLIGHT_REQUESTS` | `8` | Aggregate concurrent Airbnb requests. Clamped to `[1, 32]`. |
| `AIRBNB_MIN_INFLIGHT_REQUESTS` | `1` | Floor for concurrency after backoff. |
| `AIRBNB_MAX_INFLIGHT_SEARCH` | `6` | Per-class cap, under the aggregate. |
| `AIRBNB_MAX_INFLIGHT_PDP` | `6` | Per-class cap, under the aggregate. |
| `AIRBNB_MAX_INFLIGHT_BROWSER` | `3` | Per-class cap for Playwright navigation. |
| `AIRBNB_MAX_INFLIGHT_SESSION_REFRESH` | `1` | Per-class cap for session refresh. |
| `AIRBNB_OVERLOAD_DECREASE_FACTOR` | `0.5` | Multiplicative reduction on 429/503/block. |
| `AIRBNB_RECOVERY_INTERVAL_SECONDS` | `30` | Minimum healthy window before any increase. |
| `AIRBNB_RECOVERY_SUCCESS_THRESHOLD` | `20` | Successes required in that window. |
| `AIRBNB_RATE_INCREASE_STEP` | `0.25` | Additive rate increase per recovery step (req/s). |
| `AIRBNB_BACKOFF_BASE_SECONDS` | `1.0` | Base of the exponential, full-jitter backoff. |
| `AIRBNB_BACKOFF_MAX_SECONDS` | `60` | Cap on any single backoff, including `Retry-After`. |
| `AIRBNB_CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive overload/block signals before the circuit opens. |
| `AIRBNB_CIRCUIT_COOLDOWN_SECONDS` | `120` | Initial open duration; doubles on a failed half-open probe. |
| `AIRBNB_CIRCUIT_MAX_COOLDOWN_SECONDS` | `900` | Ceiling on that doubling. |
| `AIRBNB_RETRY_BUDGET_PER_OPERATION` | `2` | Retries allowed for one logical operation. |
| `AIRBNB_RETRY_BUDGET_PER_REPORT` | `20` | Retries allowed across the whole report, all threads. |
| `AIRBNB_ADMISSION_INSTANCES` | `1` | **Set this to the number of worker processes running concurrently.** See §6. |
| `AIRBNB_RATE_LIMIT_DISABLED` | unset | Disables throttling entirely. Local debugging only. |

`AIRBNB_MIN_REQUEST_INTERVAL_MS` is still honoured for backwards compatibility:
when set and `AIRBNB_MAX_START_RATE_PER_SEC` is not, the rate ceiling is derived
as `1000 / interval_ms`.

### Event log

| Variable | Default | Meaning |
|---|---|---|
| `WORKER_EVENT_LOG_ENABLED` | `1` | Master switch for the structured sink. |
| `WORKER_EVENT_LOG_STDOUT` | `0` | Emit JSON to stdout instead of a file (for an external log agent). |
| `WORKER_EVENT_LOG_PATH` | `worker/logs/worker.jsonl` | File destination. |
| `WORKER_EVENT_LOG_MAX_BYTES` | `20971520` | Rotation size. |
| `WORKER_EVENT_LOG_BACKUPS` | `5` | Rotated files retained. |
| `WORKER_INSTANCE_ID` | `<host>:<lane>:<pid>` | Overrides the identity stamped on every event. |

### Artifacts

| Variable | Default | Meaning |
|---|---|---|
| `SCRAPER_ARTIFACT_CAPTURE_ENABLED` | `1` | Capture on error/fallback outcomes. |
| `SCRAPER_ARTIFACT_CAPTURE_FULL_PAYLOADS` | `0` | **Also** capture successful payloads. Local development only. |
| `SCRAPER_ARTIFACT_DIR` | `worker/logs/scraper-artifacts` | Destination root. |
| `SCRAPER_ARTIFACT_MAX_BYTES` | `262144` | Max stored bytes per artifact; larger bodies are truncated and flagged. |
| `SCRAPER_ARTIFACT_MAX_PER_REPORT` | `25` | Per-report cap. |
| `SCRAPER_ARTIFACT_MAX_TOTAL_BYTES` | `209715200` | Total retained bytes; oldest evicted first. |
| `SCRAPER_ARTIFACT_RETENTION_DAYS` | `7` | Age limit. |

---

## 2. Tailing events

Every event is one JSON object per line in `worker/logs/worker.jsonl`.

### All events for one report, in order

PowerShell:

```powershell
Get-Content worker\logs\worker.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.report_id -eq '<REPORT_ID>' } |
  Sort-Object ts |
  Format-Table ts, event, search_id, source, outcome, reason_code, status, elapsed_ms
```

POSIX:

```bash
jq -c 'select(.report_id == "<REPORT_ID>")' worker/logs/worker.jsonl |
  jq -s 'sort_by(.ts) | .[] | {ts, event, search_id, source, outcome, reason_code, status, elapsed_ms}'
```

### One logical search's full fallback chain

`search_id` is stable across the direct attempt, any HTML attempt and a
Playwright escalation; `attempt_id` is unique per network attempt.

```powershell
Get-Content worker\logs\worker.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.search_id -eq '<SEARCH_ID>' } |
  Sort-Object ts |
  Format-Table ts, event, attempt_id, attempt_number, source, fallback_from, fallback_reason, outcome
```

```bash
jq -c 'select(.search_id == "<SEARCH_ID>")' worker/logs/worker.jsonl
```

### Did a Playwright fallback actually succeed?

`search_id` tells you *that* the search escalated to Playwright; it does not by
itself say whether the escalation worked. For that, follow `attempt_id`:
`playwright_started` and its matching outcome — `playwright_captured_json`
(succeeded) or `playwright_failed` (did not) — share the same `attempt_id`.
`playwright_failed` carries `outcome` and `reason_code` explaining why.

```bash
jq -c 'select(.search_id == "<SEARCH_ID>" and (.event|startswith("playwright_")))' \
  worker/logs/worker.jsonl
```

```powershell
Get-Content worker\logs\worker.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.search_id -eq '<SEARCH_ID>' -and $_.event -like 'playwright_*' } |
  Sort-Object ts |
  Format-Table ts, event, attempt_id, attempt_number, outcome, reason_code
```

A `playwright_started` with no `playwright_captured_json`/`playwright_failed`
sharing its `attempt_id` means the outcome wasn't recorded — treat that as a
logging gap to fix, not as "it must have worked."

### One target listing

```bash
jq -c 'select(.target_listing_id == "<LISTING_ID>" or .listing_id == "<LISTING_ID>")' worker/logs/worker.jsonl
```

---

## 3. Common questions

### Which searches fell back off the direct path?

```powershell
Get-Content worker\logs\worker.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.event -eq 'fallback_selected' } |
  Group-Object fallback_reason | Sort-Object Count -Descending
```

```bash
jq -c 'select(.event == "fallback_selected")' worker/logs/worker.jsonl |
  jq -r '.fallback_reason' | sort | uniq -c | sort -rn
```

### Which needed raw HTTP HTML, and which needed a browser?

`source` distinguishes them and never conflates the two:

- `direct_json` — GraphQL over plain HTTP
- `raw_http_html` — a plain HTTP GET of the server-rendered document
- `rendered_html` — markup a browser produced
- `playwright_capture` — GraphQL captured from browser traffic

```bash
jq -r 'select(.source) | .source' worker/logs/worker.jsonl | sort | uniq -c
jq -c 'select(.event | startswith("playwright_"))' worker/logs/worker.jsonl
```

### All 429/503, circuit openings, retry exhaustion, and limit changes

```powershell
$interesting = 'direct_http_overloaded','cooldown_started','circuit_opened','circuit_half_open','circuit_closed','retry_budget_exhausted','limit_adjusted'
Get-Content worker\logs\worker.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $interesting -contains $_.event } |
  Sort-Object ts |
  Format-Table ts, event, request_class, status, reason_code, permitted_rate_per_sec, permitted_concurrency, cooldown_seconds
```

```bash
jq -c 'select(.event | test("overloaded|cooldown_started|circuit_|retry_budget_exhausted|limit_adjusted"))' \
  worker/logs/worker.jsonl
```

`limit_adjusted` carries `direction` (`increase`/`decrease`) plus the previous
and new rate/concurrency — that is the current adaptive state over time.

---

## 4. Finding the payload behind a blocked decision

1. Find the blocked event and read its `artifact_path`:

```bash
jq -c 'select(.event == "direct_http_blocked" and .report_id == "<REPORT_ID>")
       | {ts, search_id, reason_code, evidence_paths, artifact_id, artifact_path, artifact_sha256}' \
  worker/logs/worker.jsonl
```

2. Open the file relative to the artifact root:

```powershell
Get-Content "worker\logs\scraper-artifacts\<artifact_path>" | ConvertFrom-Json
```

3. Verify you are reading the payload the event refers to:

```powershell
(Get-FileHash "worker\logs\scraper-artifacts\<artifact_path>" -Algorithm SHA256).Hash.ToLower()
```

```bash
sha256sum "worker/logs/scraper-artifacts/<artifact_path>"
```

`evidence_paths` names the JSON path(s) that matched — e.g.
`errors[0].extensions.code` — without copying the value. The artifact holds the
(redacted) value, so the decision can be reviewed rather than trusted.

**Replaying the decision.** JSON artifacts are stored as JSON, not a Python
`repr`, specifically so they can be fed back through the classifier:

```python
from worker.core.scrape_artifacts import load_artifact
from worker.scraper.search_result_contract import classify_search_payload

payload = load_artifact("<artifact_path>")
print(classify_search_payload(payload, 200))   # same reason_code as the event
```

If `artifact_truncated` is `true` the body exceeded `SCRAPER_ARTIFACT_MAX_BYTES`
and the tail is missing; `artifact_original_bytes` tells you by how much. If
`artifact_decode_error` is present, the body was not decodable JSON and was
stored as bounded raw text alongside its content type.

---

## 5. Running the calibration tool

It sends **real traffic to Airbnb**. It is disabled by default and needs two
independent opt-ins:

```powershell
$env:AIRBNB_CALIBRATION_ENABLED = "1"
python -m worker.core.airbnb_calibration --i-understand-live-traffic `
  --max-requests 60 --max-seconds 180 --max-rate-per-sec 2 --max-concurrency 4
```

It starts at one in-flight request with ≥1s between starts, steps up only after
a full healthy window, and stops at the first of: the request cap, the duration
cap, the configured ceiling, or `--error-threshold` consecutive
429/503/challenge responses. Every command-line value is clamped to an absolute
ceiling, so a typo cannot turn it into a load test. On an unhealthy stop it
enters cooldown and sends nothing further.

The full report lands in `worker/logs/airbnb_calibration.json`.

**How to read it.** `recommendation.recommended_*` values are **50% of the
highest healthy envelope actually observed** in that run. They are not an Airbnb
limit — the safe envelope depends on the deployment, the session, the endpoint
and the time of day, and it changes. If `recommendation.status` is
`no_healthy_envelope_observed`, no step completed cleanly: do **not** raise any
limit; investigate session health first.

Whatever you set, the runtime policy stays adaptive: it will reduce below these
values on its own the moment Airbnb pushes back.

---

## 6. Multiple worker instances

The admission policy is **process-local**. Two worker processes each enforcing
"4 concurrent requests" send Airbnb eight.

If you run more than one worker process against Airbnb concurrently, set
`AIRBNB_ADMISSION_INSTANCES` to that count in every process. Each then takes a
proportional share of the rate and concurrency ceilings. This is a *static
partition*, not dynamic coordination: an idle instance does not lend its share
to a busy one. A shared lease/token gate is deliberately not implemented.

If a deployment cannot declare its instance count, **run a single worker
instance.**

---

## 7. Artifacts: retention, access, cleanup

- Written to `SCRAPER_ARTIFACT_DIR`, one directory per UTC day
  (`YYYY-MM-DD/art-<id>.json|.txt`).
- Redacted recursively *before* the write: cookies, auth/session tokens, API
  keys, signed URL query values, emails and phone numbers. Request headers are
  never persisted.
- Bounded four ways: per-artifact bytes, per-report count, total retained bytes,
  and age. Retention runs at the end of every job.
- Writes are atomic (temp file plus `os.replace`) and best-effort. A failed
  capture emits `artifact_capture_failed` and changes nothing about the scrape.
- Full-payload capture (successful responses) requires
  `SCRAPER_ARTIFACT_CAPTURE_FULL_PAYLOADS=1` and should stay off outside local
  development.
- Redaction reduces exposure; it is not a guarantee. Artifacts are Airbnb
  response bodies from an authenticated session — treat the directory as
  operator-only, keep it off shared volumes, and do not attach raw artifacts to
  public tickets.
- Manual cleanup:

```python
from worker.core.scrape_artifacts import enforce_retention
print(enforce_retention())   # {'removed': N, 'freed_bytes': M}
```

---

## 8. Triage: "the worker is producing 503s"

1. `jq -c 'select(.event=="limit_adjusted")' worker/logs/worker.jsonl | tail -20`
   — is the policy already backing off? `permitted_rate_per_sec` falling means
   it is working as designed.
2. `jq -c 'select(.event=="circuit_opened")' worker/logs/worker.jsonl | tail`
   — if the circuit is opening repeatedly, this is not throughput tuning; the
   session is probably blocked. Check the artifacts behind the
   `direct_http_blocked` events.
3. Check `AIRBNB_ADMISSION_INSTANCES` matches the number of workers actually
   running. A mismatch here is the most common cause of "we are under our
   configured limit but Airbnb disagrees".
4. Lower `AIRBNB_MAX_START_RATE_PER_SEC` and `AIRBNB_MAX_INFLIGHT_REQUESTS`.
   Do **not** respond by raising retry counts or timeouts — that adds load in
   the direction Airbnb is pushing back from.

---

## 9. Known issue — comparable pools coming back empty

**Symptom.** Reports fail with *"We couldn't collect enough trustworthy nightly
prices to build this report."* The event log shows searches returning HTTP 200
with 40+ results, but `candidate_funnel` lines report `priced=0`.

**This is not an admission/throttling problem.** Confirm before chasing limits:

```bash
jq -c 'select(.event=="direct_http_overloaded" or .event=="circuit_opened")' worker/logs/worker.jsonl
```

If that is empty, Airbnb is answering normally and the failure is downstream in
parsing/filtering.

**Where it actually breaks.** Look at the funnel line in `worker/logs/worker.log`:

```
candidate_funnel ... fetched=40 parsed=40 structural_excluded=36 no_price=0 priced=0
```

`structural_excluded` counts comps dropped by `_matches_structural_filters()`,
which requires `comp.accommodates == target_accommodates` exactly. On the daily
paths `pdp_structural_enrichment=False`, so `accommodates` can only come from
the search card via `parse_search_listing_context()`. When that field stops
being extracted, every comp is dropped and the pool is empty regardless of how
healthy the scrape was.

Related: `worker/scraper/parsers.py` currently prefers the **primary display
total** over the nightly breakdown in `_matching_primary_display_total()` — the
guard that rejected a display price disagreeing with the breakdown total was
removed. Three tests pin the intended behaviour and currently fail:

```
pytest worker/tests/test_search_context_price_availability.py \
       worker/tests/test_price_extraction.py
```

Fix the parser (make those tests pass) before adjusting anything in this
runbook's tables.
