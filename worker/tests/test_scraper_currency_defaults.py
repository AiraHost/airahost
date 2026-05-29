from worker.scraper.deepbnb_backend import DeepBnbBackend
from worker.scraper.playwright_scraper import PlaywrightScraper


def test_deepbnb_backend_defaults_currency_to_usd(monkeypatch):
    monkeypatch.delenv("AIRBNB_CURRENCY", raising=False)
    backend = DeepBnbBackend(config={}, base_url="https://www.airbnb.com")
    assert backend.currency == "USD"


def test_playwright_scraper_defaults_currency_to_usd(monkeypatch):
    monkeypatch.delenv("AIRBNB_CURRENCY", raising=False)
    scraper = PlaywrightScraper({})
    assert scraper.currency == "USD"


def test_hardcoded_pdp_template_uses_runtime_currency(monkeypatch):
    monkeypatch.setenv("AIRBNB_USE_HARDCODED_STAYSPDP_TEMPLATE", "1")
    monkeypatch.delenv("AIRBNB_CURRENCY", raising=False)
    scraper = PlaywrightScraper({})
    assert isinstance(scraper.captured_pdp_req, dict)
    url = str(scraper.captured_pdp_req.get("url") or "")
    assert "currency=USD" in url
