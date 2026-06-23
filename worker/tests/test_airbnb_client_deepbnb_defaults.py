from worker.scraper.airbnb_client import AirbnbClient


def test_airbnb_client_disables_deepbnb_by_default(monkeypatch):
    """
    Pricing jobs should stay on the Playwright-backed path unless Deepbnb is
    explicitly enabled for a targeted experiment.
    """
    monkeypatch.delenv("AIRBNB_USE_DEEPBNB_BACKEND", raising=False)

    client = AirbnbClient({})

    assert client.use_deepbnb_backend is False
    assert client.deepbnb_scraper is None


def test_airbnb_client_can_still_opt_into_deepbnb(monkeypatch):
    monkeypatch.setenv("AIRBNB_USE_DEEPBNB_BACKEND", "1")

    client = AirbnbClient({})

    assert client.use_deepbnb_backend is True
    assert client.deepbnb_scraper is not None
