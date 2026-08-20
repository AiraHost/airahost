"""Evidence-based classification of a rendered Airbnb search page.

Replaces the old `_page_looks_challenged(html, url)` substring test, which
lowercased `url + entire serialized HTML` and returned True if *anything*
contained "/login", "captcha", "/challenge", etc. Normal Airbnb search HTML
carries a "Log in" nav link, login route data, and challenge-related bundle
strings inside <script> blocks, so that test fired on healthy pages. The
resulting ordering was the production defect:

    challenge falsely detected
      -> arbitrary /rooms/ anchors scraped off the page
      -> synthetic HTTP 200 StaysSearch payload of ID-only rows returned
      -> collector accepted the page as OK and produced zero priced comps.

Classification here works off *visible* DOM state plus the actual final URL
path, returns the matched evidence, and lets positive healthy evidence
override incidental markers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Audited Airbnb routes that only ever render an auth/challenge wall. Matched
# against the *parsed path* of the final URL — never as a substring of the
# whole URL, whose query legitimately carries /login-ish redirect targets.
_AUTH_PATH_PREFIXES = (
    "/login",
    "/logout",
    "/signup_login",
    "/challenge",
    "/checkpoint",
    "/account/security",
    "/verify",
)

# Visible-text phrases that only appear on a real interstitial. Deliberately
# excludes the bare words "log in" / "captcha", which appear in ordinary
# navigation and in script bundles on healthy pages.
_BLOCKING_TEXT_PATTERNS = (
    (re.compile(r"verify\s+you\s+are\s+(?:a\s+)?human", re.I), "visible_captcha"),
    (re.compile(r"are\s+you\s+a\s+human", re.I), "visible_captcha"),
    (re.compile(r"confirm\s+you(?:'re| are)\s+(?:a\s+)?human", re.I), "visible_captcha"),
    (re.compile(r"\bcomplete\s+(?:the\s+)?(?:security\s+)?captcha\b", re.I), "visible_captcha"),
    (re.compile(r"\bsecurity\s+check(?:point)?\b", re.I), "visible_security_check"),
    (re.compile(r"\bunusual\s+traffic\b", re.I), "visible_unusual_traffic"),
    (re.compile(r"\bsuspicious\s+activity\b", re.I), "visible_unusual_traffic"),
    (re.compile(r"\blog\s*in\s+to\s+continue\b", re.I), "visible_login_wall"),
    (re.compile(r"\bplease\s+log\s*in\b", re.I), "visible_login_wall"),
    (re.compile(r"\bsign\s+in\s+to\s+continue\b", re.I), "visible_login_wall"),
)

_ROOM_HREF_RE = re.compile(r"/rooms/(\d{5,})")
_SEARCH_PATH_RE = re.compile(r"^/s(?:/|$)")

# Stripped before any visible-text inspection: markup whose text is never shown
# to the user but routinely contains auth vocabulary.
_INVISIBLE_BLOCK_RE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HIDDEN_ELEMENT_RE = re.compile(
    r"<(\w+)\b[^>]*?(?:hidden(?=[\s/>])|aria-hidden\s*=\s*[\"']true[\"']"
    r"|style\s*=\s*[\"'][^\"']*display\s*:\s*none)[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")

# DOM containers Airbnb uses for the search-results grid / card anchors.
SEARCH_RESULT_SELECTORS = (
    "[data-testid='card-container']",
    "[itemprop='itemListElement']",
    "[data-testid='listing-card-title']",
)
SEARCH_UI_SELECTORS = (
    "[data-testid='little-search']",
    "[data-testid='structured-search-input-field-query']",
    "[data-testid='category-bar-filter-button']",
    "[data-testid='map/GoogleMap']",
)

# Page kinds. `blocked` is the only one that may never yield a 200 payload.
KIND_HEALTHY = "healthy_search"
KIND_VALID_EMPTY = "valid_empty"
KIND_BLOCKED = "blocked_or_challenged"
KIND_SHELL = "hydrating_or_shell"
KIND_DEGRADED = "degraded_no_api"

# Below this, page.content() returned an unhydrated document shell (the
# production log's repeated html_len=15), not a page worth classifying.
SHELL_HTML_MAX_LEN = 2048


@dataclass
class PageState:
    """Outcome of one classification, with the evidence that produced it."""

    kind: str
    reason_code: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_blocked(self) -> bool:
        return self.kind == KIND_BLOCKED

    @property
    def is_healthy(self) -> bool:
        return self.kind in (KIND_HEALTHY, KIND_VALID_EMPTY)

    def as_log_fields(self) -> Dict[str, Any]:
        """Bounded, redaction-safe fields for a structured log line."""
        ev = self.evidence or {}
        return {
            "page_kind": self.kind,
            "reason_code": self.reason_code,
            "final_host": ev.get("final_host"),
            "final_path": ev.get("final_path"),
            "status": ev.get("status"),
            "html_len": ev.get("html_len"),
            "room_cards": ev.get("room_card_count"),
            "search_ui": ev.get("has_search_ui"),
            "markers": ",".join(ev.get("visible_markers") or []) or None,
        }


def redact_url(url: str) -> Dict[str, Optional[str]]:
    """Split a URL into host+path for logging; the query is dropped entirely.

    Search URLs carry the user's location, dates, and guest count — none of
    which belongs in a warning-level log line.
    """
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return {"final_host": None, "final_path": None}
    return {
        "final_host": parsed.netloc or None,
        "final_path": parsed.path or None,
    }


def _normalized_path(url: str) -> str:
    try:
        path = urlparse(str(url or "")).path or ""
    except Exception:
        return ""
    path = path.rstrip("/")
    return path.lower() or "/"


def auth_route_reason(url: str) -> Optional[str]:
    """Return a reason code when the URL's *path* is an auth/challenge route."""
    path = _normalized_path(url)
    if not path or path == "/":
        return None
    # Strip a leading locale segment (/en/login, /fr-ca/login).
    stripped = re.sub(r"^/[a-z]{2}(?:-[a-z]{2})?(?=/)", "", path, flags=re.IGNORECASE)
    for candidate in (path, stripped):
        for prefix in _AUTH_PATH_PREFIXES:
            if candidate == prefix or candidate.startswith(prefix + "/"):
                if prefix in ("/login", "/logout", "/signup_login"):
                    return "final_url_login"
                if prefix == "/challenge":
                    return "final_url_challenge"
                if prefix == "/checkpoint":
                    return "final_url_checkpoint"
                return "final_url_auth_route"
    return None


