from __future__ import annotations

from worker.core import observations


def test_fixed_pool_reused_comp_prices_are_not_written_as_live_observations(monkeypatch):
    inserted = []

    monkeypatch.setattr(
        observations,
        "_batch_insert",
        lambda _client, _table, rows: inserted.extend(rows),
    )

    count = observations._write_comp_observations(
        client=object(),
        saved_listing_id="listing-1",
        pricing_report_id="report-1",
        captured_at_iso="2026-06-20T00:00:00",
        summary={
            "comparableListings": [
                {
                    "url": "https://www.airbnb.com/rooms/111",
                    "similarity": 0.91,
                    "priceByDate": {
                        "2026-06-21": 120,
                        "2026-06-22": 121,
                    },
                    "priceByDateDetails": {
                        "2026-06-22": {
                            "price": 121,
                            "source": "fixed_pool_reuse",
                            "reused": True,
                        }
                    },
                }
            ]
        },
    )

    assert count == 1
    assert [row["stay_date"] for row in inserted] == ["2026-06-21"]
