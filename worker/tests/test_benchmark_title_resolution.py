"""
The benchmark must be displayed by its real listing name, not the
"Your benchmark listing" placeholder. The PDP payload fetched for the
benchmark's price already carries the title, so the extractor must surface it
(price, confidence, title) and the pipelines must propagate it into the pinned
comp payload and benchmarkInfo.benchmarkTitle.
"""

from worker.core.benchmark import _extract_benchmark_price_with_min_stay_fallback


def _pdp_payload(title: str, price_text: str = "$150"):
    return {
        "data": {
            "presentation": {
                "stayProductDetailPage": {
                    "sections": {
                        "metadata": {"sharingConfig": {"title": title}},
                        "sections": [
                            {
                                "sectionId": "BOOK_IT_SIDEBAR",
                                "section": {
                                    "structuredDisplayPrice": {
                                        "primaryLine": {
                                            "price": price_text,
                                            "qualifier": "night",
                                        }
                                    }
                                },
                            }
                        ],
                    }
                }
            }
        }
    }


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def get_listing_details(self, listing_id, checkin=None, checkout=None, adults=None):
        return self.payload


def test_extractor_returns_pdp_title_alongside_price():
    client = _FakeClient(_pdp_payload("Cozy Cabin Retreat"))
    price, confidence, title = _extract_benchmark_price_with_min_stay_fallback(
        client, "https://www.airbnb.com/rooms/123456", "2026-08-01", "2026-08-02"
    )
    assert title == "Cozy Cabin Retreat"


def test_extractor_returns_title_even_when_price_missing():
    """A blocked/booked date must still resolve the name so the report can
    label the benchmark correctly on days without a price."""
    payload = _pdp_payload("Cozy Cabin Retreat")
    # Strip the price section so extraction fails but title survives.
    payload["data"]["presentation"]["stayProductDetailPage"]["sections"]["sections"] = []
    client = _FakeClient(payload)
    price, confidence, title = _extract_benchmark_price_with_min_stay_fallback(
        client, "https://www.airbnb.com/rooms/123456", "2026-08-01", "2026-08-02"
    )
    assert price is None
    assert title == "Cozy Cabin Retreat"
