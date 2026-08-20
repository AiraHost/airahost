"""Bounded, redacted capture of the response body a classifier acted on.

When the scraper decides "this response is blocked, escalate to Playwright", the
operator needs to see the payload that produced that decision — otherwise a
false positive is indistinguishable from a real block, which is exactly how a
healthy page once became a browser navigation storm.

Rules this store enforces, because a diagnostic feature must not become the
incident:

  * **Logs hold references, artifacts hold content.** An event carries
    ``artifact_id``, a relative path, SHA-256, byte counts and a truncation
    flag. Never the body.
  * **Redact before persistence, recursively.** Cookies, auth/session tokens,
    API keys, signed URL query values, emails and phone numbers are replaced in
    nested JSON *and* in raw text before anything is written.
  * **JSON stays JSON.** A valid payload is stored as JSON so a replay can feed
    it straight back through the classifier and reproduce the decision. Only an
    undecodable body falls back to bounded raw text, recorded with its content
    type and decode error.
  * **Bounded everywhere.** Max artifact size, max artifacts per report, max
    retained bytes, max retention age. Writes are atomic and best-effort: a
    failed capture emits a sanitized error event and changes nothing about
    scraper behaviour.
  * **Off unless asked for.** Outside local development, capture is limited to
    error/fallback events; successful payloads are never persisted. Full-payload
    capture requires an explicit opt-in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from worker.core import scrape_events

logger = logging.getLogger("worker.core.scrape_artifacts")

# Keys whose *values* are secrets wherever they appear in a nested structure.
_SECRET_KEY_RE = re.compile(
    r"(cookie|set-cookie|authorization|auth[_-]?token|access[_-]?token|refresh[_-]?token"
    r"|id[_-]?token|bearer|api[_-]?key|apikey|x-airbnb-api-key|client[_-]?secret|secret"
    r"|password|passwd|passphrase|session[_-]?id|sessionid|csrf|xsrf|signature|credential)",
    re.IGNORECASE,
)
# Query parameters that make a URL a signed/bearer URL.
_SIGNED_QUERY_RE = re.compile(
    r"([?&](?:sig|signature|token|access_token|key|api_key|apikey|auth|expires|policy|"
    r"x-amz-signature|x-amz-credential|x-amz-security-token)=)[^&#\s\"']+",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
_PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{7,}\d(?![\w.])")
# Header-shaped text anywhere in the body, not just at a line start: Airbnb's
# challenge pages inline `Set-Cookie:` fragments mid-document, and an anchored
# `^...$` pattern silently misses those.
_SET_COOKIE_HEADER_RE = re.compile(r"(?i)\b(set-cookie|cookie|authorization)\s*:\s*[^\n<]*")

REDACTED = "[redacted]"
ARTIFACT_SCHEMA_VERSION = 1

_quota_lock = threading.Lock()
# Per-report artifact counts, so one pathological report cannot exhaust the
# global retention budget on its own.
_report_counts: Dict[str, int] = {}


# ── configuration ────────────────────────────────────────────────────────────


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        raw = str(os.getenv(name, "") or "").strip()
        return int(float(raw)) if raw else int(default)
    except (TypeError, ValueError):
        return int(default)


def artifacts_enabled() -> bool:
    """Diagnostic capture for error/fallback outcomes. On by default."""
    return _env_flag("SCRAPER_ARTIFACT_CAPTURE_ENABLED", True)


def full_payload_capture_enabled() -> bool:
    """Capture on *successful* outcomes too. Requires an explicit opt-in.

    Left off in production so the store never accumulates a copy of every
    successful search payload.
    """
    return _env_flag("SCRAPER_ARTIFACT_CAPTURE_FULL_PAYLOADS", False)


def artifact_root() -> Path:
    configured = str(os.getenv("SCRAPER_ARTIFACT_DIR", "") or "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "logs" / "scraper-artifacts"


def max_artifact_bytes() -> int:
    return max(1024, min(_env_int("SCRAPER_ARTIFACT_MAX_BYTES", 256 * 1024), 8 * 1024 * 1024))


def max_artifacts_per_report() -> int:
    return max(0, min(_env_int("SCRAPER_ARTIFACT_MAX_PER_REPORT", 25), 1000))


def max_total_bytes() -> int:
    return max(
        0, min(_env_int("SCRAPER_ARTIFACT_MAX_TOTAL_BYTES", 200 * 1024 * 1024), 20 * 1024 ** 3)
    )


def retention_days() -> int:
    return max(1, min(_env_int("SCRAPER_ARTIFACT_RETENTION_DAYS", 7), 365))


# ── redaction ────────────────────────────────────────────────────────────────


def redact_text(text: str) -> str:
    """Scrub secrets and PII from free-form text (HTML, non-JSON bodies)."""
    out = str(text or "")
    out = _SET_COOKIE_HEADER_RE.sub(lambda m: f"{m.group(1)}: {REDACTED}", out)
    out = _SIGNED_QUERY_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _EMAIL_RE.sub(REDACTED, out)
    out = _PHONE_RE.sub(REDACTED, out)
    return out


def redact_json(node: Any, *, _depth: int = 0) -> Any:
    """Recursively scrub a decoded JSON structure.

    Depth-bounded: a pathological payload must not blow the stack inside a
    diagnostic path.
    """
    if _depth > 40:
        return "[truncated_depth]"
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for key, value in node.items():
            skey = str(key)
            if _SECRET_KEY_RE.search(skey):
                out[skey] = REDACTED
            else:
                out[skey] = redact_json(value, _depth=_depth + 1)
        return out
    if isinstance(node, list):
        return [redact_json(v, _depth=_depth + 1) for v in node]
    if isinstance(node, str):
        return redact_text(node)
    return node


# ── retention / quota ────────────────────────────────────────────────────────


def _iter_artifact_files(root: Path):
    if not root.exists():
        return []
    try:
        return [p for p in root.rglob("*") if p.is_file()]
    except OSError:
        return []


def enforce_retention(root: Optional[Path] = None) -> Dict[str, int]:
    """Delete artifacts past the age limit, then the oldest until under quota."""
    base = root or artifact_root()
    removed = 0
    freed = 0
    cutoff = time.time() - retention_days() * 86400
    files = _iter_artifact_files(base)
    survivors = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            try:
                path.unlink()
                removed += 1
                freed += stat.st_size
            except OSError:
                pass
            continue
        survivors.append((stat.st_mtime, stat.st_size, path))

    budget = max_total_bytes()
    if budget:
        total = sum(size for _mtime, size, _p in survivors)
        survivors.sort(key=lambda item: item[0])
        idx = 0
        while total > budget and idx < len(survivors):
            _mtime, size, path = survivors[idx]
            try:
                path.unlink()
                removed += 1
                freed += size
                total -= size
            except OSError:
                pass
            idx += 1
    # Prune now-empty day directories so the tree does not grow without bound.
    try:
        for child in sorted(base.glob("*"), reverse=True):
            if child.is_dir() and not any(child.iterdir()):
                child.rmdir()
    except OSError:
        pass
    return {"removed": removed, "freed_bytes": freed}


def reset_report_quota(report_id: Optional[str] = None) -> None:
    """Clear the per-report artifact counter (call when a report finishes)."""
    with _quota_lock:
        if report_id is None:
            _report_counts.clear()
        else:
            _report_counts.pop(str(report_id), None)


def _claim_report_slot(report_id: str) -> bool:
    limit = max_artifacts_per_report()
    if limit <= 0:
        return False
    with _quota_lock:
        used = _report_counts.get(report_id, 0)
        if used >= limit:
            return False
        _report_counts[report_id] = used + 1
        return True


def _release_report_slot(report_id: str) -> None:
    with _quota_lock:
        used = _report_counts.get(report_id, 0)
        if used > 0:
            _report_counts[report_id] = used - 1


# ── capture ──────────────────────────────────────────────────────────────────


def _serialize(
    body: Any, *, content_type: str
) -> Tuple[bytes, str, Optional[str]]:
    """Return (bytes, stored_format, decode_error).

    A valid JSON payload is stored as JSON — never a Python ``repr`` — so a
    replay can decode it and re-run the classifier on the identical structure.
    """
    decode_error: Optional[str] = None
    if isinstance(body, (dict, list)):
        try:
            return (
                json.dumps(redact_json(body), ensure_ascii=False, indent=2).encode("utf-8"),
                "json",
                None,
            )
        except (TypeError, ValueError) as exc:
            decode_error = f"json_encode_failed: {type(exc).__name__}"
            body = str(body)
    if isinstance(body, (bytes, bytearray)):
        try:
            body = bytes(body).decode("utf-8", errors="replace")
        except Exception as exc:  # pragma: no cover - decode with replace cannot raise
            decode_error = f"decode_failed: {type(exc).__name__}"
            body = ""
    text = str(body or "")
    if "json" in str(content_type or "").lower():
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError) as exc:
            decode_error = decode_error or f"json_decode_failed: {exc}"
        else:
            return (
                json.dumps(redact_json(parsed), ensure_ascii=False, indent=2).encode("utf-8"),
                "json",
                None,
            )
    stored_format = "html" if "html" in str(content_type or "").lower() else "text"
    return redact_text(text).encode("utf-8"), stored_format, decode_error


def capture_artifact(
    body: Any,
    *,
    capture_reason: str,
    reason_code: str,
    source: str,
    content_type: str = "application/json",
    report_id: Optional[str] = None,
    status: Any = None,
    evidence_paths: Optional[list] = None,
    is_error_outcome: bool = True,
    root: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Persist one sanitized artifact and return its reference metadata.

    Returns None when capture is disabled, quota-limited, or failed. Never
    raises: the caller's control flow must be identical whether or not the
    artifact was written.
    """
    try:
        if not artifacts_enabled():
            return None
        if not is_error_outcome and not full_payload_capture_enabled():
            return None

        rid = str(report_id or "unassigned")
        if not _claim_report_slot(rid):
            return None

        payload, stored_format, decode_error = _serialize(body, content_type=content_type)
        original_bytes = len(payload)
        limit = max_artifact_bytes()
        truncated = original_bytes > limit
        stored = payload[:limit] if truncated else payload

        artifact_id = f"art-{uuid.uuid4().hex[:20]}"
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        base = root or artifact_root()
        directory = base / day
        directory.mkdir(parents=True, exist_ok=True)
        suffix = ".json" if stored_format == "json" and not truncated else ".txt"
        target = directory / f"{artifact_id}{suffix}"

        # Atomic: write to a temp file in the same directory, then replace, so a
        # crashed write never leaves a half-artifact a replay could misread.
        handle, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".tmp-", suffix=suffix)
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(stored)
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        reference = {
            "artifact_id": artifact_id,
            "artifact_path": str(target.relative_to(base)).replace("\\", "/"),
            "artifact_sha256": hashlib.sha256(stored).hexdigest(),
            "artifact_original_bytes": original_bytes,
            "artifact_stored_bytes": len(stored),
            "artifact_truncated": truncated,
            "artifact_format": stored_format,
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "capture_reason": str(capture_reason),
            "reason_code": str(reason_code),
            "source": str(source),
        }
        if decode_error:
            reference["artifact_decode_error"] = decode_error
            reference["artifact_content_type"] = str(content_type or "")[:120]
        if evidence_paths:
            # The *paths* that matched (e.g. "errors[0].extensions.code"), never
            # the values at them.
            reference["evidence_paths"] = [str(p)[:120] for p in list(evidence_paths)[:10]]

        scrape_events.emit(
            scrape_events.ARTIFACT_CAPTURED,
            status=status,
            **reference,
        )
        return reference
    except Exception as exc:
        try:
            if report_id is not None:
                _release_report_slot(str(report_id))
            scrape_events.emit(
                scrape_events.ARTIFACT_CAPTURE_FAILED,
                level=logging.WARNING,
                capture_reason=str(capture_reason),
                reason_code=str(reason_code),
                error_type=type(exc).__name__,
            )
        except Exception:
            pass
        return None


def event_fields(reference: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The subset of a reference safe to splat into an event.

    Only the artifact's own identity fields. ``reason_code``, ``source`` and
    ``evidence_paths`` are omitted because the emitting call site already
    supplies them — and supplies them whether or not an artifact was written,
    so the event stays complete when capture is disabled.
    """
    if not reference:
        return {}
    return {
        k: v
        for k, v in reference.items()
        if k.startswith("artifact_") or k == "capture_reason"
    }


def load_artifact(artifact_path: str, *, root: Optional[Path] = None) -> Any:
    """Read a stored artifact back. JSON artifacts decode; others return text.

    Used by replay tooling to re-run the classifier over the exact structure the
    original decision was made on.
    """
    base = root or artifact_root()
    target = base / str(artifact_path)
    text = target.read_text(encoding="utf-8")
    if target.suffix == ".json":
        return json.loads(text)
    return text
