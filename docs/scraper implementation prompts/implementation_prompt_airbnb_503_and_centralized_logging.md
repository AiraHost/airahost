# Implementation Prompt: Prevent Airbnb 503s and Add End-to-End Scraper Observability

You are working in the AiraHost repository. Implement a production-safe solution for recurring Airbnb HTTP 503/429 responses and make every scraper fallback traceable through centralized, structured logging.

Do not assume the existing request limits are safe. Determine the actual request paths, measure current behavior, choose conservative defaults from evidence, and make the worker adapt automatically when Airbnb signals overload. Do not attempt to bypass CAPTCHAs, authentication, or anti-bot controls. Respect Airbnb's terms and stop/cool down when challenged.

## Read before changing code

Read these files and their immediate callers/tests first:

- `worker/core/rate_limiter.py`
- `worker/scraper/playwright_scraper.py`
- `worker/scraper/airbnb_client.py`
- `worker/scraper/comp_collection.py`
- `worker/scraper/page_state.py`
- `worker/scraper/search_result_contract.py`
- `worker/core/concurrent_runner.py`
- `worker/main.py`, especially root logger setup and worker/report context
- `worker/tests/test_rate_limiter.py`
- `worker/tests/test_fetch_search_direct.py`
- `worker/tests/test_search_via_browser_capture.py`
- `worker/tests/test_browser_search_blocked_recovery.py`
- `worker/tests/test_collect_search_comps_integrity.py`

Search the entire `worker/` tree for every outbound Airbnb request, including `requests.Session`, Playwright navigation, GraphQL response capture, PDP calls, retries, session refreshes, and browser recovery. Produce a short inventory before implementation. Every outbound Airbnb request path must either pass through the shared admission policy or be explicitly documented as not controllable and accounted for by a stricter browser budget.

## Required outcomes

### 1. Establish a safe request envelope without creating a load test against production

The current defaults (`AIRBNB_MIN_REQUEST_INTERVAL_MS=250`, `AIRBNB_MAX_INFLIGHT_REQUESTS=8`) are hypotheses, not proof. Add a bounded diagnostic/benchmark mode that can determine a conservative operating envelope using ordinary scraper work or a small, explicitly capped probe. It must:

- Start conservatively (for example one in-flight request and at least 1 second between starts), then increase only after a configurable healthy sample window.
- Never run an unbounded ramp, never deliberately continue through repeated 429/503/challenge responses, and have hard caps on requests, duration, concurrency, and target rate.
- Record attempted rate, actual start rate, concurrency, latency percentiles, status/outcome counts, time-to-first-429/503, and recovery time.
- Stop immediately and enter cooldown when the configured error/challenge threshold is reached.
- Produce a machine-readable report and a concise recommendation. The recommendation must apply a safety margin below the highest healthy observed envelope; it must not claim a universal Airbnb limit.
- Be disabled by default and documented. Unit tests must use fakes; normal CI must make no live Airbnb calls.

If a live calibration cannot safely or legally be run, implement the instrumentation and conservative defaults, document that the final envelope must be validated in the deployment environment, and do not invent a measured limit.

### 2. Replace fixed retry behavior with coordinated adaptive throttling

Use one admission policy for direct StaysSearch, direct PDP, and browser-triggered Airbnb work. Preserve a simple design, but cover all concurrent report threads and, if deployments can run more than one worker process/instance, provide a shared coordination option (for example a database-backed lease/token gate) or explicitly force/document single-instance operation. A process-local singleton alone is insufficient for a multi-instance deployment.

Required behavior:

- Globally smooth request starts and cap in-flight work by request class (`search`, `pdp`, `browser_navigation`, `session_refresh`) while also enforcing an aggregate ceiling.
- On 429 or 503, honor a valid `Retry-After` header. Otherwise use exponential backoff with full jitter and a configurable cap.
- Reduce permitted concurrency/rate multiplicatively after 429/503 or authoritative challenge/auth-block evidence. Recover additively and slowly only after a sustained healthy window.
- Open a circuit breaker after repeated overload/block signals. While open, do not stampede Playwright: fail/requeue or defer according to existing job semantics. Permit only a bounded half-open probe after cooldown.
- Apply a retry budget per logical operation and a shared retry budget per report. Retries must re-enter the limiter and must not multiply load across concurrent threads.
- Distinguish overload (`429`, `503`, `Retry-After`) from auth/challenge blocking, transport failures, malformed payloads, valid empty inventory, and application/server errors. Do not treat all 503s as proof of rate limiting, but respond conservatively.
- Avoid synchronized retries across workers. Do not log or expose cookies, authorization values, API keys, full request headers, or signed URLs.
- Keep all controls configurable through validated environment variables with safe bounds and startup logging of effective, non-secret configuration.

