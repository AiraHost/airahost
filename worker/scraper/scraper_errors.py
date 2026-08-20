from typing import Any, Dict, Optional


class ScraperForbiddenError(RuntimeError):
    """Raised when a scraper is blocked by Airbnb (403/challenge/auth wall)."""


class BrowserRuntimeUnavailable(RuntimeError):
    """A Playwright driver runtime could not be started or reached.

    Distinct from every Airbnb-side failure: nothing about the target site is
    wrong, the worker host or its CDP browser could not give us a browser
    runtime. Carries only sanitized structured fields — endpoint host/port, the
    operation, runtime counts — never cookies, tokens, query URLs, HTML, or the
    process environment, because the message reaches logs and the report
    failure path.
    """

    PUBLIC_MESSAGE = "browser runtime unavailable"
    ERROR_CODE = "browser_runtime_unavailable"

    def __init__(
        self,
        reason_code: str,
        *,
        operation: str = "",
        endpoint: str = "",
        active_runtimes: Optional[int] = None,
        runtime_cap: Optional[int] = None,
        attempt: Optional[int] = None,
        detail: str = "",
    ):
        self.reason_code = str(reason_code or "unknown")
        self.operation = str(operation or "")
        self.endpoint = str(endpoint or "")
        self.active_runtimes = active_runtimes
        self.runtime_cap = runtime_cap
        self.attempt = attempt
        self.detail = str(detail or "")
        super().__init__(f"{self.PUBLIC_MESSAGE} (reason={self.reason_code})")

    @property
    def public_message(self) -> str:
        return self.PUBLIC_MESSAGE

    def structured_fields(self) -> Dict[str, Any]:
        """Log/debug-safe fields. Deliberately excludes any free-form detail."""
        return {
            "error": self.ERROR_CODE,
            "reasonCode": self.reason_code,
            "operation": self.operation or None,
            "endpoint": self.endpoint or None,
            "activeRuntimes": self.active_runtimes,
            "runtimeCap": self.runtime_cap,
            "attempt": self.attempt,
        }


class BrowserRuntimeResourceExhausted(BrowserRuntimeUnavailable):
    """The OS refused to create the Playwright driver process.

    On Windows this is `OSError [WinError 1455] The paging file is too small
    for this operation to complete` raised out of `CreateProcess`. It says the
    host has run out of committable virtual memory, so retrying the same spawn
    immediately can only fail again — callers must fail fast, not fall back to
    another browser attempt.
    """

    PUBLIC_MESSAGE = "browser runtime unavailable: system virtual memory exhausted"
    ERROR_CODE = "browser_runtime_resource_exhausted"

    def __init__(self, reason_code: str = "process_spawn_resource_exhausted", **kwargs):
        super().__init__(reason_code, **kwargs)


class CdpAttachFailed(RuntimeError):
    """Could not attach to an externally managed CDP browser, and launching a
    replacement is disabled by default (``AIRAHOST_ALLOW_BROWSER_LAUNCH=false``).

    Raised instead of silently falling back to ``chromium.launch()`` — that
    fallback is the exact bug this error replaces: a transient CDP hiccup would
    otherwise spawn a fresh, logged-out browser instead of the operator's
    already-running, already-authenticated Chrome. Carries only the normalized
    host:port and a sanitized reason (the causing exception's type name) —
    never the full CDP URL, which can carry an opaque debugger-session token.
    """

    def __init__(self, endpoint: str, reason: str):
        self.endpoint = str(endpoint or "")
        self.reason = str(reason or "unknown")
        super().__init__(
            f"Could not attach to externally managed Chrome via CDP at {self.endpoint} "
            f"(reason={self.reason}). Launching a replacement browser is disabled by "
            "default; verify Chrome is running with --remote-debugging-port and the "
            "configured CDP_URL/CDP_URLS, or set AIRAHOST_ALLOW_BROWSER_LAUNCH=true to "
            "opt in to a standalone browser for local development."
        )


class AirbnbSearchBlocked(RuntimeError):
    """StaysSearch is unreachable because the session is logged out / challenged.

    Distinct from AirbnbRateLimited (retryable throttling) and from an ordinary
    empty result set. Carries only a bounded machine-readable reason code —
    never raw HTML, cookies, tokens, or a full URL with query parameters, since
    the message can reach logs and the report failure path.
    """

    def __init__(self, reason_code: str, *, detail: str = ""):
        self.reason_code = str(reason_code or "unknown")
        self.detail = str(detail or "")
        message = f"Airbnb search session is blocked (reason={self.reason_code})"
        if self.detail:
            message = f"{message}: {self.detail}"
        super().__init__(message)


class AirbnbSearchDegraded(RuntimeError):
    """The search page never produced a usable StaysSearch result.

    Covers unhydrated shells, malformed payloads, and missing API traffic.
    Retryable, and explicitly *not* evidence of a login/challenge wall.
    """

    def __init__(self, reason_code: str, *, detail: str = ""):
        self.reason_code = str(reason_code or "unknown")
        self.detail = str(detail or "")
        message = f"Airbnb search returned no usable results (reason={self.reason_code})"
        if self.detail:
            message = f"{message}: {self.detail}"
        super().__init__(message)
