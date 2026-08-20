"""Page-state classification must be driven by evidence, not by substrings.

The production incident (2026-08-12) logged "reached login/challenge page;
StaysSearch is blocked" for pages whose final URL was a normal /s/.../homes
search URL and whose document was Airbnb's ordinary guest SPA. The old
detector lowercased `page_url + the entire serialized HTML` and returned True
on any of "/login", "captcha", "/challenge", ... — all of which appear in
healthy Airbnb markup (nav links, route data, script bundles).

These tests pin the rule that replaced it: only the *actual* URL path and
*visible* DOM state can prove a block, positive search evidence outranks
incidental marker strings, and no verdict is sticky across navigations.
"""

from __future__ import annotations

import asyncio

from worker.scraper.page_state import (
    KIND_BLOCKED,
    KIND_HEALTHY,
    KIND_SHELL,
    PageState,
    auth_route_reason,
    classify_page_state,
    collect_dom_signals,
    redact_url,
    signals_from_html,
    visible_text_from_html,
)

SEARCH_URL = "https://www.airbnb.com/s/Belmont--California/homes?checkin=2026-09-01&query=Belmont"


def _healthy_search_html(room_ids=("1001111", "1002222", "1003333")) -> str:
    """A normal search page that carries every marker the old detector tripped on.

    Contains a "Log in" nav link pointing at /login, a serialized route table
    naming /login and /challenge, and the word captcha inside a script — none of
    which is visible to a guest looking at a working results grid.
    """
    cards = "".join(
        f'<div data-testid="card-container">'
        f'<a href="/rooms/{rid}?check_in=2026-09-01"><span data-testid="listing-card-title">Home {rid}</span></a>'
        f"<span>$210 CAD night</span></div>"
        for rid in room_ids
    )
    return f"""
    <html><head><title>Belmont vacation rentals</title></head>
    <body>
      <header>
        <nav><a href="/login">Log in</a><a href="/signup_login">Sign up</a></nav>
        <div data-testid="little-search">Belmont · Sep 1 · 2 guests</div>
      </header>
      <script type="application/json">
        {{"routes": ["/login", "/challenge", "/checkpoint"],
          "bot": {{"captchaProvider": "recaptcha", "securityCheckUrl": "/challenge"}}}}
      </script>
      <script>window.__CAPTCHA_SITE_KEY__ = "abc"; // verify you are human</script>
      <template id="login-modal"><h1>Log in to continue</h1><p>Security check</p></template>
      <main>{cards}</main>
      <div data-testid="map/GoogleMap"></div>
    </body></html>
    """


def _captcha_html() -> str:
    return """
    <html><head><title>Security check</title></head>
    <body>
      <main>
        <h1>Verify you are human</h1>
        <p>Please complete the security check to continue browsing Airbnb.</p>
      </main>
    </body></html>
    """


def _classify_html(html: str, url: str = SEARCH_URL, status=200) -> PageState:
    return classify_page_state(
        final_url=url,
        signals=signals_from_html(html, final_url=url, status=status),
        status=status,
    )


# ── Healthy pages must survive incidental markers (required test 22) ──────────

def test_healthy_search_page_with_login_links_and_script_captcha_is_not_blocked():
    state = _classify_html(_healthy_search_html())
    assert state.kind == KIND_HEALTHY
    assert state.reason_code == "visible_search_results"
    assert state.is_blocked is False


def test_login_and_captcha_vocabulary_in_scripts_produces_no_visible_markers():
    # The precise defect: those words exist in the document but not on screen.
    html = _healthy_search_html()
    assert "captcha" in html.lower()
    assert "/login" in html
    assert signals_from_html(html)["visible_markers"] == []


def test_visible_text_extraction_drops_scripts_templates_and_comments():
    text = visible_text_from_html(
        "<body><script>verify you are human</script>"
        "<template><h1>Security check</h1></template>"
        "<!-- unusual traffic -->"
        "<p>Homes in Belmont</p></body>"
    )
    assert "Homes in Belmont" in text
    for hidden in ("verify you are human", "Security check", "unusual traffic"):
        assert hidden not in text


# ── Authoritative blocked evidence (required tests 23, 24) ────────────────────

def test_auth_route_paths_classify_blocked_with_specific_reason_codes():
    cases = {
        "https://www.airbnb.com/login?redirect_url=/s/Belmont/homes": "final_url_login",
        "https://www.airbnb.com/challenge/captcha?x=1": "final_url_challenge",
        "https://www.airbnb.com/checkpoint/12345": "final_url_checkpoint",
        "https://www.airbnb.com/en/login": "final_url_login",
    }
    for url, expected in cases.items():
        state = _classify_html(_healthy_search_html(), url=url)
        assert state.kind == KIND_BLOCKED, url
        assert state.reason_code == expected, url


def test_login_in_query_string_only_is_not_an_auth_route():
    # A search URL whose query mentions /login is still a search URL: the old
    # detector's substring match over the whole URL is exactly what this bans.
    url = "https://www.airbnb.com/s/Belmont/homes?redirect=/login&query=captcha%20street"
    assert auth_route_reason(url) is None
    assert _classify_html(_healthy_search_html(), url=url).kind == KIND_HEALTHY


def test_visible_captcha_page_classifies_blocked():
    state = _classify_html(_captcha_html(), url="https://www.airbnb.com/s/Belmont/homes")
    assert state.kind == KIND_BLOCKED
    assert state.reason_code.startswith("visible_")


