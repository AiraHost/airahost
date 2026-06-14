import logging
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from worker.scraper.airbnb_client import AirbnbClient

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

    fallback = str(
        config.get("CDP_URL") or os.getenv("CDP_URL", "http://127.0.0.1:9222")
    ).strip() or "http://127.0.0.1:9222"

    if urls:
        return _dedupe_urls(urls)[:MAX_BROWSER_CLIENTS]

    discovered = _discover_local_cdp_urls(fallback)
    if discovered:
        deduped = _dedupe_urls(discovered)
        # Shift the discovered endpoints based on lane so they don't all pile onto the first one
        lane = str(os.getenv("WORKER_LANE", "interactive")).strip().lower()
        if lane == "nightly" and len(deduped) > 1:
            deduped = deduped[1:] + [deduped[0]]
        elif lane == "auto_apply" and len(deduped) > 2:
            deduped = deduped[2:] + deduped[:2]
        return deduped[:MAX_BROWSER_CLIENTS]
    return [fallback]


def _dedupe_urls(urls: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for item in urls:
        url = str(item or "").strip()
        if not url:
            continue
        key = url.lower()
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


def build_warmed_browser_client_pool(
    base_config: Dict[str, Any],
    requested_size: int,
    *,
    pool_name: str = "browser_pool",
) -> List[AirbnbClient]:
    try:
        parsed_size = int(requested_size)
    except Exception:
        parsed_size = 1
    requested_pool_size = max(1, min(parsed_size, MAX_BROWSER_CLIENTS))
    cdp_urls = resolve_cdp_urls(base_config)
    if not cdp_urls:
        cdp_urls = [str(base_config.get("CDP_URL") or "http://127.0.0.1:9222")]
    pool_size = requested_pool_size
    logger.info(
        "[%s] browser pool init requested=%s active=%s endpoints=%s urls=%s",
        pool_name,
        requested_pool_size,
        pool_size,
        len(cdp_urls),
        cdp_urls,
    )

    pool: List[AirbnbClient] = []
    for idx in range(pool_size):
        cfg = dict(base_config)
        cfg["CDP_URL"] = cdp_urls[idx % len(cdp_urls)]
        client = AirbnbClient(cfg)
        try:
            client.ensure_browser_ready()
            logger.info(
                "[%s] browser slot=%s ready cdp=%s",
                pool_name,
                idx,
                client.cdp_url,
            )
        except Exception as exc:
            logger.warning(
                "[%s] browser slot=%s warmup failed cdp=%s err=%s",
                pool_name,
                idx,
                client.cdp_url,
                exc,
            )
        pool.append(client)
    return pool


def close_browser_client_pool(pool: List[AirbnbClient]) -> None:
    for idx, client in enumerate(pool):
        try:
            client.close_browser()
        except Exception as exc:
            logger.debug("browser pool close failed slot=%s err=%s", idx, exc)
