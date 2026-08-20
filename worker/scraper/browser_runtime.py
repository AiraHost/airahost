import logging
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from worker.scraper.airbnb_client import AirbnbClient
from worker.scraper.playwright_runtime import (
    endpoint_label,
    is_process_spawn_resource_exhaustion,
    max_playwright_runtimes,
    normalize_cdp_endpoint,
)
from worker.scraper.scraper_errors import BrowserRuntimeResourceExhausted

logger = logging.getLogger(__name__)
MAX_BROWSER_CLIENTS = 3


def _parse_cdp_urls(raw_value: Any) -> List[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, (list, tuple)):
        out: List[str] = []
        for value in raw_value:
            text = str(value or "").strip()
            if text:
                out.append(text)
        return out
    text = str(raw_value or "").strip()
    if not text:
        return []
    urls: List[str] = []
    for token in text.split(","):
        item = token.strip()
        if item:
            urls.append(item)
    return urls


def resolve_cdp_urls(config: Dict[str, Any]) -> List[str]:
    urls = _parse_cdp_urls(config.get("CDP_URLS"))
    if not urls:
        urls = _parse_cdp_urls(os.getenv("CDP_URLS", ""))

    # Support comma-separated URLs in CDP_URL (e.g. "http://...:9222,http://...:9223,http://...:9224")
    if not urls:
        cdp_url_val = str(config.get("CDP_URL") or os.getenv("CDP_URL", "")).strip()
        if "," in cdp_url_val:
            urls = _parse_cdp_urls(cdp_url_val)

    cdp_url_raw = str(
        config.get("CDP_URL") or os.getenv("CDP_URL", "http://127.0.0.1:9222")
    ).strip() or "http://127.0.0.1:9222"
    # If CDP_URL contains commas (multi-URL), use only the first for single-endpoint fallback
    fallback = cdp_url_raw.split(",")[0].strip() or "http://127.0.0.1:9222"

    if urls:
        return _dedupe_urls(urls)[:MAX_BROWSER_CLIENTS]

    discovered = _discover_local_cdp_urls(fallback)
    if discovered:
        deduped = _dedupe_urls(discovered)
        return deduped[:MAX_BROWSER_CLIENTS]
    return [fallback]