Prefer reducing concurrency and deferring work over adding more browser retries. Playwright is a recovery path, not an unlimited parallel bypass.

### 3. Make fallback decisions explicit and traceable

Model each search attempt as a small state machine and emit one event at every transition. Use stable names such as:

`direct_http_started -> direct_http_succeeded`

`direct_http_started -> direct_http_degraded -> raw_html_parse_started -> raw_html_parse_succeeded`

`raw_html_parse_failed -> playwright_started -> playwright_captured_json`

`direct_http_blocked -> cooldown_started` (do not automatically escalate into a browser stampede)

Use the repository's actual paths after investigation; do not invent a raw-HTML fallback if one does not exist. If HTML parsing currently occurs inside a browser navigation, name it accurately (`rendered_html`) and keep `raw_http_html` distinct. Preserve the existing contract that blocked/degraded responses never masquerade as successful empty inventory.

For every attempt, make it easy to answer:

- Which report, target listing, comparable search, date window, location (redacted or hashed if needed), page offset, and logical operation caused it?
- Was the source direct GraphQL JSON, raw HTTP HTML, or Playwright-rendered HTML?
- Why did the previous method fail, and which classifier/reason code caused fallback?
- How many retries occurred, how long did limiter waiting take, what was the status, latency, result count, and final outcome?
- Did this attempt observe 429/503, open a circuit, enter cooldown, use raw HTML, or start Playwright?

Generate one `trace_id` per report/job, one `search_id` per logical listing search (stable across its retries/fallbacks), and one `attempt_id` per network attempt. Propagate them through worker threads and scraper layers without mutable global context. Include `report_id`, `target_listing_id`, `search_id`, `attempt_id`, `operation`, `source`, `fallback_from`, `fallback_reason`, `checkin`, `checkout`, `offset`, `attempt_number`, and `worker_instance_id` where applicable.

### 4. Centralized structured logging

Keep human-readable console logs, but add a centralized structured event sink. Prefer newline-delimited JSON through Python logging so all `worker.*` modules use the same pipeline. If this deployment already has an external log platform, integrate using its standard stdout/agent transport; otherwise write rotating `worker/logs/worker.jsonl` and document how it can be tailed and filtered. Do not introduce a hosted vendor without explicit configuration.

Each event must include at least:

- UTC ISO-8601 timestamp, severity, logger, event name, schema version
- trace/search/attempt IDs and worker/report/listing context
- operation and source (`direct_json`, `raw_http_html`, `rendered_html`, `playwright_capture`)
- sanitized endpoint host/path and GraphQL operation name, never the full sensitive query string
- HTTP status, Airbnb request IDs when safe, elapsed time, limiter wait, current rate/concurrency state
- outcome and stable reason code
- fallback transition and retry/circuit/cooldown state
- payload/artifact reference when diagnostic capture occurred

Use a single event helper/filter/adapter rather than hand-formatting JSON strings throughout business logic. Logging failures must never fail a report. Avoid duplicate handlers when modules are imported more than once.

### 5. Capture the exact blocked/degraded response safely

The operator needs to inspect the JSON that led the program to conclude it was blocked and to use Playwright/rendered HTML. Implement a diagnostic artifact store with these rules:

- Capture the response body used by the classifier before mutation whenever the outcome is blocked/degraded or a fallback is triggered. Preserve whether it was JSON, raw HTTP HTML, or rendered HTML.
- Store valid JSON as JSON, not a Python repr. If JSON decoding failed, store a bounded raw-body artifact plus content type and decode error.
- The structured log event references `artifact_id`, relative path/object key, SHA-256, original byte count, stored byte count, truncation flag, capture reason, and classifier reason code.
- Default to a local rotating/retained directory such as `worker/logs/scraper-artifacts/YYYY-MM-DD/`; make the destination pluggable for centralized object storage if already available.
- Redact recursively before persistence: cookies, auth/session tokens, API keys, signed URL/query values, emails, phone numbers, and sensitive headers. Do not persist request headers by default. Add tests proving secrets are absent.
- Bound maximum artifact size, artifacts per report, total retained bytes, and retention age. Writes must be atomic and best-effort. A failure to save an artifact emits a sanitized error event but does not alter scraper behavior.
- Full payload capture must be explicitly enabled outside local development. In production, default to sanitized diagnostic capture only for error/fallback events; never log every successful raw payload.
- Do not put raw payloads or HTML directly into the ordinary log line. Logs hold metadata and a reference; artifacts hold bounded sanitized content.