def visible_text_from_html(html: str) -> str:
    """Strip scripts/styles/templates/comments/hidden nodes, then de-tag.

    Everything removed here is markup whose content the user never sees but
    which carries the exact vocabulary the old substring detector tripped on.
    """
    text = str(html or "")
    text = _COMMENT_RE.sub(" ", text)
    text = _INVISIBLE_BLOCK_RE.sub(" ", text)
    text = _HIDDEN_ELEMENT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text)


def signals_from_html(html: str, *, final_url: str = "", status: Any = None) -> Dict[str, Any]:
    """Derive classification signals from a serialized HTML document.

    Used for fixtures/tests and as the fallback when live DOM evaluation is
    unavailable. `collect_dom_signals()` is preferred against a live page
    because it can see computed visibility.
    """
    raw = str(html or "")
    visible = visible_text_from_html(raw)
    body_only = _INVISIBLE_BLOCK_RE.sub(" ", _COMMENT_RE.sub(" ", raw))
    room_ids = set(_ROOM_HREF_RE.findall(body_only))
    markers = sorted({code for pattern, code in _BLOCKING_TEXT_PATTERNS if pattern.search(visible)})
    lowered = body_only.lower()

    def _has_any(selectors) -> bool:
        # Serialized HTML cannot be queried, so match each selector's value
        # token ("card-container", "little-search") against the markup.
        return any(sel.split("=")[-1].strip("'\"]").lower() in lowered for sel in selectors)

    has_search_ui = _has_any(SEARCH_UI_SELECTORS)
    has_result_container = _has_any(SEARCH_RESULT_SELECTORS)
    return {
        "html_len": len(raw),
        "room_card_count": len(room_ids),
        "has_search_ui": bool(has_search_ui),
        "has_result_container": bool(has_result_container),
        "visible_markers": markers,
        "has_no_results_copy": bool(
            re.search(r"no (?:exact )?(?:matches|results)|try adjusting|change your (?:search|dates)", visible, re.I)
        ),
        "source": "html",
        "status": status,
        **redact_url(final_url),
    }


# Runs inside the page: reads *rendered* state, so a hidden login template or a
# script blob containing "captcha" contributes nothing.
_DOM_SIGNALS_JS = r"""
() => {
  const isVisible = (el) => {
    if (!el) return false;
    if (el.hidden) return false;
    if (el.getAttribute && el.getAttribute('aria-hidden') === 'true') return false;
    const style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const roomIds = new Set();
  for (const a of Array.from(document.querySelectorAll('a[href*="/rooms/"]'))) {
    if (!isVisible(a)) continue;
    const m = /\/rooms\/(\d{5,})/.exec(a.getAttribute('href') || a.href || '');
    if (m) roomIds.add(m[1]);
  }

  const anyVisible = (selectors) => selectors.some((sel) => {
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { return false; }
    return nodes.some(isVisible);
  });

  // Visible text only: walk the body and skip subtrees that are not rendered.
  let visibleText = '';
  try {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => {
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        const tag = parent.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEMPLATE') {
          return NodeFilter.FILTER_REJECT;
        }
        return isVisible(parent) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      },
    });
    const chunks = [];
    let n;
    while ((n = walker.nextNode()) && chunks.length < 4000) chunks.push(n.nodeValue);
    visibleText = chunks.join(' ');
  } catch (e) {
    visibleText = '';
  }

  return {
    html_len: (document.documentElement && document.documentElement.outerHTML || '').length,
    room_card_count: roomIds.size,
    has_search_ui: anyVisible(__SEARCH_UI__),
    has_result_container: anyVisible(__SEARCH_RESULTS__),
    visible_text: visibleText.replace(/\s+/g, ' ').slice(0, 20000),
    source: 'dom',
  };
}
"""