def _dedupe_urls(urls: List[str]) -> List[str]:
    """Dedupe by *normalized endpoint*, not by raw string.

    `ws://127.0.0.1:9222/devtools/browser/<id>` and `http://127.0.0.1:9222`
    address the same browser; keying on the raw text let them through as two
    "distinct" endpoints, and each one then started its own driver.
    """
    out: List[str] = []
    seen: set[str] = set()
    for item in urls:
        url = str(item or "").strip()
        if not url:
            continue
        key = normalize_cdp_endpoint(url) or url.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def _cdp_http_base(cdp_url: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    raw = str(cdp_url or "").strip()
    if not raw:
        return None, None, None
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlparse(candidate)
    except Exception:
        return None, None, None

    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return None, None, None
    try:
        port = parsed.port
    except ValueError:
        return None, host, None

    if port is None:
        if parsed.scheme in ("https", "wss"):
            port = 443
        else:
            port = 80
    scheme = "https" if parsed.scheme in ("https", "wss") else "http"
    return f"{scheme}://{host}:{int(port)}", host, int(port)


def _is_local_host(host: Optional[str]) -> bool:
    if host is None:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _format_host_for_url(host: str) -> str:
    raw = str(host or "").strip()
    if not raw:
        return raw
    if ":" in raw and not raw.startswith("["):
        return f"[{raw}]"
    return raw


def _is_cdp_endpoint(url: str, timeout_seconds: float = 0.25) -> bool:
    base, _host, _port = _cdp_http_base(url)
    if not base:
        return False
    probe = f"{base}/json/version"
    try:
        req = Request(probe, headers={"User-Agent": "AiraHost-Worker/1.0"})
        with urlopen(req, timeout=timeout_seconds) as response:
            return int(getattr(response, "status", 0)) == 200
    except Exception:
        return False


def _discover_local_cdp_urls(seed_cdp_url: str) -> List[str]:
    base, host, port = _cdp_http_base(seed_cdp_url)
    if not base or not host or port is None or not _is_local_host(host):
        return [seed_cdp_url]

    raw_ports = str(os.getenv("CDP_DISCOVERY_PORTS", "")).strip()
    ports: List[int] = []
    if raw_ports:
        for token in raw_ports.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                ports.append(int(token))
            except Exception:
                continue
    if not ports:
        span = max(MAX_BROWSER_CLIENTS * 3, 8)
        ports = [port + i for i in range(span)]

    try:
        discovery_timeout = float(os.getenv("CDP_DISCOVERY_TIMEOUT_SECONDS", "0.25"))
    except Exception:
        discovery_timeout = 0.25

    discovered: List[str] = []
    host_for_url = _format_host_for_url(host)
    for candidate_port in ports:
        candidate = f"http://{host_for_url}:{int(candidate_port)}"
        if _is_cdp_endpoint(candidate, timeout_seconds=max(0.05, discovery_timeout)):
            discovered.append(candidate)
            if len(discovered) >= MAX_BROWSER_CLIENTS:
                break

    if not discovered:
        return [seed_cdp_url]
    return discovered


def _release_client(client: AirbnbClient) -> None:
    try:
        client.close_browser()
    except Exception as exc:
        logger.debug("browser client release failed: %s", exc)


def build_warmed_browser_client_pool(
    base_config: Dict[str, Any],
    requested_size: int,
    *,
    pool_name: str = "browser_pool",
) -> List[AirbnbClient]:
    """Logical browser clients for concurrent work, round-robin over endpoints.

    Several clients may share one endpoint — callers size their thread pools
    from ``len(pool)`` — but they share that endpoint's single Playwright
    runtime, so pool size no longer multiplies driver subprocesses. Concurrency
    against one browser is expressed with tab leases instead.

    Raises ``BrowserRuntimeResourceExhausted`` when every endpoint failed
    because the host could not spawn a driver: handing back a cold client there
    only defers the identical failure into the browser-fallback path, once per
    date and offset.
    """
    try:
        parsed_size = int(requested_size)
    except Exception:
        parsed_size = 1
    requested_pool_size = max(1, min(parsed_size, MAX_BROWSER_CLIENTS))
    cdp_urls = resolve_cdp_urls(base_config)
    if not cdp_urls:
        cdp_urls = [str(base_config.get("CDP_URL") or "http://127.0.0.1:9222")]

    runtime_cap = max_playwright_runtimes()
    effective_urls = cdp_urls[:runtime_cap]
    if len(effective_urls) < len(cdp_urls):
        logger.warning(
            "[%s] %s CDP endpoints configured but AIRAHOST_MAX_PLAYWRIGHT_RUNTIMES=%s; "
            "using the first %s",
            pool_name,
            len(cdp_urls),
            runtime_cap,
            len(effective_urls),
        )

    pool: List[AirbnbClient] = []
    # Warm each *endpoint* once, not each slot: slots sharing an endpoint share
    # its runtime, so a second warmup would prove nothing and (before the shared
    # runtime) started a second driver.
    endpoint_status: Dict[str, Optional[BaseException]] = {}
    for idx in range(requested_pool_size):
        url = effective_urls[idx % len(effective_urls)]
        key = normalize_cdp_endpoint(url) or url
        client = AirbnbClient({**base_config, "CDP_URL": url})
        if key not in endpoint_status:
            try:
                client.ensure_browser_ready()
            except BaseException as exc:  # noqa: BLE001 - classified below
                endpoint_status[key] = exc
                logger.warning(
                    "[%s] endpoint warmup failed cdp=%s err=%s — dropping its slots",
                    pool_name,
                    endpoint_label(url) or url,
                    exc,
                )
                _release_client(client)
                continue
            endpoint_status[key] = None
            logger.info(
                "[%s] endpoint ready cdp=%s", pool_name, endpoint_label(url) or url
            )
            pool.append(client)
            continue
        if endpoint_status[key] is not None:
            _release_client(client)
            continue
        # Endpoint already warm: a no-op on the shared started runtime, so extra
        # slots cannot start extra drivers.
        client.ensure_browser_ready()
        pool.append(client)

    failures = [exc for exc in endpoint_status.values() if exc is not None]
    if not pool:
        exhausted = next(
            (exc for exc in failures if is_process_spawn_resource_exhaustion(exc)), None
        )
        if exhausted is not None:
            raise BrowserRuntimeResourceExhausted(
                operation=f"{pool_name}:warmup",
                endpoint=endpoint_label(effective_urls[0]) if effective_urls else "",
                runtime_cap=runtime_cap,
                attempt=len(endpoint_status),
            ) from exhausted
        # Non-resource failures (browser not up yet, transient CDP refusal) keep
        # the historical behavior: hand back one cold client and let the normal
        # bounded retry/recovery path deal with it.
        cfg = dict(base_config)
        cfg["CDP_URL"] = effective_urls[0] if effective_urls else cdp_urls[0]
        logger.warning(
            "[%s] all warmups failed; falling back to primary endpoint %s",
            pool_name,
            endpoint_label(cfg["CDP_URL"]) or cfg["CDP_URL"],
        )
        return [AirbnbClient(cfg)]

    logger.info(
        "[%s] browser pool init requested=%s clients=%s endpoints=%s/%s runtime_cap=%s",
        pool_name,
        requested_pool_size,
        len(pool),
        sum(1 for exc in endpoint_status.values() if exc is None),
        len(cdp_urls),
        runtime_cap,
    )
    return pool


def close_browser_client_pool(pool: List[AirbnbClient]) -> None:
    for idx, client in enumerate(pool):
        try:
            client.close_extra_tabs()
        except Exception as exc:
            logger.debug("browser pool tab close failed slot=%s err=%s", idx, exc)
        try:
            client.close_browser()
        except Exception as exc:
            logger.debug("browser pool close failed slot=%s err=%s", idx, exc)
