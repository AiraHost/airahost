"""One validator for StaysSearch payloads, applied at every acceptance boundary.

Before this existed, `collect_search_comps()` set `page_ok=True` for any 2xx
response and only filtered rows afterwards, so a synthetic ID-only payload —
built from arbitrary /rooms/ anchors after a false challenge detection — looked
like a successful search page. Rows carrying nothing but a listing ID then
survived into the candidate pool because `parse_search_listing_context()`
defaulted `is_available` to True.

Both the browser path and the direct-HTTP path now classify the payload here
before anyone treats it as a page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Payload outcomes.
USABLE = "usable_results"
VALID_EMPTY = "valid_empty"
BLOCKED = "blocked_or_challenged"
DEGRADED = "degraded_or_malformed"

_AUTH_ERROR_TOKENS = (
    "unauth",
    "forbidden",
    "csrf",
    "captcha",
    "challenge",
    "login",
    "security",
)


@dataclass
class SearchPayloadState:
    outcome: str
    reason_code: str
    result_count: int = 0
    priced_count: int = 0
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        return self.outcome == USABLE

    @property
    def is_blocked(self) -> bool:
        return self.outcome == BLOCKED

    @property
    def is_authoritative(self) -> bool:
        """True when the payload is a well-formed StaysSearch answer.

        Only authoritative payloads may drive pagination decisions such as
        "result set exhausted"; a blocked or malformed page must not.
        """
        return self.outcome in (USABLE, VALID_EMPTY)


def build_empty_search_payload() -> Dict[str, Any]:
    """Canonical valid-empty StaysSearch payload.

    Shape matches what `_search_results()` (and every downstream parser/
    counter that walks the same path) expects, so a synthesized "no direct
    response, but nothing to fall back for" result is indistinguishable from
    a genuine empty page from Airbnb.
    """
    return {
        "data": {
            "presentation": {
                "staysSearch": {"results": {"searchResults": []}}
            }
        }
    }


def _search_results(data: Any) -> Optional[List[Any]]:
    if not isinstance(data, dict):
        return None
    node: Any = data
    for key in ("data", "presentation", "staysSearch", "results", "searchResults"):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, list) else None


def payload_has_auth_error(data: Any) -> bool:
    """True when GraphQL errors name an auth/challenge condition."""
    if not isinstance(data, dict):
        return False
    errors = data.get("errors")
    if not isinstance(errors, list):
        return False
    for err in errors:
        if not isinstance(err, dict):
            continue
        extensions = err.get("extensions") if isinstance(err.get("extensions"), dict) else {}
        text = " ".join(
            str(x or "")
            for x in (
                err.get("message"),
                err.get("errorType"),
                err.get("code"),
                extensions.get("code"),
                extensions.get("errorType"),
            )
        ).lower()
        if any(token in text for token in _AUTH_ERROR_TOKENS):
            return True
    return False


def auth_error_evidence_paths(data: Any) -> List[str]:
    """JSON paths whose *presence* triggered a blocked verdict.

    Returns paths only — never the values at them — so an event can say why the
    classifier decided "blocked" without copying an error message that may carry
    session or account detail. The saved artifact holds the (redacted) values.
    """
    paths: List[str] = []
    if not isinstance(data, dict):
        return paths
    errors = data.get("errors")
    if not isinstance(errors, list):
        return paths
    for idx, err in enumerate(errors):
        if not isinstance(err, dict):
            continue
        extensions = err.get("extensions") if isinstance(err.get("extensions"), dict) else {}
        candidates = (
            ("message", err.get("message")),
            ("errorType", err.get("errorType")),
            ("code", err.get("code")),
            ("extensions.code", extensions.get("code")),
            ("extensions.errorType", extensions.get("errorType")),
        )
        for suffix, value in candidates:
            text = str(value or "").lower()
            if text and any(token in text for token in _AUTH_ERROR_TOKENS):
                paths.append(f"errors[{idx}].{suffix}")
    return paths


def row_is_priced(row: Dict[str, Any]) -> bool:
    """A row with an *explicit* availability claim plus a positive price.

    Stricter than `row_is_bookable_priced()`: this only counts rows where the
    card itself said `available: true`. Kept for callers that want that
    narrower signal specifically (e.g. distinguishing explicit-True evidence
    from the weaker "unknown but priced" evidence in diagnostics).
    """
    if not isinstance(row, dict):
        return False
    if row.get("is_available") is not True:
        return False
    nightly = row.get("nightly_price")
    total = row.get("total_price")
    return bool(
        (isinstance(nightly, (int, float)) and nightly > 0)
        or (isinstance(total, (int, float)) and total > 0)
    )


def row_is_bookable_priced(row: Dict[str, Any], query_nights: Optional[int] = None) -> bool:
    """A row that may contribute a trusted date-specific nightly price.

    This is the single acceptance rule for the "is this row's price usable"
    question. Parser classification (`parse_search_listing_context()`) and
    collector acceptance (`collect_search_comps()`) must both call this
    function rather than each re-deriving the rule, so they cannot diverge
    the way they did in the incident: the parser kept an observed price for
    unknown availability while the collector rejected every such row outright.

    Accepts:
      - `is_available is True` (explicit), or
      - `is_available is None` (the card simply did not state it) — an
        *absence* of an availability claim is not itself a negative signal.

    Rejects:
      - `is_available is False` (explicit negative signal) — the ghost-price
        protection for sold-out/unavailable cards.
      - a minimum-stay requirement that exceeds `query_nights`, when both are
        known.
      - no positive parsed price for the exact queried dates.
    """
    if not isinstance(row, dict):
        return False
    if row.get("is_available") is False:
        return False
    min_nights = row.get("min_nights")
    if (
        query_nights is not None
        and isinstance(min_nights, int)
        and min_nights > int(query_nights)
    ):
        return False
    nightly = row.get("nightly_price")
    total = row.get("total_price")
    return bool(
        (isinstance(nightly, (int, float)) and nightly > 0)
        or (isinstance(total, (int, float)) and total > 0)
    )


def classify_search_payload(
    data: Any,
    status: Any = None,
    *,
    context: Optional[Dict[str, Dict[str, Any]]] = None,
    query_nights: Optional[int] = None,
) -> SearchPayloadState:
    """Classify a StaysSearch response.

    `context` is the output of `parse_search_listing_context()`; when supplied,
    priced-row counting uses it so callers do not each re-derive the rule.
    `priced_count` reflects `row_is_bookable_priced()` (explicit availability
    or unknown-but-otherwise-clean availability), not the stricter
    `row_is_priced()`, so this count matches what the collector will actually
    accept.
    """
    evidence: Dict[str, Any] = {"status": status}

    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None

    if status_int is not None and status_int in (401, 403):
        return SearchPayloadState(BLOCKED, f"http_{status_int}", evidence=evidence)

    if payload_has_auth_error(data):
        return SearchPayloadState(BLOCKED, "graphql_auth_error", evidence=evidence)

    if not isinstance(data, dict):
        return SearchPayloadState(DEGRADED, "payload_not_an_object", evidence=evidence)

    if status_int is not None and not (200 <= status_int < 300):
        return SearchPayloadState(DEGRADED, f"http_{status_int}", evidence=evidence)

    results = _search_results(data)
    if results is None:
        return SearchPayloadState(DEGRADED, "missing_search_results_node", evidence=evidence)

    evidence["result_count"] = len(results)

    if data.get("errors"):
        # Non-auth GraphQL errors: the page answered, but not authoritatively.
        return SearchPayloadState(
            DEGRADED, "graphql_errors", result_count=len(results), evidence=evidence
        )

    if not results:
        return SearchPayloadState(VALID_EMPTY, "empty_result_set", evidence=evidence)

    priced = 0
    if context is not None:
        priced = sum(1 for row in context.values() if row_is_bookable_priced(row, query_nights))
    evidence["priced_count"] = priced
    return SearchPayloadState(
        USABLE, "well_formed_results", result_count=len(results), priced_count=priced, evidence=evidence
    )
