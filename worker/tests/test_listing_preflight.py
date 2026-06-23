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


def test_listing_preflight_marks_oops_404_page_as_missing():
    client = _PreflightClient({
        "status": 200,
        "html": "<main>Oops, this page returned a 404.</main>",
    })

    missing, debug = _preflight_listing_exists(
        client,
        "https://www.airbnb.com/rooms/789",
    )

    assert missing is True
    assert debug["htmlHasOops404"] is True


def test_listing_preflight_navigation_failure_is_inconclusive():
    client = _PreflightClient(exc=RuntimeError("browser unavailable"))

    missing, debug = _preflight_listing_exists(
        client,
        "https://www.airbnb.com/rooms/789",
    )

    assert missing is False
    assert debug["reason"] == "navigation_failed"
