"""Shared wishlist refresh persistence semantics."""

from types import SimpleNamespace

import db
from wishlist.refresh import refresh_item
from wishlist.scraper import FAILURE_BLOCKED, FAILURE_UNSUPPORTED, ScrapeResult


def _scraper(result):
    return SimpleNamespace(fetch=lambda _url: result)


def test_successful_refresh_updates_snapshot_and_appends_changed_price(tmp_db):
    url = "https://shop.example/item"
    db.add_scraped_item(7, url, title="Old", price=100, stock=False, currency="RON")
    item = db.get_scraped_item(7, url)

    result = ScrapeResult(price=80, in_stock=True, title="New", currency="RON")
    assert refresh_item(item, _scraper(result)) is result

    refreshed = db.get_scraped_item(7, url)
    assert refreshed[3:7] == (80.0, 1, "New", "RON")
    assert refreshed[14] == "ok"
    assert [row[0] for row in db.get_price_history(7, url)] == [80.0]


def test_failed_refresh_records_status_without_overwriting_snapshot(tmp_db):
    url = "https://shop.example/item"
    db.add_scraped_item(7, url, title="Saved", price=100, stock=True, currency="RON")
    original = db.get_scraped_item(7, url)

    for failure in (FAILURE_BLOCKED, FAILURE_UNSUPPORTED):
        result = ScrapeResult(failure=failure)
        refresh_item(original, _scraper(result))
        refreshed = db.get_scraped_item(7, url)
        assert refreshed[3:7] == original[3:7]
        assert refreshed[14] == failure
        assert db.get_price_history(7, url) == []