def test_same_captcha_text_hidden_in_template_or_script_does_not_block():
    html = (
        "<html><body>"
        '<div data-testid="little-search">Belmont</div>'
        '<template><h1>Verify you are human</h1><p>Security check</p></template>'
        "<script>const msg = 'unusual traffic detected';</script>"
        '<main><div data-testid="card-container"><a href="/rooms/1001111">A</a></div>'
        '<div data-testid="card-container"><a href="/rooms/1002222">B</a></div></main>'
        "</body></html>"
    )
    state = _classify_html(html)
    assert state.kind == KIND_HEALTHY
    assert state.is_blocked is False


def test_http_401_and_403_are_authoritative_blocks():
    for status in (401, 403):
        state = _classify_html(_healthy_search_html(), status=status)
        assert state.kind == KIND_BLOCKED
        assert state.reason_code == f"http_{status}"


def test_graphql_auth_error_classifies_blocked_even_on_a_healthy_looking_page():
    state = classify_page_state(
        final_url=SEARCH_URL,
        signals=signals_from_html(_healthy_search_html(), final_url=SEARCH_URL),
        status=200,
        graphql_auth_error=True,
    )
    assert state.kind == KIND_BLOCKED
    assert state.reason_code == "graphql_auth_error"


# ── Shell / hydration (required tests 25, 26) ────────────────────────────────

def test_commit_time_shell_is_shell_not_blocked_and_not_valid_empty():
    # The production log's repeated html_len=15: an unhydrated document says
    # nothing about the session, so it must not become a challenge verdict.
    state = _classify_html("<html></html>", status=200)
    assert state.kind == KIND_SHELL
    assert state.reason_code == "unhydrated_shell"
    assert state.is_blocked is False
    assert state.is_healthy is False


def test_shell_snapshot_then_hydrated_dom_classifies_healthy():
    shell = _classify_html("<html></html>")
    hydrated = _classify_html(_healthy_search_html())
    assert shell.kind == KIND_SHELL
    assert hydrated.kind == KIND_HEALTHY


def test_blocked_verdict_is_not_sticky_once_authoritative_health_arrives():
    # First navigation lands on /login; the retry lands on a normal search page
    # with a parsed StaysSearch payload. Each classification stands on its own.
    first = _classify_html(_captcha_html(), url="https://www.airbnb.com/login")
    assert first.kind == KIND_BLOCKED

    second = classify_page_state(
        final_url=SEARCH_URL,
        signals=signals_from_html(_healthy_search_html(), final_url=SEARCH_URL),
        status=200,
        api_result_count=18,
    )
    assert second.kind == KIND_HEALTHY
    assert second.reason_code == "stayssearch_results"


def test_captured_stayssearch_payload_outranks_page_markers():
    # A session that answered the API is not logged out, whatever the markup
    # contains. Only the URL path / HTTP status / GraphQL errors can override.
    state = classify_page_state(
        final_url=SEARCH_URL,
        signals=signals_from_html(_captcha_html(), final_url=SEARCH_URL),
        status=200,
        api_result_count=0,
    )
    assert state.kind == "valid_empty"
    assert state.is_blocked is False


# ── Live DOM signal collection ───────────────────────────────────────────────

class _FakePage:
    """Minimal Playwright page stand-in for collect_dom_signals()."""

    def __init__(self, *, evaluate_result=None, content_html="", raise_on_evaluate=False):
        self.evaluate_result = evaluate_result
        self.content_html = content_html
        self.raise_on_evaluate = raise_on_evaluate
        self.evaluate_calls = 0

    async def evaluate(self, _js):
        self.evaluate_calls += 1
        if self.raise_on_evaluate:
            raise RuntimeError("Execution context was destroyed")
        return self.evaluate_result

    async def content(self):
        return self.content_html


def test_collect_dom_signals_uses_visible_text_for_markers():
    page = _FakePage(
        evaluate_result={
            "html_len": 420_000,
            "room_card_count": 0,
            "has_search_ui": False,
            "has_result_container": False,
            "visible_text": "Verify you are human. Security check required.",
        }
    )
    signals = asyncio.run(collect_dom_signals(page, final_url=SEARCH_URL, status=200))
    assert signals["source"] == "dom"
    assert "visible_captcha" in signals["visible_markers"]
    state = classify_page_state(final_url=SEARCH_URL, signals=signals, status=200)
    assert state.kind == KIND_BLOCKED


def test_collect_dom_signals_falls_back_to_html_when_evaluation_fails():
    # A page navigating out from under us must degrade to a shell reading, not
    # to a challenge verdict.
    page = _FakePage(raise_on_evaluate=True, content_html="<html></html>")
    signals = asyncio.run(collect_dom_signals(page, final_url=SEARCH_URL, status=200))
    assert signals["source"] == "html"
    assert classify_page_state(final_url=SEARCH_URL, signals=signals).kind == KIND_SHELL


# ── Logging safety (required test 14) ────────────────────────────────────────

def test_redact_url_drops_the_query_string():
    fields = redact_url(SEARCH_URL)
    assert fields["final_host"] == "www.airbnb.com"
    assert fields["final_path"] == "/s/Belmont--California/homes"
    assert "checkin" not in str(fields)
    assert "query" not in str(fields)


def test_log_fields_carry_no_html_or_query_data():
    state = _classify_html(_healthy_search_html())
    rendered = " ".join(f"{k}={v}" for k, v in state.as_log_fields().items())
    assert "<" not in rendered
    assert "checkin=2026-09-01" not in rendered
    assert "captcha" not in rendered.lower()
    # But the diagnostics needed to triage the next incident are present.
    assert "page_kind=healthy_search" in rendered
    assert "final_path=/s/Belmont--California/homes" in rendered