async def collect_dom_signals(page, *, final_url: str = "", status: Any = None) -> Dict[str, Any]:
    """Read visible DOM state from a live Playwright page.

    Falls back to serialized-HTML signals when evaluation fails, so a closed or
    navigating page degrades to `hydrating_or_shell` rather than to a false
    challenge.
    """
    js = _DOM_SIGNALS_JS.replace(
        "__SEARCH_UI__", str(list(SEARCH_UI_SELECTORS)).replace("'", '"')
    ).replace("__SEARCH_RESULTS__", str(list(SEARCH_RESULT_SELECTORS)).replace("'", '"'))
    raw: Any = None
    try:
        raw = await page.evaluate(js)
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        html = ""
        try:
            html = await page.content() or ""
        except Exception:
            html = ""
        return signals_from_html(html, final_url=final_url, status=status)

    visible_text = str(raw.get("visible_text") or "")
    markers = sorted({code for pattern, code in _BLOCKING_TEXT_PATTERNS if pattern.search(visible_text)})
    return {
        "html_len": int(raw.get("html_len") or 0),
        "room_card_count": int(raw.get("room_card_count") or 0),
        "has_search_ui": bool(raw.get("has_search_ui")),
        "has_result_container": bool(raw.get("has_result_container")),
        "visible_markers": markers,
        "has_no_results_copy": bool(
            re.search(r"no (?:exact )?(?:matches|results)|try adjusting|change your (?:search|dates)", visible_text, re.I)
        ),
        "source": "dom",
        "status": status,
        **redact_url(final_url),
    }


def classify_page_state(
    *,
    final_url: str,
    signals: Dict[str, Any],
    status: Any = None,
    api_result_count: Optional[int] = None,
    graphql_auth_error: bool = False,
) -> PageState:
    """Classify one page snapshot.

    Precedence, highest first:
      1. Authoritative block  — auth/challenge URL path, 401/403, GraphQL auth
         error. These stand alone.
      2. Authoritative health — a parsed StaysSearch payload. A page that
         answered the API is not a challenge page whatever its markup says.
      3. Rendered results grid — cards inside real result containers. A
         challenge interstitial does not paint these, so they outrank markers.
      4. Corroborated block   — two or more independent visible signals, or one
         on a page showing no search chrome at all.
      5. Weaker health        — cards plus search chrome or a search URL path.
      6. Shell / degraded.
    """
    ev: Dict[str, Any] = dict(signals or {})
    if status is not None:
        ev["status"] = status
    ev.setdefault("status", None)
    ev.update(redact_url(final_url))

    html_len = int(ev.get("html_len") or 0)
    room_cards = int(ev.get("room_card_count") or 0)
    has_search_ui = bool(ev.get("has_search_ui"))
    has_result_container = bool(ev.get("has_result_container"))
    markers: List[str] = list(ev.get("visible_markers") or [])

    # 1. Authoritative blocked evidence.
    if graphql_auth_error:
        return PageState(KIND_BLOCKED, "graphql_auth_error", ev)
    route_reason = auth_route_reason(final_url)
    if route_reason:
        return PageState(KIND_BLOCKED, route_reason, ev)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    if status_int in (401, 403):
        return PageState(KIND_BLOCKED, f"http_{status_int}", ev)

    # 2. A parsed StaysSearch payload settles it: the session answered the API.
    if api_result_count is not None:
        if api_result_count > 0:
            return PageState(KIND_HEALTHY, "stayssearch_results", ev)
        return PageState(KIND_VALID_EMPTY, "stayssearch_empty", ev)

    # 3. Decisive healthy evidence: a rendered results grid. A challenge
    #    interstitial does not paint Airbnb's search-card containers, so this
    #    outranks any marker text — including markers that leaked in from a
    #    hidden template the DOM reader mis-scored.
    on_search_path = bool(_SEARCH_PATH_RE.match(_normalized_path(final_url)))
    if room_cards >= 2 and has_result_container:
        return PageState(KIND_HEALTHY, "visible_search_results", ev)

    # 4. Corroborated block: two independent visible signals, or one on a page
    #    showing no search chrome at all. Never a single incidental marker.
    if len(markers) >= 2 or (markers and not has_search_ui and html_len > SHELL_HTML_MAX_LEN):
        return PageState(KIND_BLOCKED, markers[0], ev)

    # 5. Weaker healthy evidence: room cards plus search chrome or a search URL.
    if room_cards >= 2 and (has_search_ui or on_search_path):
        return PageState(KIND_HEALTHY, "visible_search_results", ev)
    if on_search_path and has_search_ui and bool(ev.get("has_no_results_copy")):
        return PageState(KIND_VALID_EMPTY, "visible_no_results", ev)

    # 6. Nothing usable and nothing blocking.
    if html_len <= SHELL_HTML_MAX_LEN:
        return PageState(KIND_SHELL, "unhydrated_shell", ev)
    return PageState(KIND_DEGRADED, "no_search_evidence", ev)
