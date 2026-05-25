import logging
import os
from typing import Any, Dict, List

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
    if not urls:
        fallback = str(
            config.get("CDP_URL") or os.getenv("CDP_URL", "http://127.0.0.1:9222")
        ).strip()
        if fallback:
            urls = [fallback]
    return urls[:MAX_BROWSER_CLIENTS] if urls else ["http://127.0.0.1:9222"]


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
    pool_size = max(1, min(parsed_size, MAX_BROWSER_CLIENTS))
    cdp_urls = resolve_cdp_urls(base_config)
    logger.info(
        "[%s] browser pool init size=%s cdp_urls=%s",
        pool_name,
        pool_size,
        len(cdp_urls),
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
