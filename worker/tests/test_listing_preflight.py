from __future__ import annotations

from worker.scraper.price_estimator import _preflight_listing_exists


class _PreflightClient:
    def __init__(self, result=None, exc: Exception | None = None):
        self.result = result or {}
        self.exc = exc
        self.calls = []

    def browse_url_html(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


def test_listing_preflight_marks_404_status_as_missing():
    client = _PreflightClient({"status": 404, "html": "<html></html>"})

    missing, debug = _preflight_listing_exists(
        client,
        "https://www.airbnb.com/rooms/123456",
    )

    assert missing is True
    assert debug["missing"] is True
    assert debug["statusIs404"] is True
    assert client.calls[0][0] == "https://airbnb.com/rooms/123456"


def test_listing_preflight_marks_narrow_200_not_found_page_as_missing():
    # Detection is deliberately narrow: a <title>/<h1> carrying 404, or the
    # literal not-found copy. Generic body prose mentioning 404 is not enough
    # (see the embedded-JS case below), so the fixture uses the real shape.
    for html in (
        "<html><head><title>Airbnb - 404 Page not found</title></head><body>Oops</body></html>",
        "<html><body><h1>404</h1></body></html>",
        "<html><body><p>This listing is no longer available</p></body></html>",
    ):
        client = _PreflightClient({"status": 200, "html": html})

        missing, debug = _preflight_listing_exists(
            client,
            "https://www.airbnb.com/rooms/789",
        )

        assert missing is True, html
        assert debug["htmlHasOops404"] is True


def test_listing_preflight_ignores_generic_404_text_in_embedded_javascript():
    # Required behavior 19: valid Airbnb listing pages ship error templates that
    # contain "404" and "oops". Those must never fail the user's report.
    html = (
        "<html><head><title>Cozy loft in Belmont · Airbnb</title></head><body>"
        "<script>window.errorTemplates={notFound:'404',oops:'Oops! Page not found in app'};</script>"
        + ("<div>listing content</div>" * 8000)
        + "</body></html>"
    )
    client = _PreflightClient({"status": 200, "html": html})

    missing, debug = _preflight_listing_exists(client, "https://www.airbnb.com/rooms/789")

    assert missing is False
    assert debug["htmlHasOops404"] is False


def test_listing_preflight_navigation_failure_is_inconclusive():
    client = _PreflightClient(exc=RuntimeError("browser unavailable"))

    missing, debug = _preflight_listing_exists(
        client,
        "https://www.airbnb.com/rooms/789",
    )

    assert missing is False
    assert debug["reason"] == "navigation_failed"


def test_listing_preflight_timeouts_challenges_429_and_5xx_stay_inconclusive():
    # Required behavior 20: none of these may be reported as a missing listing.
    inconclusive = [
        _PreflightClient(exc=TimeoutError("Timeout 8000ms exceeded")),
        _PreflightClient({"status": 429, "html": "<html><body>Too many requests</body></html>"}),
        _PreflightClient({"status": 503, "html": "<html><body>Service unavailable</body></html>"}),
        _PreflightClient(
            {
                "status": 200,
                "final_url": "https://www.airbnb.com/login",
                "html": "<html><body><h1>Verify you are human</h1></body></html>",
            }
        ),
    ]
    for client in inconclusive:
        missing, _ = _preflight_listing_exists(client, "https://www.airbnb.com/rooms/789")
        assert missing is False
