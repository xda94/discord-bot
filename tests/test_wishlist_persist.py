import asyncio
from unittest.mock import MagicMock

import db
from db.connection import _connect
from db.wishlist import persist_scrape_pass
from features.wishlist import ScrapeResult, WishlistFeature

URL = "https://example.com/item"


def _row(item_id):
    with _connect() as c:
        c.execute(
            "SELECT last_price, last_stock_status, title, currency, target_alerted, "
            "last_alert_kind, last_alert_price FROM scraped_items WHERE id = ?",
            (item_id,),
        )
        return c.fetchone()


def _history(item_id):
    with _connect() as c:
        c.execute("SELECT price FROM price_history WHERE item_id = ? ORDER BY id", (item_id,))
        return [r[0] for r in c.fetchall()]


def _item():
    return db.add_scraped_item(1, URL, title="Old", price=100.0, stock=True, currency="EUR")


def test_init_db_creates_indexes_and_drops_single_column_price_history_index(tmp_db):
    with _connect() as c:
        c.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        names = {r[0] for r in c.fetchall()}
    assert {"idx_keyword_usage_guild_keyword", "idx_keyword_usage_guild_user", "idx_price_history_item_ts"} <= names
    assert "idx_price_history_item_id" not in names


def test_init_db_drops_legacy_price_history_index(tmp_db):
    with _connect(commit=True) as c:
        c.execute("CREATE INDEX idx_price_history_item_id ON price_history(item_id)")
    db.init_db()
    with _connect() as c:
        c.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_price_history_item_id'")
        assert c.fetchone() is None


def test_persist_scrape_pass_writes_everything(tmp_db):
    item_id = _item()
    assert persist_scrape_pass(
        item_id, price=80.0, in_stock=False, title="New", currency="USD",
        target_alerted=True, alert_kind="low", alert_price=80.0,
    ) is True
    assert _row(item_id) == (80.0, 0, "New", "USD", 1, "low", 80.0)
    assert _history(item_id) == [80.0]


def test_persist_scrape_pass_ellipsis_and_none_leave_values_unchanged(tmp_db):
    item_id = _item()
    db.update_scraped_item_target_state(item_id, True)
    db.update_item_alert_state(item_id, "high", 120.0)
    assert persist_scrape_pass(item_id, price=None, in_stock=None, title=None, currency=None) is True
    assert _row(item_id) == (100.0, 1, "Old", "EUR", 1, "high", 120.0)
    assert _history(item_id) == []


def test_persist_scrape_pass_clears_alert_state(tmp_db):
    item_id = _item()
    db.update_item_alert_state(item_id, "low", 90.0)
    assert persist_scrape_pass(
        item_id, price=95.0, in_stock=True, title=None, currency=None,
        target_alerted=False, alert_kind=None, alert_price=None,
    )
    assert _row(item_id) == (95.0, 1, "Old", "EUR", 0, None, None)


def test_persist_scrape_pass_missing_item_writes_nothing(tmp_db):
    assert persist_scrape_pass(999, price=10.0, in_stock=True, title=None, currency=None) is False
    with _connect() as c:
        c.execute("SELECT COUNT(*) FROM price_history")
        assert c.fetchone()[0] == 0


def test_persist_scrape_pass_failure_rolls_back_everything(tmp_db):
    item_id = _item()
    with _connect(commit=True) as c:
        c.execute(
            "CREATE TRIGGER fail_update BEFORE UPDATE ON scraped_items "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
    before = _row(item_id)
    assert persist_scrape_pass(
        item_id, price=80.0, in_stock=False, title="New", currency="USD",
        target_alerted=True, alert_kind="low", alert_price=80.0,
    ) is False
    assert _row(item_id) == before
    assert _history(item_id) == []


def _feature(monkeypatch, *, price, deliver):
    feature = object.__new__(WishlistFeature)
    feature.scraper = MagicMock()
    feature.scraper.fetch.return_value = ScrapeResult(price=price, in_stock=True, title="New", currency="EUR")
    feature.converter = MagicMock()
    feature.converter.format_with_conversions.side_effect = lambda v, s: f"{v:.2f} {s}"
    feature.client = MagicMock()
    monkeypatch.setattr("features.wishlist.deliver_tracking", deliver)
    monkeypatch.setattr("features.wishlist.generate_price_change_message", lambda *a, **k: None)
    return feature


def test_scrape_item_persists_state_after_notification(monkeypatch, tmp_db):
    item_id = _item()
    sent = []

    async def deliver(*args, **kwargs):
        sent.append(_row(item_id))

    feature = _feature(monkeypatch, price=80.0, deliver=deliver)
    asyncio.run(feature._process_scrape_item(db.get_all_scraped_items()[0]))

    assert sent == [(100.0, 1, "Old", "EUR", 0, None, None)]
    assert _row(item_id)[:4] == (80.0, 1, "New", "EUR")
    assert _history(item_id) == [80.0]


def test_scrape_item_notification_failure_keeps_state_and_records_history(monkeypatch, tmp_db):
    item_id = _item()

    async def deliver(*args, **kwargs):
        raise RuntimeError("queue down")

    feature = _feature(monkeypatch, price=80.0, deliver=deliver)
    asyncio.run(feature._process_scrape_item(db.get_all_scraped_items()[0]))

    assert _row(item_id) == (100.0, 1, "Old", "EUR", 0, None, None)
    assert _history(item_id) == [80.0]