When the classifier says “blocked,” log both its stable reason code and the minimal evidence path(s) that matched (for example `errors[0].extensions.code`), without copying sensitive values into the event. The artifact must allow an authorized operator to reproduce why the decision was made.

### 6. Operator usability

Add a short runbook covering:

- Environment variables and conservative defaults.
- How to tail all structured events for a report, target listing, or `search_id` in chronological order using PowerShell and a POSIX example.
- How to find searches where direct fetch failed and raw/rendered HTML or Playwright was used.
- How to locate the sanitized raw JSON artifact for a blocked decision.
- How to find all 429/503 events, circuit openings, retry exhaustion, and current adaptive limit changes.
- How to run the bounded calibration and interpret its report without treating it as a guaranteed permanent limit.
- Artifact retention, redaction, access control, and cleanup behavior.

## Tests and acceptance criteria

Add deterministic tests that prove:

1. Concurrent direct search, PDP, and browser work share the aggregate admission ceiling.
2. Request starts are smoothed and concurrency never exceeds the configured cap.
3. `Retry-After` supports delta-seconds and valid HTTP dates; invalid values fall back safely.
4. 429/503 causes multiplicative reduction, jittered cooldown, and bounded retries; a healthy window causes slow additive recovery without exceeding configured maxima.
5. The circuit breaker prevents concurrent Playwright fallback stampedes and permits only the intended half-open probe.
6. Retry budgets are enforced per operation and per report.
7. Blocked, degraded, overload, malformed, valid-empty, and healthy outcomes remain distinct.
8. One logical search retains its `search_id` across direct, HTML, and Playwright transitions while every network attempt gets a unique `attempt_id`.
9. A direct-fetch failure followed by raw/rendered HTML emits an ordered, queryable transition sequence with the target listing/date/offset context.
10. A blocked JSON response is saved before fallback/termination, is valid JSON, is referenced from the event, and produces the same classifier reason on replay.
11. Artifact redaction removes secrets and PII; truncation, retention, quota, atomic-write failure, and disabled-capture behavior are tested.
12. No raw JSON/HTML, cookie, token, API key, full query URL, or authorization value appears in normal console or JSONL logs.
13. Existing browser-blocked and search-result integrity tests still pass; blocked responses never become empty inventory or synthesized success.
14. The calibration tool stops at every configured safety bound and makes no network call in unit tests.

Run the focused worker tests first, then `python -m unittest discover -s worker/tests`. Clearly report all failures and skips. Live tests must remain opt-in and must not be used as the only verification.

## Deliverables

- Production code and tests.
- A request-path inventory showing which admission policy covers each path.
- A documented conservative initial configuration and the evidence behind it. If no live measurement was authorized, state that plainly.
- A sample redacted JSONL trace showing direct fetch failure, HTML attempt, Playwright escalation or cooldown, and the artifact reference.
- The operator runbook and calibration instructions.
- A brief before/after report containing request rate, peak concurrency, 429/503 count, fallback count, Playwright count, p50/p95 latency, and report completion time. Never fabricate unavailable baseline data.

## Non-goals and guardrails

- Do not defeat CAPTCHAs, rotate identities/proxies to evade blocking, spoof users, or keep retrying through an authoritative challenge.
- Do not solve 503s merely by increasing retries or timeouts.
- Do not log secrets or unlimited response bodies.
- Do not silently change pricing, comparable-selection, empty-inventory, or report contracts.
- Do not replace tested behavior with a broad scraper rewrite unless evidence shows it is necessary.
- Do not claim a fixed “safe Airbnb rate limit.” The safe envelope is deployment-, session-, endpoint-, and time-dependent, so the runtime must remain adaptive.

Before coding, summarize assumptions, the request-path inventory, the proposed event schema, and the exact success criteria. After each significant step, state what changed, what was verified, and what remains.
