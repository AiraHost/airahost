"""Bounded, two-stage readiness check for externally managed CDP endpoints.

Before startup auth checks or scraping trust a configured ``CDP_URL``/``CDP_URLS``
entry, this module verifies it in two stages:

1. A lightweight, driver-free HTTP ``GET /json/version`` — confirms something is
   actually listening and speaks CDP at all, without spending a Playwright
   driver on a port nothing is bound to.
2. A real, bounded ``connect_over_cdp()`` attach through the shared runtime
   registry (``worker.scraper.playwright_runtime``), so a probe on a healthy
   endpoint reuses — rather than duplicates — the single driver that
   endpoint's normal scraping work already shares. The lease is released
   immediately after the check, so a probe that finds nothing else using that
   endpoint tears its runtime back down instead of leaking a started driver.

Each endpoint gets one typed, sanitized result — transport readiness only.
Airbnb login state is a separate concern, checked afterward and only for
endpoints this module reports ready (see ``worker.main``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from worker.scraper.playwright_runtime import acquire_runtime, endpoint_label

logger = logging.getLogger(__name__)

STATUS_READY = "ready"
STATUS_TCP_OR_HTTP_UNAVAILABLE = "tcp_or_http_unavailable"
STATUS_INVALID_VERSION_RESPONSE = "invalid_version_response"
STATUS_CDP_ATTACH_FAILED = "cdp_attach_failed"

DEFAULT_HTTP_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class CdpPreflightResult:
    url: str
    label: str
    status: str
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY


def _http_base(url: str) -> Optional[str]:
    """``scheme://host:port`` for a CDP endpoint value, or None if unparseable."""
    raw = str(url or "").strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlparse(candidate)
    except Exception:
        return None
    host = str(parsed.hostname or "").strip()
    if not host:
        return None
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme in ("https", "wss") else 80
    scheme = "https" if parsed.scheme in ("https", "wss") else "http"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{int(port)}"


def _check_json_version(url: str, *, timeout_seconds: float) -> tuple[bool, str]:
    """Stage 1: HTTP GET /json/version. Returns (ok, status_if_not_ok)."""
    base = _http_base(url)
    if not base:
        return False, STATUS_TCP_OR_HTTP_UNAVAILABLE

    probe_url = f"{base}/json/version"
    try:
        request = Request(probe_url, headers={"User-Agent": "AiraHost-Worker/1.0"})
        with urlopen(request, timeout=timeout_seconds) as response:
            if int(getattr(response, "status", 0)) != 200:
                return False, STATUS_TCP_OR_HTTP_UNAVAILABLE
            raw_body = response.read(8192)
    except Exception:
        return False, STATUS_TCP_OR_HTTP_UNAVAILABLE

    try:
        payload = json.loads(raw_body.decode("utf-8", errors="replace"))
    except Exception:
        return False, STATUS_INVALID_VERSION_RESPONSE

    if not isinstance(payload, dict):
        return False, STATUS_INVALID_VERSION_RESPONSE
    # A usable CDP /json/version response identifies a browser/WebSocket target.
    # The WebSocket URL itself is never logged or stored — only that it was present.
    if not (payload.get("webSocketDebuggerUrl") or payload.get("Browser")):
        return False, STATUS_INVALID_VERSION_RESPONSE

    return True, ""


def check_cdp_endpoint(
    url: str,
    *,
    http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> CdpPreflightResult:
    """Two-stage readiness check for one CDP endpoint. Never raises."""
    label = endpoint_label(url)

    ok, status = _check_json_version(url, timeout_seconds=http_timeout_seconds)
    if not ok:
        return CdpPreflightResult(url=url, label=label, status=status)

    lease = acquire_runtime(url)
    try:
        lease.ensure_started(operation="cdp_preflight")
    except BaseException as exc:  # noqa: BLE001 - sanitized into a reason code below
        reason = getattr(exc, "reason_code", None) or type(exc).__name__
        return CdpPreflightResult(
            url=url, label=label, status=STATUS_CDP_ATTACH_FAILED, reason=str(reason)
        )
    finally:
        # Disconnects the driver's CDP connection only. Never closes the
        # operator-owned browser (see PlaywrightRuntime._teardown_async).
        lease.release()

    return CdpPreflightResult(url=url, label=label, status=STATUS_READY)


def check_cdp_endpoints(
    urls: List[str],
    *,
    http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> List[CdpPreflightResult]:
    """Check each endpoint in turn (never concurrently, so the shared runtime
    cap is never contested by the preflight itself)."""
    return [
        check_cdp_endpoint(url, http_timeout_seconds=http_timeout_seconds) for url in urls
    ]


def summarize_preflight(results: List[CdpPreflightResult]) -> str:
    """One concise line, e.g. ``9222=ready 9223=ready 9224=cdp_attach_failed``."""
    return " ".join(f"{result.label}={result.status}" for result in results)
